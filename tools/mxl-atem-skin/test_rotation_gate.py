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
import mxl_rotation_gate as gate  # noqa: E402

RUN = "mxl-recovery-rot-0001"
PORT = 18191
OPERATOR = "http://operator.invalid:18085"
AUDIO_URL = "http://audio.invalid:9740"
EASY = "http://easy.invalid:9700"
WRITER = {"id": "c0ffee00c0ffee00", "name": "zoomiso-a"}
OLD_V = "00000000-0000-4000-8000-0000000004a0"
OLD_A = "00000000-0000-4000-8000-0000000004b0"
NEW_V = "00000000-0000-4000-8000-0000000004a1"
NEW_A = "00000000-0000-4000-8000-0000000004b1"
CONTROL = "00000000-0000-4000-8000-0000000002a0"
OP_PGM = "00000000-0000-4000-8000-0000000001a0"
GROUP, VROLE, AROLE = "ZoomISO Output 4", "video", "audio"
SEL_SRC, CTL_SRC = "gate-rot-sel", "gate-rot-ctl"


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 0.01)


def flow(fid, kind, role, active=True, writer=WRITER, group=GROUP, media=None):
    definition = ({"frame_width": 1920, "frame_height": 1080, "grain_rate": {"numerator": 30, "denominator": 1},
                   "media_type": "video/v210"} if kind == "video"
                  else {"sample_rate": {"numerator": 48000}, "channel_count": 2, "media_type": "audio/float32"})
    return {"id": fid, "format": kind, "group": group, "role": role, "def": definition,
            "active": active, "status": "active" if active else "inactive",
            "writerContainer": None if writer is None else dict(writer)}


