import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import mxl_direct as mxl  # noqa: E402
import mxl_index_mixer_gate as gate  # noqa: E402

TOOLS = str(pathlib.Path(__file__).parent)
RUN = "mxl-gate-test-0001"
FLOWS = {
    "a": "00000000-0000-4000-8000-00000000000a",
    "b": "00000000-0000-4000-8000-00000000000b",
    "output": "00000000-0000-4000-8000-0000000000c0",
}
READ_ONLY = {"version", "inspect", "logs", "stats"}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 0.01)


class World:
    """A docker daemon with one simulated index mixer. Faults are switches."""

    def __init__(self, clock):
        self.clock = clock
        self.containers = {}
        self.volumes = {}
        self.images = {"mxl-lab:test"}
        self.foreign = {}  # name -> label of a resource this run did not create
        self.a_can_join = True
        self.a2_creates = True
        self.crash_on = None  # container role whose start kills the mixer
        self.b_stalls_in = None  # phase name driven by the gate
        self.restart_on_probe = None
        self.probes = 0
        self.control = False
        self.ch = {"1": self.channel(False), "2": self.channel(True)}
        self.head = 480 * 1000
        self.mixer_up_after = 1  # the first probe fails: control server still starting

    @staticmethod
    def channel(online):
        return {"online": online, "stalled": False, "opens": 0, "closes": 0, "on_time": 0, "late": 0,
                "skipped": 0, "recoveries": 0, "last_open_error": None,
                "skipped_reasons": {"missing": 0, "overrun": 0, "torn": 0, "invalid": 0, "offline": 0, "error": 0}}

    def a_running(self):
        return any(self.containers.get(f"{RUN}-writer-{n}", {}).get("running") for n in ("a1", "a2"))

    def __call__(self, command, **_):
        self.clock.now += 0.05
        args = command[1:]
        verb = args[0]
        out, err, status = "", "", 0
        if verb == "version":
            out = "27.0.0"
        elif verb == "image":
            status = 0 if args[2] in self.images else 1
        elif verb == "volume":
            name = args[-1]
            if args[1] == "inspect":
                if "--format" in args:
                    label = self.volumes.get(name) or self.foreign.get(name)
                    status, out = (0, label) if label is not None else (1, "")
                else:
                    status = 0 if name in self.volumes or name in self.foreign else 1
            elif args[1] == "create":
                self.volumes[name] = args[args.index("--label") + 1].split("=", 1)[1]
                out = name
            elif args[1] == "rm":
                status = 0 if self.volumes.pop(name, None) is not None else 1
        elif verb == "container":
            name = args[-1]
            box = self.containers.get(name)
            if box is None and name in self.foreign:
                box = {"label": self.foreign[name], "running": True, "started": "x", "restarts": 0, "exit": 0}
            if box is None:
                status, err = 1, f"No such container: {name}"
            elif "--format" in args:
                out = box["label"]
            else:
                out = json.dumps([{"State": {"Running": box["running"], "Status": "running" if box["running"] else "exited",
                                             "ExitCode": box["exit"], "StartedAt": box["started"]},
                                   "RestartCount": box["restarts"], "Config": {"Labels": {gate.LABEL: box["label"]}}}])
        elif verb == "run":
            name = args[args.index("--name") + 1]
            label = args[args.index("--label") + 1].split("=", 1)[1]
            log = ""
            if "tone-writer" in args:
                created = not (name.endswith("a2") and not self.a2_creates)
                log = f"tone_writer=ready flow={args[args.index('--flow') + 1]} created={created} ring=19456\n"
            self.containers[name] = {"label": label, "running": True, "started": f"t{self.clock.now}",
                                     "restarts": 0, "exit": 0, "log": log, "command": args}
            if self.crash_on and name.endswith(self.crash_on):
                mixer = self.containers[f"{RUN}-mixer"]
                mixer.update(running=False, exit=139)
            out = "c0ffee"
        elif verb == "stop":
            box = self.containers[args[-1]]
            box["running"] = False
            box["log"] += "tone_writer=stopped blocks=10 errors=0\n"
            if "--remove-flow-on-exit" in box["command"]:
                box["log"] += f"tone_writer=removed flow={FLOWS['a']}\n"
        elif verb == "rm":
            status = 0 if self.containers.pop(args[-1], None) is not None else 1
        elif verb == "logs":
            box = self.containers.get(args[-1])
            status, err = (0, box["log"]) if box else (1, "no such container")
        elif verb == "stats":
            out = json.dumps({"CPUPerc": "3.50%", "MemUsage": "21.5MiB / 62.7GiB"}) + "\n"
        elif verb == "exec":
            return self.probe(args)
        else:
            raise AssertionError(f"unexpected docker command {command}")
        return SimpleNamespace(returncode=status, stdout=out, stderr=err)

    def probe(self, args):
        mixer = self.containers.get(args[1])
        if not mixer or not mixer["running"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="container is not running")
        self.probes += 1
        if self.probes <= self.mixer_up_after:
            return SimpleNamespace(returncode=1, stdout="", stderr="Connection refused")
        if "--control" in args:
            self.control = True
        if self.restart_on_probe and self.probes == self.restart_on_probe:
            mixer["restarts"] = 1
        a = self.ch["1"]
        want = self.a_running() and self.a_can_join
        if want and not a["online"]:
            a.update(online=True, opens=a["opens"] + 1, last_open_error=None)
        elif not self.a_running() and a["online"]:
            a.update(online=False, closes=a["closes"] + 1, last_open_error="flow left the domain")
        self.head += 4800
        if self.control:
            if a["online"]:
                a["on_time"] += 10
            else:
                a["skipped"] += 10
                a["skipped_reasons"]["offline"] += 10
            self.ch["2"]["on_time"] += 10
            if self.b_stalls_in and self.b_stalls_in():
                self.ch["2"]["stalled"] = True
        sample = (0.25 + (0.125 if a["online"] else 0.0)) if self.control else 0.0
        state = {"output_head_index": self.head, "output_progress_age_seconds": 0.01, "blocks_written": self.head // 480,
                 "blocks_degraded": 0, "channels": json.loads(json.dumps(self.ch))}
        return SimpleNamespace(returncode=0, stdout=json.dumps({"state": state, "output_sample": sample}), stderr="")


