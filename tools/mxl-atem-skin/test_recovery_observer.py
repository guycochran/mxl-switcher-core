import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import mxl_recovery_observer as obs  # noqa: E402

TOOLS = str(pathlib.Path(__file__).parent)
RUN = "mxl-recovery-test-0001"
SELECTED = "00000000-0000-4000-8000-0000000000a5"
CONTROL = "00000000-0000-4000-8000-0000000000a6"
OPERATOR_PGM = "00000000-0000-4000-8000-0000000000a1"
OPERATOR_PVW = "00000000-0000-4000-8000-0000000000a3"
AUDIO_FLOW = "00000000-0000-4000-8000-0000000000b5"
PORT = 18190
OPERATOR = "http://operator.invalid:18085"
AUDIO = "http://audio.invalid:9740"


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 0.01)


class Lab:
    """A simulated host: Docker, the candidate Core, two probes, and an operator.

    ``scenario`` is "recreate" (flow destroyed and recreated), "continue"
    (writer stops committing, then resumes), or "never" (operator never acts).
    The operator interrupts ``act_after`` seconds after the prompt and
    restores ``outage`` seconds later.
    """

    def __init__(self, clock, domain, scenario="recreate", act_after=5.0, outage=8.0):
        self.clock, self.domain, self.scenario = clock, domain, scenario
        self.act_after, self.outage = act_after, outage
        self.containers = {"zoomiso-a": self.box("other", running=True), "operator-core": self.box("other", running=True)}
        self.images = {"core:1.0.0.gabc", "lab:test"}
        self.core_start = None
        self.probe_start = {}
        self.interrupt_at = None
        self.operator_program, self.operator_preview = "zoom-1", "zoom-3"
        self.audio_active = {"1": True, "5": False}
        self.faults = set()
        self.extra_drop_at = None
        self.removed_flows = []

    @staticmethod
    def box(label, running=True):
        return {"label": label, "running": running, "restarts": 0, "started": "s0", "exit": 0, "log": ""}

    # operator
    def prompt(self, text):
        if "interrupt the selected" in text and self.scenario != "never":
            self.interrupt_at = self.clock.now + self.act_after

    def phase(self):
        if self.interrupt_at is None or self.clock.now < self.interrupt_at:
            return "before"
        if self.clock.now < self.interrupt_at + self.outage:
            return "outage"
        return "after"

    # candidate Core model
    def health(self):
        t = self.clock.now - self.core_start
        phase = self.phase()
        outage_frames = 0
        if phase != "before":
            outage_frames = int(30 * min(self.clock.now - self.interrupt_at, self.outage))
        held = outage_frames
        skip_events = skipped = reopens = resets = alarms = active = 0
        fallbacks = entries = 0
        if phase != "before":
            fallbacks, entries = outage_frames, 1
        if self.scenario == "recreate" and phase != "before":
            alarms = 1 if outage_frames > 30 else 0
            active = 1 if phase == "outage" and outage_frames > 30 else 0
        if phase == "after":
            if self.scenario == "recreate":
                reopens, resets = 1, 1
            else:
                skip_events, skipped = 1, held + ("skew" in self.faults) * 5
        if "synthetic" in self.faults and phase == "after":
            skip_events, skipped = 1, 4_000_000_000
        head = 1000 + int(30 * t)
        if phase == "outage":
            head = 1000 + int(30 * (self.interrupt_at - self.core_start))
        elif phase == "after" and self.scenario == "recreate":
            head = 50 + int(30 * (self.clock.now - self.interrupt_at - self.outage))
        lag = 3 if phase == "after" and self.clock.now < self.interrupt_at + self.outage + 2 else 1
        too_late = 1 if "too_late" in self.faults and phase != "before" else 0
        return {"status": "ok", "held": held, "invalid": 0, "too_early": 0, "too_late": too_late, "timeouts": 0,
                "reader_reopens": reopens, "input_timeline_resets": 2 + resets,
                "sustained_unavailable_alarms": alarms, "sustained_unavailable_active": active,
                "presentation_fallbacks": fallbacks, "presentation_fallback_entries": entries,
                "selected_input_repeats": held, "selected_input_skip_events": skip_events,
                "selected_input_skipped_grains": skipped, "selected_input_regressions": 0,
                "sources": [{"flow_id": SELECTED, "reader_reopens": reopens, "latest_head_index": head,
                             "latest_input_lag_frames": lag},
                            {"flow_id": CONTROL, "reader_reopens": 0, "latest_head_index": 1000 + int(30 * t),
                             "latest_input_lag_frames": 1}]}

    def fetch(self, url):
        if url.startswith(OPERATOR):
            if url.endswith("/state"):
                return {"program": self.operator_program, "preview": self.operator_preview}
            return {"sources": [{"id": "zoom-1", "flow_id": OPERATOR_PGM}, {"id": "zoom-3", "flow_id": OPERATOR_PVW},
                                {"id": "zoom-5", "flow_id": SELECTED}, {"id": "zoom-6", "flow_id": CONTROL}]}
        if url.startswith(AUDIO):
            return {"mode": "afv", "channels": {n: {"active": v, "flow_id": AUDIO_FLOW if n == "5" else None}
                                                 for n, v in self.audio_active.items()}}
        if f":{PORT}/" in url:
            core = self.containers.get(f"{RUN}-core")
            if not core or not core["running"] or self.core_start is None:
                raise OSError("connection refused")
            if url.endswith("/state"):
                if "misrouted" in self.faults:
                    return {"program": "gate-ctl-src", "preview": "gate-sel-src"}
                return {"program": "gate-sel-src", "preview": "gate-ctl-src"}
            if url.endswith("/health"):
                return self.health()
        raise AssertionError(url)

    def probe_report(self, name):
        role = "program" if name.endswith("program") else "preview"
        started = self.probe_start[role]
        if self.clock.now - started < 1.5:
            return ""
        frames = int(30 * (self.clock.now - started))
        drops = 1 if self.extra_drop_at and self.clock.now >= self.extra_drop_at and role == "program" else 0
        return json.dumps({"t": self.clock.now, "frames": frames, "drops": drops, "stalls": 0, "torn": 0,
                           "longest_gap_ms": 40.0}) + "\n"

    def __call__(self, command, **_):
        args = command[1:]
        verb = args[0]
        ok = SimpleNamespace(returncode=0, stdout="", stderr="")
        if verb == "version":
            return SimpleNamespace(returncode=0, stdout="27", stderr="")
        if verb == "image":
            return SimpleNamespace(returncode=0 if args[-1] in self.images else 1, stdout="[]", stderr="")
        if verb == "container":
            box = self.containers.get(args[-1])
            if box is None:
                return SimpleNamespace(returncode=1, stdout="[]", stderr="No such container")
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps([{
                "State": {"Running": box["running"], "ExitCode": box["exit"], "StartedAt": box["started"]},
                "RestartCount": box["restarts"], "Config": {"Labels": {obs.LABEL: box["label"]}}}]))
        if verb == "run":
            name = args[args.index("--name") + 1]
            label = args[args.index("--label") + 1].split("=", 1)[1]
            if "domain-check" in args:
                flows = [args[i + 1] for i, a in enumerate(args) if a == "--flow"]
                present = {f: os.path.isdir(os.path.join(self.domain, f"{f}.mxl-flow")) for f in flows}
                return SimpleNamespace(returncode=0, stdout=json.dumps({"present": present}) + "\n", stderr="")
            if "remove-flows" in args:
                flows = [args[i + 1] for i, a in enumerate(args) if a == "--flow"]
                self.removed_flows += flows
                return SimpleNamespace(returncode=0, stdout=json.dumps({"removed": flows}) + "\n", stderr="")
            self.containers[name] = self.box(label)
            if name.endswith("-core"):
                self.core_start = self.clock.now
                if "relabel" in self.faults:
                    self.containers[name]["label"] = "someone-else"
            else:
                self.probe_start["program" if name.endswith("program") else "preview"] = self.clock.now
            return ok
        if verb == "logs":
            name = args[-1]
            if name not in self.containers:
                return SimpleNamespace(returncode=1, stdout="", stderr="no such container")
            if "probe" in name:
                return SimpleNamespace(returncode=0, stdout=self.probe_report(name), stderr="")
            return SimpleNamespace(returncode=0, stdout="core log\n", stderr="")
        if verb == "rm":
            self.containers.pop(args[-1], None)
            return ok
        raise AssertionError(command)