class Lab:
    """Simulated host: Docker, EasyMXL, the candidate Core, probes, motion, and the operator."""

    def __init__(self, clock, domain, scenario="rotate", act_after=4.0, gap=6.0):
        self.clock, self.domain, self.scenario = clock, domain, scenario
        self.act_after, self.gap = act_after, gap
        self.containers = {"zoomiso-a": self.box("other"), "operator-core": self.box("other")}
        self.images = {"core:1.0.0.gabc", "lab:test"}
        self.faults = set()
        self.interrupt_at = None
        self.core_start = None
        self.probe_start = {}
        self.revision = 0
        self.bindings = {}
        self.puts = []
        self.motion_fps = {}
        self.removed_flows = []

    @staticmethod
    def box(label):
        return {"label": label, "running": True, "restarts": 0, "started": "s0", "exit": 0}

    # operator ---------------------------------------------------------------
    def prompt(self, text):
        if "switch the declared ZoomISO output away" in text and self.scenario != "never":
            self.interrupt_at = self.clock.now + self.act_after

    def phase(self):
        if self.interrupt_at is None or self.clock.now < self.interrupt_at:
            return "before"
        return "gap" if self.clock.now < self.interrupt_at + self.gap else "after"

    # EasyMXL ----------------------------------------------------------------
    def inventory(self):
        phase = self.phase()
        control = [flow(CONTROL, "video", VROLE, group="ZoomISO Output 2"),
                   flow(OP_PGM, "video", VROLE, group="ZoomISO Output 1")]
        if phase == "before":
            return control + [flow(OLD_V, "video", VROLE), flow(OLD_A, "audio", AROLE)]
        old = [flow(OLD_V, "video", VROLE, active=False), flow(OLD_A, "audio", AROLE, active=False)]
        if phase == "gap" or self.scenario == "never_returns":
            return control + old
        if self.scenario == "same":
            return control + [flow(OLD_V, "video", VROLE), flow(OLD_A, "audio", AROLE)]
        new = [flow(NEW_V, "video", VROLE), flow(NEW_A, "audio", AROLE)]
        if "ambiguous" in self.faults:
            new.append(flow("00000000-0000-4000-8000-0000000004a2", "video", VROLE))
        if "missing_writer" in self.faults:
            new[0]["writerContainer"] = None
        if "pair_mismatch" in self.faults:
            new[1]["writerContainer"] = {"id": "another-container", "name": WRITER["name"]}
        if "collision" in self.faults:
            new[0]["id"] = CONTROL
        return control + old + new

    # candidate Core -----------------------------------------------------------
    def core_sources(self):
        fmt = {"width": 1920, "height": 1080, "rate_numerator": 30, "rate_denominator": 1, "encoding": "video/v210"}
        return [{"id": sid, "flow_id": b["flow_id"], "audio_flow_id": b["audio_flow_id"], "format": fmt,
                 "label": sid, "kind": "mxl", "capabilities": ["video"]} for sid, b in self.bindings.items()]

    def core_state(self):
        if "revision_bump" in self.faults and self.phase() == "after":
            self.revision = max(self.revision, 99)
        return {"revision": self.revision, "program": SEL_SRC, "preview": CTL_SRC,
                "program_flow": self.outputs["program"], "preview_flow": self.outputs["preview"]}

    def health(self):
        phase = self.phase()
        gap_frames = 0 if phase == "before" else int(30 * min(self.clock.now - self.interrupt_at, self.gap))
        selected = self.bindings[SEL_SRC]["flow_id"]
        rebound = selected != OLD_V
        lag = 1
        return {"held": gap_frames, "invalid": 0, "too_early": 0, "too_late": 0, "timeouts": 0,
                "reader_reopens": 0, "input_timeline_resets": 2 + (1 if rebound else 0),
                "sustained_unavailable_alarms": 1 if gap_frames > 30 else 0, "sustained_unavailable_active": 0,
                "presentation_fallbacks": gap_frames, "presentation_fallback_entries": 1 if gap_frames else 0,
                "selected_input_repeats": gap_frames, "selected_input_skip_events": 0,
                "selected_input_skipped_grains": 0, "selected_input_regressions": 0,
                "sources": [{"flow_id": selected, "reader_reopens": 0, "latest_head_index": int(30 * self.clock.now),
                             "latest_input_lag_frames": lag},
                            {"flow_id": CONTROL, "reader_reopens": 0, "latest_head_index": int(30 * self.clock.now),
                             "latest_input_lag_frames": 1}]}

    def put(self, path, body):
        source = path.rsplit("/", 1)[-1]
        if "apply_fail" in self.faults and body["flow_id"] == NEW_V:
            self.bindings[source] = dict(body)
            self.revision += 2  # a concurrent change: the executor must roll back
            self.puts.append((source, dict(body)))
            return {"changed": True, "revision": self.revision}
        self.bindings[source] = dict(body)
        self.revision += 1
        self.puts.append((source, dict(body)))
        return {"changed": True, "revision": self.revision}

    def request_json(self, base, path, method="GET", body=None, timeout=10.0):
        assert base == f"http://127.0.0.1:{PORT}", base
        if method == "PUT":
            return self.put(path, body)
        if path.endswith("/state"):
            return self.core_state()
        if path.endswith("/sources"):
            return self.core_sources()
        raise AssertionError(path)

    def fetch(self, url):
        if url.startswith(OPERATOR):
            if url.endswith("/state"):
                return {"program": "zoom-1", "preview": "zoom-3"}
            return [{"id": "zoom-1", "flow_id": OP_PGM}, {"id": "zoom-3", "flow_id": "00000000-0000-4000-8000-0000000003a0"},
                    {"id": "zoom-4", "flow_id": OLD_V}]
        if url.startswith(AUDIO_URL):
            return {"mode": "afv", "channels": {"4": {"active": False, "flow_id": OLD_A}}}
        if f":{PORT}/" in url:
            if self.core_start is None or f"{RUN}-core" not in self.containers:
                raise OSError("connection refused")
            if url.endswith("/state"):
                return self.core_state()
            if url.endswith("/sources"):
                return self.core_sources()
            if url.endswith("/health"):
                return self.health()
        raise AssertionError(url)

    # docker -------------------------------------------------------------------
    def probe_report(self, name):
        role = "program" if name.endswith("program") else "preview"
        if self.clock.now - self.probe_start[role] < 1.5:
            return ""
        return json.dumps({"frames": int(30 * (self.clock.now - self.probe_start[role])), "drops": 0, "stalls": 0}) + "\n"

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
            if "motion" in args:
                target = args[args.index("--flow") + 1]
                fps = self.motion_fps.get(target, 28.0)
                return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
                    {"flow": target, "frames": 150, "changes": int(fps * 5), "seconds": 5.0, "change_fps": fps}) + "\n")
            if "remove-flows" in args:
                flows = [args[i + 1] for i, a in enumerate(args) if a == "--flow"]
                self.removed_flows += flows
                return SimpleNamespace(returncode=0, stdout=json.dumps({"removed": flows}) + "\n", stderr="")
            self.containers[name] = self.box(label)
            if name.endswith("-core"):
                self.core_start = self.clock.now
                self.bindings = {SEL_SRC: {"flow_id": OLD_V, "audio_flow_id": None},
                                 CTL_SRC: {"flow_id": CONTROL, "audio_flow_id": None}}
            else:
                self.probe_start["program" if name.endswith("program") else "preview"] = self.clock.now
            return ok
        if verb == "logs":
            name = args[-1]
            if name not in self.containers:
                return SimpleNamespace(returncode=1, stdout="", stderr="no such container")
            return SimpleNamespace(returncode=0, stdout=self.probe_report(name) if "probe" in name else "log\n",
                                   stderr="")
        if verb == "rm":
            self.containers.pop(args[-1], None)
            return ok
        raise AssertionError(command)