def plan_args(evidence, *extra):
    return ["plan", "--image", "mxl-lab:test", "--run-id", RUN, "--evidence-dir", evidence,
            "--hold-seconds", "20", *extra]


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.evidence = os.path.join(self.tmp.name, "evidence")
        self.clock = Clock()
        self.world = World(self.clock)
        self.docker = gate.Docker(run=self.world)

    def main(self, *extra, apply=True):
        args = plan_args(self.evidence, *extra) + (["--apply"] if apply else [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = gate.main(args, docker=self.docker, clock=self.clock, sleep=self.clock.sleep)
        return code, out.getvalue()

    def verdict(self):
        return json.loads(pathlib.Path(self.evidence, "verdict.json").read_text())

    def verbs(self):
        return [command[1] for command in self.docker.log]


class PlanTest(Harness):
    def plan(self, **overrides):
        return gate.Plan(run_id=RUN, image="mxl-lab:test", tools_dir=TOOLS, library="libmxl.so",
                         evidence_dir=self.evidence, flows=FLOWS, **overrides)

    def test_every_container_is_isolated_labelled_and_never_restarted(self):
        plan = self.plan()
        for command in (plan.mixer(), plan.writer("writer_b"), plan.writer("writer_a1"), plan.writer("writer_a2")):
            self.assertEqual(command[:3], ["docker", "run", "-d"])
            self.assertIn("--network", command)
            self.assertEqual(command[command.index("--network") + 1], "none")
            self.assertEqual(command[command.index("--restart") + 1], "no")
            self.assertIn(f"{gate.LABEL}={RUN}", command)
            self.assertIn(f"{RUN}-domain:/mxl-domain", command)
            self.assertIn(f"{TOOLS}:/gate-tools:ro", command)
            self.assertNotIn("-p", command)
            self.assertNotIn("--publish", command)
            self.assertNotIn("--privileged", command)

    def test_mixer_reads_absent_a_and_b_and_writes_a_new_output(self):
        command = self.plan().mixer()
        inputs = [command[i + 1] for i, arg in enumerate(command) if arg == "--input"]
        self.assertEqual(inputs, [FLOWS["a"], FLOWS["b"]])
        self.assertEqual(command[command.index("--output") + 1], FLOWS["output"])
        self.assertEqual(command[command.index("--domain") + 1], "/mxl-domain")
        self.assertEqual(command[command.index("--reopen-interval-ms") + 1], "1000.0")

    def test_only_the_first_a_writer_removes_its_flow_and_both_use_one_id(self):
        plan = self.plan()
        a1, a2, b = plan.writer("writer_a1"), plan.writer("writer_a2"), plan.writer("writer_b")
        self.assertIn("--remove-flow-on-exit", a1)
        self.assertNotIn("--remove-flow-on-exit", a2)
        self.assertNotIn("--remove-flow-on-exit", b)
        flow = lambda c: c[c.index("--flow") + 1]  # noqa: E731
        self.assertEqual((flow(a1), flow(a2), flow(b)), (FLOWS["a"], FLOWS["a"], FLOWS["b"]))

    def test_volume_is_a_new_labelled_tmpfs(self):
        command = self.plan().volume_create()
        self.assertEqual(command[-1], f"{RUN}-domain")
        self.assertIn("type=tmpfs", command)
        self.assertIn(f"{gate.LABEL}={RUN}", command)

    def test_runs_are_unique(self):
        first, second = gate.new_run_id(), gate.new_run_id()
        self.assertNotEqual(first, second)
        self.assertRegex(first, gate.RUN_ID)
        a = gate.Plan(run_id=first, image="i", tools_dir=TOOLS, library="l", evidence_dir="x")
        b = gate.Plan(run_id=second, image="i", tools_dir=TOOLS, library="l", evidence_dir="x")
        self.assertFalse(set(a.flows.values()) & set(b.flows.values()))
        self.assertFalse(set(a.names.values()) & set(b.names.values()))
        for flow in a.flows.values():
            self.assertRegex(flow, gate.FLOW_ID)

    def test_unsafe_plans_are_refused(self):
        for run_id in ("mxl-core-operator", "mxl-gate-UPPER", "mxl-gate-x", "mxl-gate-a b"):
            with self.assertRaises(gate.Refused):
                gate.Plan(run_id=run_id, image="i", tools_dir=TOOLS, library="l", evidence_dir="x")
        with self.assertRaises(gate.Refused):
            self.plan(hold_seconds=0)
        with self.assertRaises(gate.Refused):
            gate.Plan(run_id=RUN, image="i", tools_dir=TOOLS, library="l", evidence_dir="x",
                      flows={"a": FLOWS["a"], "b": FLOWS["a"], "output": FLOWS["output"]})
        with self.assertRaises(gate.Refused):
            gate.Plan(run_id=RUN, image="i", tools_dir=TOOLS, library="l", evidence_dir="x",
                      flows={**FLOWS, "a": "/srv/mxl/domain"})

    def test_total_duration_is_bounded(self):
        plan = self.plan(hold_seconds=600)
        bounds = plan.as_dict()["bounds_seconds"]
        self.assertGreater(bounds["max_total"], 600)
        self.assertLess(bounds["max_total"], 1200)


class DryRunTest(Harness):
    def test_dry_run_prints_the_plan_and_only_reads(self):
        code, out = self.main(apply=False)
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["mode"], "dry_run")
        self.assertEqual(report["plan"]["containers"]["mixer"], f"{RUN}-mixer")
        self.assertEqual(report["plan"]["volume"], f"{RUN}-domain")
        self.assertTrue(all(command[1] in READ_ONLY or command[1:3] in (["image", "inspect"], ["volume", "inspect"],
                                                                          ["container", "inspect"])
                            for command in self.docker.log), self.docker.log)
        self.assertEqual((self.world.containers, self.world.volumes), ({}, {}))
        self.assertFalse(os.path.exists(self.evidence))

    def test_collisions_fail_closed_before_any_change(self):
        cases = [
            lambda: self.world.foreign.update({f"{RUN}-mixer": "someone-else"}),
            lambda: self.world.foreign.update({f"{RUN}-domain": "someone-else"}),
            lambda: self.world.images.clear(),
            lambda: os.makedirs(self.evidence),
        ]
        for arrange in cases:
            with self.subTest(arrange=arrange):
                self.setUp()
                arrange()
                code, _ = self.main()
                self.assertEqual(code, 2)
                self.assertEqual(self.world.containers, {})
                self.assertEqual(self.world.volumes, {})
                self.assertFalse(any(verb in ("run", "create", "rm", "stop") for verb in self.verbs()))

    def test_missing_docker_is_a_refusal_not_a_crash(self):
        def absent(command, **_):
            raise FileNotFoundError(2, "No such file or directory", command[0])

        self.docker = gate.Docker(run=absent)
        code, _ = self.main()
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(self.evidence))

    def test_tools_dir_must_hold_the_mixer(self):
        code, _ = self.main("--tools-dir", self.tmp.name, apply=False)
        self.assertEqual(code, 2)