def plan_args(evidence, domain, *extra):
    return ["plan", "--image", "core:1.0.0.gabc", "--probe-image", "lab:test", "--domain-path", domain,
            "--selected-source", "gate-sel-src", "--selected-flow", SELECTED, "--selected-audio-flow", AUDIO_FLOW,
            "--control-source", "gate-ctl-src", "--control-flow", CONTROL,
            "--operator-core-url", OPERATOR, "--program-audio-url", AUDIO,
            "--permanent", "zoomiso-a", "--permanent", "operator-core",
            "--host-port", str(PORT), "--run-id", RUN, "--evidence-dir", evidence,
            "--baseline-seconds", "5", "--interruption-timeout", "60", "--restoration-timeout", "60",
            "--recovery-timeout", "30", *extra]


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.domain = os.path.join(self.tmp.name, "domain")
        for flow in (SELECTED, CONTROL, OPERATOR_PGM):
            os.makedirs(os.path.join(self.domain, f"{flow}.mxl-flow"))
        self.evidence = os.path.join(self.tmp.name, "evidence")
        self.clock = Clock()
        self.lab = Lab(self.clock, self.domain)

    def run_gate(self, *extra, apply=True, port_free=True):
        args = plan_args(self.evidence, self.domain, *extra) + (["--apply"] if apply else [])
        self.docker = obs.Docker(run=self.lab)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = obs.main(args, docker=self.docker, fetch=self.lab.fetch, port_check=lambda *_: port_free,
                            clock=self.clock, sleep=self.clock.sleep, say=self.lab.prompt)
        return code, out.getvalue(), err.getvalue()

    def verdict(self):
        return json.loads(pathlib.Path(self.evidence, "verdict.json").read_text())

    def mutations(self):
        return [c for c in self.docker.log if c[1] in ("run", "rm", "stop", "create")]