def args_for(evidence, domain, *extra):
    return ["plan", "--image", "core:1.0.0.gabc", "--probe-image", "lab:test", "--domain-path", domain,
            "--selected-source", SEL_SRC, "--selected-flow", OLD_V, "--selected-audio-flow", OLD_A,
            "--control-source", CTL_SRC, "--control-flow", CONTROL,
            "--operator-core-url", OPERATOR, "--program-audio-url", AUDIO_URL,
            "--permanent", "zoomiso-a", "--permanent", "operator-core",
            "--host-port", str(PORT), "--run-id", RUN, "--evidence-dir", evidence,
            "--easy-mxl-url", EASY, "--easy-mxl-domain", "lab",
            "--writer-container", WRITER["name"], "--group", GROUP, "--video-role", VROLE, "--audio-role", AROLE,
            "--baseline-seconds", "5", "--interruption-timeout", "60", "--restoration-timeout", "60",
            "--recovery-timeout", "30", "--motion-seconds", "1", *extra]


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.domain = os.path.join(self.tmp.name, "domain")
        for fid in (OLD_V, CONTROL):
            os.makedirs(os.path.join(self.domain, f"{fid}.mxl-flow"))
        self.evidence = os.path.join(self.tmp.name, "evidence")
        self.clock = Clock()
        self.lab = Lab(self.clock, self.domain)
        original = gate.executor._request_json
        gate.executor._request_json = self.lab.request_json
        self.addCleanup(setattr, gate.executor, "_request_json", original)

    def run_gate(self, *extra, apply=True, confirm=True):
        argv = args_for(self.evidence, self.domain, *extra) + (["--apply"] if apply else []) \
            + (["--confirm", gate.CONFIRMATION] if confirm else [])
        self.docker = obs.Docker(run=self.lab)
        original_plan = obs.Plan.__init__
        lab = self.lab

        def capture(self_, *args, **kwargs):
            original_plan(self_, *args, **kwargs)
            lab.outputs = self_.outputs
        obs.Plan.__init__ = capture
        try:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = gate.main(argv, docker=self.docker, fetch=self.lab.fetch, inventory=self.lab.inventory,
                                 port_check=lambda *_: True, clock=self.clock, sleep=self.clock.sleep,
                                 say=self.lab.prompt)
        finally:
            obs.Plan.__init__ = original_plan
        return code, out.getvalue(), err.getvalue()

    def verdict(self):
        return json.loads(pathlib.Path(self.evidence, "verdict.json").read_text())

    def detail(self):
        return self.verdict()["checks"]["sequence_completed"]["detail"]

    def rotation_puts(self):
        return [p for p in self.lab.puts if p[1]["flow_id"] != OLD_V]


class DryRunTest(Harness):
    def test_dry_run_records_the_old_pair_and_changes_nothing(self):
        code, out, _ = self.run_gate(apply=False, confirm=False)
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["preflight"]["old_pair"]["pair"], {"flow_id": OLD_V, "audio_flow_id": OLD_A})
        self.assertEqual(report["preflight"]["old_pair"]["writer"], WRITER)
        self.assertEqual([c for c in self.docker.log if c[1] in ("run", "rm")], [])
        self.assertEqual(self.lab.puts, [])
        self.assertFalse(os.path.exists(self.evidence))

    def test_selected_flow_must_be_the_mapped_active_video(self):
        argv = args_for(self.evidence, self.domain)
        argv[argv.index("--group") + 1] = "ZoomISO Output 2"  # the control's group: a different active video
        self.docker = obs.Docker(run=self.lab)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = gate.main(argv, docker=self.docker, fetch=self.lab.fetch, inventory=self.lab.inventory,
                             port_check=lambda *_: True)
        self.assertEqual(code, 2)
        self.assertIn("refused", err.getvalue())

    def test_confirm_must_be_the_literal_phrase(self):
        argv = args_for(self.evidence, self.domain) + ["--confirm", "yes"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gate.main(argv, docker=obs.Docker(run=self.lab), fetch=self.lab.fetch,
                                       inventory=self.lab.inventory, port_check=lambda *_: True), 2)