class GateRunTest(Harness):
    def test_full_sequence_passes_and_cleans_up_exactly(self):
        code, _ = self.main()
        verdict = self.verdict()
        self.assertEqual(code, 0, json.dumps(verdict["checks"], indent=1))
        self.assertEqual(verdict["verdict"], "PASS")
        phases = [t["phase"] for t in verdict["a_transitions"]]
        self.assertEqual(phases[0], "a_absent")
        self.assertIn("a_joined", phases)
        self.assertIn("a_recreated", phases)
        final = verdict["final_channels"]["1"]
        self.assertEqual((final["online"], final["opens"], final["closes"]), (True, 2, 1))
        self.assertEqual(verdict["resources"]["mixer_rss_mib"], {"min": 21.5, "max": 21.5})
        self.assertEqual(verdict["resources"]["mixer_cpu_percent"]["max"], 3.5)
        self.assertEqual(sorted(verdict["cleanup"]["removed"]),
                         sorted([f"{RUN}-mixer", f"{RUN}-writer-b", f"{RUN}-writer-a2", f"{RUN}-domain"]))
        self.assertEqual(verdict["cleanup"]["not_removed"], [])
        self.assertEqual((self.world.containers, self.world.volumes), ({}, {}))
        files = set(os.listdir(self.evidence))
        self.assertTrue({"samples.jsonl", "events.json", "plan.json", "verdict.json", "cleanup.json",
                         f"{RUN}-mixer.log", f"{RUN}-writer-b.log", f"{RUN}-writer-a2.log"} <= files)

    def test_no_broad_or_pattern_cleanup_ever(self):
        self.main()
        for command in self.docker.log:
            self.assertNotIn("prune", command)
            self.assertFalse(any("*" in part or part.startswith("--filter") for part in command), command)
        removals = [c[-1] for c in self.docker.log if c[1] == "rm" or c[1:3] == ["volume", "rm"]]
        self.assertTrue(all(name.startswith(RUN + "-") for name in removals))

    def test_a_that_never_joins_fails_within_its_bound(self):
        self.world.a_can_join = False
        started = self.clock.now
        code, _ = self.main()
        verdict = self.verdict()
        self.assertEqual(code, 1)
        self.assertFalse(verdict["checks"]["sequence_completed"]["pass"])
        self.assertIn("a_joined", verdict["checks"]["sequence_completed"]["detail"])
        self.assertLess(self.clock.now - started, 120)
        self.assertEqual(self.world.containers, {})

    def test_mixer_crash_on_recreate_fails_and_still_cleans_up(self):
        self.world.crash_on = "writer-a2"
        code, _ = self.main()
        verdict = self.verdict()
        self.assertEqual(code, 1)
        self.assertIn("exited", verdict["checks"]["sequence_completed"]["detail"])
        self.assertIn(139, verdict["checks"]["mixer_never_restarted"]["detail"]["exit_codes"])
        self.assertEqual((self.world.containers, self.world.volumes), ({}, {}))

    def test_reopening_an_existing_flow_does_not_count_as_recreation(self):
        self.world.a2_creates = False
        code, _ = self.main()
        verdict = self.verdict()
        self.assertEqual(code, 1)
        self.assertIn("re-creation was not exercised", verdict["checks"]["sequence_completed"]["detail"])
        self.assertFalse(verdict["checks"]["a_recreated_closed_and_reopened"]["pass"])

    def test_b_stalling_fails_the_gate(self):
        self.world.b_stalls_in = lambda: self.world.ch["1"]["closes"] > 0
        code, _ = self.main()
        verdict = self.verdict()
        self.assertEqual(code, 1)
        self.assertFalse(verdict["checks"]["b_never_stalled_or_skipped"]["pass"])

    def test_mixer_restart_fails_the_gate(self):
        self.world.restart_on_probe = 6
        code, _ = self.main()
        self.assertEqual(code, 1)
        self.assertIn("restarted", self.verdict()["checks"]["sequence_completed"]["detail"])

    def test_cleanup_leaves_a_resource_whose_label_is_not_ours(self):
        self.world.crash_on = "writer-a2"
        original = self.world.__call__

        def relabel(command, **kwargs):
            result = original(command, **kwargs)
            if command[1] == "run" and command[command.index("--name") + 1] == f"{RUN}-writer-b":
                self.world.containers[f"{RUN}-writer-b"]["label"] = "someone-else"
            return result

        self.docker = gate.Docker(run=relabel)
        self.main()
        cleanup = self.verdict()["cleanup"]
        self.assertIn(f"{RUN}-writer-b", self.world.containers)
        self.assertTrue(any(entry.startswith(f"{RUN}-writer-b:") for entry in cleanup["not_removed"]))
        self.assertNotIn(f"{RUN}-writer-b", cleanup["removed"])