class PlanTest(Harness):
    def test_dry_run_changes_nothing_and_shows_the_plan(self):
        code, out, _ = self.run_gate(apply=False)
        self.assertEqual(code, 0)
        plan = json.loads(out)["plan"]
        self.assertEqual(plan["containers"]["core"], f"{RUN}-core")
        self.assertEqual(plan["selected"]["candidate_bus"], "program")
        self.assertEqual(plan["control"]["candidate_bus"], "preview")
        core = plan["commands"]["core"]
        self.assertIn(f"127.0.0.1:{PORT}:18085", core)
        self.assertEqual(core[core.index("--restart") + 1], "no")
        self.assertIn(f"{obs.LABEL}={RUN}", core)
        probe = plan["commands"]["probe_program"]
        self.assertEqual(probe[probe.index("--network") + 1], "none")
        self.assertTrue(any(part.endswith(",readonly") and "mxl-domain" in part for part in probe))
        self.assertFalse(any("readonly" in part and "mxl-domain" in part
                             for part in plan["commands"]["remove_candidate_flows"]))
        self.assertEqual(self.mutations(), [])
        self.assertFalse(os.path.exists(self.evidence))

    def test_generated_candidate_config_routes_selected_to_program(self):
        args = obs.build_parser().parse_args(plan_args(self.evidence, self.domain))
        plan = obs.Plan(args, RUN)
        config = plan.core_config()
        self.assertIn(f"source=gate-sel-src|{SELECTED}|", config)
        self.assertIn("initial_program=gate-sel-src", config)
        self.assertIn("initial_preview=gate-ctl-src", config)
        self.assertIn("input_delay_frames=1", config)
        self.assertNotIn(OPERATOR_PGM, config)

    def test_runs_are_unique(self):
        self.assertNotEqual(obs.new_run_id(), obs.new_run_id())
        args = obs.build_parser().parse_args(plan_args(self.evidence, self.domain))
        a, b = obs.Plan(args, RUN), obs.Plan(args, RUN)
        self.assertFalse(set(a.outputs.values()) & set(b.outputs.values()))