class RotationTest(Harness):
    def test_uuid_rotation_rebinds_the_stable_source(self):
        code, _, _ = self.run_gate()
        verdict = self.verdict()
        self.assertEqual(code, 0, json.dumps(verdict["checks"], indent=1))
        self.assertEqual(verdict["path"], "rotated")
        self.assertEqual(self.lab.bindings[SEL_SRC], {"flow_id": NEW_V, "audio_flow_id": NEW_A})
        self.assertEqual(self.lab.bindings[CTL_SRC]["flow_id"], CONTROL)
        self.assertEqual(self.rotation_puts(), [(SEL_SRC, {"flow_id": NEW_V, "audio_flow_id": NEW_A})])
        receipt = json.loads(pathlib.Path(self.evidence, "rotation-rebind-receipt.json").read_text())
        self.assertEqual(receipt["final_revision"], receipt["starting_revision"] + 1)
        for mark in ("old_pair_recorded", "baseline", "old_pair_inactive", "replacement_discovered",
                     "rebind_applied", "identity_verified", "stable_recovery"):
            self.assertIn(mark, verdict["marks"])
        for label in ("selected_before", "control_before", "program_after"):
            self.assertTrue(verdict["checks"][f"content_{label}"]["pass"])

    def test_same_uuid_continuation_needs_no_rebind(self):
        self.lab.scenario = "same"
        code, _, _ = self.run_gate()
        verdict = self.verdict()
        self.assertEqual(code, 0, json.dumps(verdict["checks"], indent=1))
        self.assertEqual(verdict["path"], "same_uuid_continued")
        self.assertEqual(self.rotation_puts(), [])
        self.assertEqual(self.lab.bindings[SEL_SRC], {"flow_id": OLD_V, "audio_flow_id": OLD_A})

    def test_static_content_is_rejected_before_baseline(self):
        self.lab.motion_fps[OLD_V] = 0.0  # cadence continues, picture does not
        code, _, _ = self.run_gate()
        self.assertEqual(code, 1)
        self.assertIn("not changing content", self.detail())
        self.assertNotIn("baseline", self.verdict()["marks"])
        self.assertIsNone(self.lab.interrupt_at)

    def test_refusals_after_the_replacement_appears(self):
        cases = {
            "ambiguous": "replacement refused: ambiguous",
            "missing_writer": "no writer attribution",
            "pair_mismatch": "different writers",
            "collision": "bound_to_other_source",
        }
        for fault, contains in cases.items():
            with self.subTest(fault):
                self.setUp()
                self.lab.faults.add(fault)
                code, _, _ = self.run_gate()
                self.assertEqual(code, 1)
                self.assertIn(contains, self.detail())
                self.assertEqual(self.rotation_puts(), [])
                self.assertEqual(set(self.lab.containers), {"zoomiso-a", "operator-core"})  # cleaned up

    def test_stale_revision_refuses_without_writing(self):
        self.lab.faults.add("revision_bump")
        code, _, _ = self.run_gate()
        self.assertEqual(code, 1)
        self.assertIn("revision changed since baseline", self.detail())
        self.assertEqual(self.rotation_puts(), [])

    def test_without_confirmation_it_plans_and_never_writes(self):
        code, _, _ = self.run_gate(confirm=False)
        self.assertEqual(code, 1)
        self.assertIn("rotation-rebind: planned only", self.detail())
        self.assertEqual(self.lab.puts, [])
        self.assertIn("replacement_discovered", self.verdict()["marks"])  # a full rehearsal up to the write
        self.assertTrue(pathlib.Path(self.evidence, "replacement-plan.json").is_file())
        self.assertTrue(pathlib.Path(self.evidence, "rotation-rebind-preflight.json").is_file())

    def test_partial_failure_rolls_back_and_keeps_evidence(self):
        self.lab.faults.add("apply_fail")
        code, _, _ = self.run_gate()
        verdict = self.verdict()
        self.assertEqual(code, 1)
        self.assertIn("concurrent Core revision change", self.detail())
        rollback = verdict["facts"]["rotation-rebind_rollback"]
        self.assertEqual(rollback["bindings_after"][SEL_SRC], {"flow_id": OLD_V, "audio_flow_id": OLD_A})
        self.assertEqual(self.lab.puts[-1], (SEL_SRC, {"flow_id": OLD_V, "audio_flow_id": OLD_A}))
        self.assertTrue(pathlib.Path(self.evidence, "samples.jsonl").is_file())

    def test_operator_never_acting_times_out(self):
        self.lab.scenario = "never"
        code, _, _ = self.run_gate()
        self.assertEqual(code, 1)
        self.assertIn("awaiting_old_pair_inactive", self.detail())
        self.assertEqual(set(self.lab.containers), {"zoomiso-a", "operator-core"})

    def test_replacement_that_never_appears_times_out(self):
        self.lab.scenario = "never_returns"
        code, _, _ = self.run_gate()
        self.assertEqual(code, 1)
        self.assertIn("awaiting_replacement", self.detail())

    def test_signal_preserves_evidence_and_cleans_up(self):
        holder = {}
        original_init = gate.RotationObserver.__init__

        def capture(self_, *a, **k):
            original_init(self_, *a, **k)
            holder["o"] = self_
        gate.RotationObserver.__init__ = capture
        self.addCleanup(setattr, gate.RotationObserver, "__init__", original_init)
        original = self.lab.prompt

        def stop(text):
            original(text)
            holder["o"]._on_signal()
        self.lab.prompt = stop
        code, _, _ = self.run_gate()
        self.assertEqual(code, 1)
        self.assertIn("Interrupted", self.detail())
        self.assertEqual(set(self.lab.containers), {"zoomiso-a", "operator-core"})
        self.assertTrue(pathlib.Path(self.evidence, "events.json").is_file())

    def test_cleanup_is_exact(self):
        self.run_gate()
        self.assertEqual(set(self.lab.containers), {"zoomiso-a", "operator-core"})
        plan = json.loads(pathlib.Path(self.evidence, "plan.json").read_text())
        self.assertEqual(sorted(self.lab.removed_flows), sorted(plan["candidate_outputs"].values()))
        for command in self.docker.log:
            self.assertNotIn("prune", command)
            if command[1] in ("rm", "stop", "exec", "restart"):
                self.assertTrue(command[-1].startswith(RUN + "-"), command)

    def test_public_summary_is_redacted(self):
        self.run_gate()
        text = pathlib.Path(self.evidence, "public-summary.json").read_text()
        for secret in (OLD_V, OLD_A, NEW_V, NEW_A, CONTROL, SEL_SRC, CTL_SRC, WRITER["id"], WRITER["name"],
                       GROUP, self.evidence, EASY, OPERATOR):
            self.assertNotIn(secret, text)
        self.assertEqual(json.loads(text)["verdict"], "PASS")