class EvaluateTest(unittest.TestCase):
    def sample(self, t, phase, head, a_online, sample, opens=0, closes=0, offline=0, a_on=0, b_on=0, **extra):
        reasons = {"offline": offline}
        return {"t": t, "phase": phase, "errors": [], "output_head": head, "output_sample": sample,
                "container": {"running": True, "started_at": "s", "restart_count": 0, "exit_code": 0},
                "channels": {"1": {"online": a_online, "stalled": False, "opens": opens, "closes": closes,
                                   "on_time": a_on, "late": 0, "skipped": offline, "skipped_reasons": reasons},
                             "2": {"online": True, "stalled": extra.get("b_stalled", False), "opens": 1, "closes": 0,
                                   "on_time": b_on, "late": 0, "skipped": extra.get("b_skipped", 0),
                                   "skipped_reasons": {}}},
                "cpu_percent": 2.0, "rss_mib": 20.0}

    def good(self):
        s = self.sample
        return [
            s(1, "a_absent", 10, False, 0.0, offline=0, b_on=0),
            s(2, "a_absent", 20, False, 0.25, offline=5, b_on=5),
            s(3, "a_absent", 30, False, 0.25, offline=10, b_on=10),
            s(4, "a_joined", 40, True, 0.375, opens=1, offline=10, a_on=5, b_on=15),
            s(5, "a_gone", 50, False, 0.25, opens=1, closes=1, offline=12, a_on=5, b_on=20),
            s(6, "a_recreated", 60, True, 0.375, opens=2, closes=1, offline=14, a_on=8, b_on=25),
            s(7, "hold", 70, True, 0.375, opens=2, closes=1, offline=14, a_on=12, b_on=30),
            s(8, "hold", 80, True, 0.375, opens=2, closes=1, offline=14, a_on=16, b_on=35),
        ]

    def test_good_run_passes(self):
        verdict = gate.evaluate(self.good(), True)
        self.assertEqual(verdict["verdict"], "PASS", verdict["checks"])
        self.assertEqual([t["phase"] for t in verdict["a_transitions"]],
                         ["a_absent", "a_joined", "a_gone", "a_recreated", "hold"])

    def test_each_invariant_fails_on_its_own(self):
        def broken(mutate):
            samples = self.good()
            mutate(samples)
            return gate.evaluate(samples, True)

        cases = {
            "output_advanced_every_sample": lambda s: s[6].update(output_head=60),
            "mixer_never_restarted": lambda s: s[4]["container"].update(started_at="later"),
            "a_absent_offline_and_b_airs": lambda s: s[2]["channels"]["1"].update(online=True),
            "a_joined_without_restart": lambda s: s[3].update(output_sample=0.25),
            "a_recreated_closed_and_reopened": lambda s: s[5]["channels"]["1"].update(closes=0),
            "hold_both_contribute": lambda s: s[7]["channels"]["1"].update(on_time=12),
            "b_never_stalled_or_skipped": lambda s: s[6]["channels"]["2"].update(skipped=3),
            "sampling_clean": lambda s: s[6]["errors"].append("probe: timeout"),
        }
        for check, mutate in cases.items():
            with self.subTest(check=check):
                verdict = broken(mutate)
                self.assertEqual(verdict["verdict"], "FAIL")
                self.assertFalse(verdict["checks"][check]["pass"])

    def test_recreation_must_be_proven_by_the_writer(self):
        self.assertFalse(gate.evaluate(self.good(), False)["checks"]["a_recreated_closed_and_reopened"]["pass"])

    def test_startup_probe_errors_are_not_faults(self):
        samples = [{"t": 0, "phase": "startup", "errors": ["probe: refused"],
                    "container": {"running": True, "started_at": "s", "restart_count": 0, "exit_code": 0}}]
        self.assertEqual(gate.evaluate(samples + self.good(), True)["verdict"], "PASS")

    def test_resource_units(self):
        self.assertEqual(gate._mib("1.5GiB / 62GiB"), 1536.0)
        self.assertEqual(gate._mib("512KiB / 1GiB"), 0.5)
        self.assertIsNone(gate._mib("--"))
        self.assertEqual(gate._percent("12.5%"), 12.5)
        self.assertIsNone(gate._percent(""))