class RefusalTest(Harness):
    def refused(self, *extra, contains, apply=True, port_free=True, arrange=None):
        if arrange:
            arrange()
        code, _, err = self.run_gate(*extra, apply=apply, port_free=port_free)
        self.assertEqual(code, 2, err)
        self.assertIn(contains, json.loads(err)["refused"])
        self.assertEqual(self.mutations(), [])
        self.assertFalse(os.path.exists(self.evidence))

    def test_selected_on_operator_program(self):
        def arrange():
            self.lab.operator_program = "zoom-5"
        self.refused(contains="operator Core's Program", arrange=arrange)

    def test_selected_on_operator_preview_unless_allowed(self):
        def arrange():
            self.lab.operator_preview = "zoom-5"
        self.refused(contains="operator Preview", arrange=arrange)
        self.setUp()
        self.lab.operator_preview = "zoom-5"
        code, _, _ = self.run_gate("--allow-operator-preview", apply=False)
        self.assertEqual(code, 0)

    def test_selected_active_in_program_audio(self):
        def arrange():
            self.lab.audio_active["5"] = True
        self.refused(contains="active in Program Audio", arrange=arrange)

    def test_program_audio_identity_is_required(self):
        args = [a for a in plan_args(self.evidence, self.domain) if a not in ("--selected-audio-flow", AUDIO_FLOW)]
        self.docker = obs.Docker(run=self.lab)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = obs.main(args, docker=self.docker, fetch=self.lab.fetch, port_check=lambda *_: True)
        self.assertEqual(code, 2)
        self.assertIn("--selected-audio-flow", err.getvalue())

    def test_collisions_and_prerequisites(self):
        cases = [
            ("already exists", lambda: self.lab.containers.update({f"{RUN}-core": Lab.box("x")}), {}),
            ("in use", None, {"port_free": False}),
            ("not present", lambda: self.lab.images.discard("lab:test"), {}),
            ("not running", lambda: self.lab.containers.update({"zoomiso-a": Lab.box("other", running=False)}), {}),
            ("evidence dir already exists", lambda: os.makedirs(self.evidence), {}),
            ("selected source flow is not in the domain",
             lambda: os.rmdir(os.path.join(self.domain, f"{SELECTED}.mxl-flow")), {}),
        ]
        for contains, arrange, kwargs in cases:
            with self.subTest(contains):
                self.setUp()
                if arrange:
                    arrange()
                code, _, err = self.run_gate(**kwargs)
                self.assertEqual(code, 2, err)
                self.assertIn(contains, json.loads(err)["refused"])
                self.assertEqual(self.mutations(), [])

    def test_unsafe_arguments(self):
        for extra, contains in ((("--forbid-port", str(PORT)), "permanent port"),
                                (("--stable-seconds", "10"), "at least 30")):
            with self.subTest(contains):
                self.setUp()
                self.refused(*extra, contains=contains)
        self.setUp()
        args = plan_args(self.evidence, self.domain)
        args[args.index("--control-flow") + 1] = SELECTED
        self.docker = obs.Docker(run=self.lab)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(obs.main(args, docker=self.docker, fetch=self.lab.fetch, port_check=lambda *_: True), 2)
        self.assertIn("must differ", err.getvalue())