class UnitTest(unittest.TestCase):
    a = SimpleNamespace(selected_source=SEL_SRC, selected_flow=OLD_V, control_source=CTL_SRC, control_flow=CONTROL,
                        writer_container=WRITER["name"], group=GROUP, video_role=VROLE, audio_role=AROLE,
                        audio_channels=2)

    def test_old_pair_discovery_requires_one_writer(self):
        flows = [flow(OLD_V, "video", VROLE), flow(OLD_A, "audio", AROLE, writer={"id": "x", "name": WRITER["name"]})]
        with self.assertRaises(obs.Refused):
            gate.discover_old_pair(flows, self.a, gate.candidate_snapshot(self.a))

    def test_undeterminable_writer_state_is_refused(self):
        flows = [flow(OLD_V, "video", VROLE), flow(OLD_A, "audio", AROLE)]
        flows[0]["active"] = None
        with self.assertRaises(obs.GateFailure):
            gate.check_inventory(flows, self.a)

    def test_labels_are_never_used(self):
        flows = [flow(OLD_V, "video", VROLE, group="other"), flow(OLD_A, "audio", AROLE, group="other")]
        for item in flows:
            item["label"] = GROUP
        with self.assertRaises(obs.Refused):
            gate.discover_old_pair(flows, self.a, gate.candidate_snapshot(self.a))


if __name__ == "__main__":
    unittest.main()