class InContainerRolesTest(unittest.TestCase):
    def test_tone_writer_removes_only_its_own_flow_on_exit(self):
        domain = tempfile.TemporaryDirectory()
        self.addCleanup(domain.cleanup)
        for flow in (FLOWS["a"], FLOWS["b"]):
            os.makedirs(os.path.join(domain.name, f"{flow}.mxl-flow"))

        class Api:
            def mxlCreateInstance(self, domain, options):
                return 1

            def mxlDestroyInstance(self, instance):
                return 0

            def mxlCreateFlowWriter(self, instance, definition, options, handle, info, created):
                handle._obj.value = 1
                info._obj.continuous.bufferLength = 19_456
                created._obj.value = True
                return mxl.MXL_STATUS_OK

            def mxlReleaseFlowWriter(self, instance, handle):
                return mxl.MXL_STATUS_OK

        original_load, original_running = mxl.load_library, gate.RUNNING
        mxl.load_library = lambda name: Api()
        gate.RUNNING = False  # stop before the first block
        self.addCleanup(setattr, mxl, "load_library", original_load)
        self.addCleanup(setattr, gate, "RUNNING", original_running)
        args = SimpleNamespace(flow=FLOWS["a"], domain=domain.name, value=0.125, library="x",
                               remove_flow_on_exit=True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(gate.tone_writer(args), 0)
        self.assertIn("created=True", err.getvalue())
        self.assertIn("tone_writer=removed", err.getvalue())
        self.assertEqual(os.listdir(domain.name), [f"{FLOWS['b']}.mxl-flow"])

    def test_tone_writer_refuses_a_non_uuid_flow(self):
        args = SimpleNamespace(flow="../escape", domain="/nowhere", value=0.1, library="x", remove_flow_on_exit=True)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(gate.tone_writer(args), 2)

    def test_probe_applies_control_then_reads_state(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"channels": {}, "output_head_index": 9}).encode())

            def log_message(self, *_):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        domain = tempfile.TemporaryDirectory()
        self.addCleanup(domain.cleanup)
        args = SimpleNamespace(port=server.server_address[1], domain=domain.name, output=FLOWS["output"],
                               library="never-loaded", control=['{"mode":"manual"}', '{"channel":1,"on":true}'])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gate.probe(args), 0)
        self.assertEqual(received, [{"mode": "manual"}, {"channel": 1, "on": True}])
        result = json.loads(out.getvalue())
        self.assertEqual((result["state"]["output_head_index"], result["output_sample"]), (9, None))


if __name__ == "__main__":
    unittest.main()