class GateTest(Harness):
    def test_destroyed_and_recreated_flow_passes(self):
        code, out, _ = self.run_gate()
        verdict = self.verdict()
        self.assertEqual(code, 0, json.dumps(verdict["checks"], indent=1))
        self.assertEqual(verdict["signature"], "destroyed_recreated")
        for mark in ("baseline", "awaiting_interruption", "interruption_observed", "recovery_observed",
                     "fallback_exit", "stable_recovery"):
            self.assertIn(mark, verdict["marks"])
        self.assertEqual(verdict["deltas"]["selected_reader_reopens"], 1)
        self.assertEqual(verdict["deltas"]["selected_input_skip_events"], 0)
        self.assertTrue(verdict["permanent_services"]["unchanged"])

    def test_stopped_and_continued_writer_passes(self):
        self.lab.scenario = "continue"
        code, _, _ = self.run_gate()
        verdict = self.verdict()
        self.assertEqual(code, 0, json.dumps(verdict["checks"], indent=1))
        self.assertEqual(verdict["signature"], "stopped_continued")
        self.assertEqual(verdict["deltas"]["selected_input_skipped_grains"], verdict["outage_frames"])

    def test_cleanup_is_exact_and_evidence_is_kept(self):
        self.run_gate()
        cleanup = self.verdict()["cleanup"]
        self.assertEqual(sorted(cleanup["removed"]),
                         sorted([f"{RUN}-core", f"{RUN}-probe-program", f"{RUN}-probe-preview"]))
        self.assertEqual(set(self.lab.containers), {"zoomiso-a", "operator-core"})
        plan = json.loads(pathlib.Path(self.evidence, "plan.json").read_text())
        self.assertEqual(sorted(self.lab.removed_flows), sorted(plan["candidate_outputs"].values()))
        self.assertNotIn(SELECTED, self.lab.removed_flows)
        removals = [c[-1] for c in self.docker.log if c[1] == "rm"]
        self.assertTrue(all(name.startswith(RUN + "-") for name in removals))
        for command in self.docker.log:
            self.assertNotIn("prune", command)
            self.assertFalse(any(part in ("zoomiso-a", "operator-core") for part in command
                                 if command[1] in ("run", "rm", "stop", "exec", "restart")))
        files = set(os.listdir(self.evidence))
        self.assertTrue({"samples.jsonl", "events.json", "plan.json", "preflight.json", "verdict.json",
                         "public-summary.json", "cleanup.json", f"{RUN}-core.log"} <= files)

    def test_public_summary_is_redacted(self):
        self.run_gate()
        text = pathlib.Path(self.evidence, "public-summary.json").read_text()
        for secret in (SELECTED, CONTROL, "gate-sel-src", "gate-ctl-src", self.evidence, OPERATOR, AUDIO):
            self.assertNotIn(secret, text)
        self.assertEqual(json.loads(text)["verdict"], "PASS")

    def test_operator_never_acting_times_out_and_cleans_up(self):
        self.lab.scenario = "never"
        code, _, _ = self.run_gate()
        verdict = self.verdict()
        self.assertEqual(code, 1)
        self.assertIn("awaiting_interruption", verdict["checks"]["sequence_completed"]["detail"])
        self.assertLess(self.clock.now, 200)
        self.assertEqual(set(self.lab.containers), {"zoomiso-a", "operator-core"})

    def test_probe_drop_fails(self):
        self.lab.extra_drop_at = 25.0
        code, _, _ = self.run_gate()
        self.assertEqual(code, 1)
        self.assertIn("dropped", self.verdict()["checks"]["sequence_completed"]["detail"])

    def test_must_not_move_counter_fails_immediately(self):
        self.lab.faults.add("too_late")
        code, _, _ = self.run_gate()
        self.assertEqual(code, 1)
        self.assertIn("too_late moved", self.verdict()["checks"]["sequence_completed"]["detail"])

    def test_skip_that_does_not_match_the_outage_is_no_signature(self):
        self.lab.scenario = "continue"
        self.lab.faults.add("skew")
        code, _, _ = self.run_gate()
        verdict = self.verdict()
        self.assertEqual(code, 1)
        self.assertIsNone(verdict["signature"])
        self.assertFalse(verdict["checks"]["exactly_one_recovery_signature"]["pass"])

    def test_synthetic_epoch_skip_fails(self):
        self.lab.faults.add("synthetic")
        code, _, _ = self.run_gate()
        self.assertEqual(code, 1)
        self.assertFalse(self.verdict()["checks"]["no_synthetic_skip"]["pass"])

    def test_misrouted_candidate_is_refused_before_baseline(self):
        self.lab.faults.add("misrouted")
        code, _, _ = self.run_gate()
        self.assertEqual(code, 1)
        self.assertIn("selected on Program", self.verdict()["checks"]["sequence_completed"]["detail"])

    def test_permanent_service_restart_fails(self):
        original = self.lab.prompt

        def restart_permanent(text):
            original(text)
            self.lab.containers["zoomiso-a"]["restarts"] = 1
        self.lab.prompt = restart_permanent
        code, _, _ = self.run_gate()
        verdict = self.verdict()
        self.assertEqual(code, 1)
        self.assertEqual(verdict["permanent_services"]["changed"], ["zoomiso-a"])

    def test_ctrl_c_preserves_evidence_and_cleans_up(self):
        observer_holder = {}
        original_init = obs.Observer.__init__

        def capture(self_, *args, **kwargs):
            original_init(self_, *args, **kwargs)
            observer_holder["o"] = self_
        obs.Observer.__init__ = capture
        self.addCleanup(setattr, obs.Observer, "__init__", original_init)
        original = self.lab.prompt

        def interrupt(text):
            original(text)
            observer_holder["o"]._on_signal()
        self.lab.prompt = interrupt
        code, _, _ = self.run_gate()
        verdict = self.verdict()
        self.assertEqual(code, 1)
        self.assertIn("Interrupted", verdict["checks"]["sequence_completed"]["detail"])
        self.assertEqual(set(self.lab.containers), {"zoomiso-a", "operator-core"})
        self.assertTrue(pathlib.Path(self.evidence, "samples.jsonl").is_file())

    def test_cleanup_leaves_a_container_whose_label_is_not_ours(self):
        self.lab.faults.add("relabel")
        self.run_gate()
        cleanup = self.verdict()["cleanup"]
        self.assertIn(f"{RUN}-core", self.lab.containers)
        self.assertTrue(any(entry.startswith(f"{RUN}-core:") for entry in cleanup["not_removed"]))
        self.assertIn("still present", cleanup["candidate_flows"]["error"])
        self.assertEqual(self.lab.removed_flows, [])


class EvaluateTest(unittest.TestCase):
    a = SimpleNamespace(input_delay_frames=1)

    def sample(self, t, **health):
        base = {key: 0 for key in (*obs.MUST_NOT_MOVE, *obs.OUTAGE_MEASUREMENTS, *obs.SIGNATURE_COUNTERS)}
        base.update(sustained_unavailable_active=0)
        reopens = health.pop("reopens", 0)
        base.update(health)
        return {"t": t, "health": base, "selected": {"reader_reopens": reopens, "latest_input_lag_frames": 1},
                "probes": {r: {"frames": int(30 * t), "drops": 0, "stalls": 0} for r in ("program", "preview")}}

    def test_signatures_are_mutually_exclusive(self):
        base = self.sample(0)
        recreated = self.sample(60, held=300, reader_reopens=1, reopens=1, input_timeline_resets=1,
                                sustained_unavailable_alarms=1)
        continued = self.sample(60, held=300, selected_input_skip_events=1, selected_input_skipped_grains=301)
        self.assertEqual(obs.evaluate([base, recreated], base, self.a, None)["signature"], "destroyed_recreated")
        self.assertEqual(obs.evaluate([base, continued], base, self.a, None)["signature"], "stopped_continued")
        both = self.sample(60, held=300, reopens=1, input_timeline_resets=1, sustained_unavailable_alarms=1,
                           selected_input_skip_events=1, selected_input_skipped_grains=300)
        self.assertIsNone(obs.evaluate([base, both], base, self.a, None)["signature"])
        neither = self.sample(60, held=300)
        self.assertIsNone(obs.evaluate([base, neither], base, self.a, None)["signature"])

    def test_recreate_without_alarm_is_not_a_signature(self):
        base = self.sample(0)
        quick = self.sample(60, held=10, reopens=1, input_timeline_resets=1)
        self.assertIsNone(obs.evaluate([base, quick], base, self.a, None)["signature"])

    def test_no_baseline_fails(self):
        self.assertEqual(obs.evaluate([], None, self.a, "GateFailure: x")["verdict"], "FAIL")


class InContainerTest(unittest.TestCase):
    def test_remove_flows_touches_only_listed_uuid_directories(self):
        with tempfile.TemporaryDirectory() as domain:
            keep = os.path.join(domain, f"{SELECTED}.mxl-flow")
            gone = os.path.join(domain, f"{CONTROL}.mxl-flow")
            os.makedirs(keep)
            os.makedirs(gone)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                obs.run_remove_flows(SimpleNamespace(domain=domain, flow=[CONTROL, "../escape", "*"]))
            self.assertTrue(os.path.isdir(keep))
            self.assertFalse(os.path.exists(gone))
            self.assertEqual(json.loads(out.getvalue())["removed"], [CONTROL])

    def test_domain_check_reports_presence(self):
        with tempfile.TemporaryDirectory() as domain:
            os.makedirs(os.path.join(domain, f"{SELECTED}.mxl-flow"))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                obs.run_domain_check(SimpleNamespace(domain=domain, flow=[SELECTED, CONTROL]))
            self.assertEqual(json.loads(out.getvalue())["present"], {SELECTED: True, CONTROL: False})


if __name__ == "__main__":
    unittest.main()
