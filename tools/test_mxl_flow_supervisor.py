import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import mxl_flow_supervisor as sup  # noqa: E402
import mxl_rebind_apply as executor  # noqa: E402
import mxl_discovery_planner as planner  # noqa: E402

CORE = "http://127.0.0.1:18085"
EASY = "http://easy.invalid:9700"
TOKEN = "tok-SECRET-do-not-log"
WRITER = {"id": "c0ffee0000000001", "name": "zoomiso"}


def uid(n):
    return f"00000000-0000-4000-8000-{n:012x}"


V4, A4, V4B, A4B, V4C, A4C = uid(0x4a0), uid(0x4b0), uid(0x4a1), uid(0x4b1), uid(0x4a2), uid(0x4b2)
V2, A2 = uid(0x2a0), uid(0x2b0)


def flow(fid, kind, group, active=True, writer=WRITER, width=1920):
    definition = ({"frame_width": width, "frame_height": 1080, "grain_rate": {"numerator": 30, "denominator": 1},
                   "media_type": "video/v210"} if kind == "video"
                  else {"sample_rate": {"numerator": 48000}, "channel_count": 2, "media_type": "audio/float32"})
    return {"id": fid, "format": kind, "group": group, "role": kind, "def": definition, "active": active,
            "status": "active" if active else ("inactive" if active is False else "unknown"),
            "writerContainer": None if writer is None else dict(writer)}


def pair(v, a, group, **kw):
    return [flow(v, "video", group, **kw), flow(a, "audio", group, **kw)]


class Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeCore:
    """Loopback Core seen through ``mxl_rebind_apply._request_json``."""

    def __init__(self):
        self.revision = 1
        self.program, self.preview = "cam-2", "cam-4"
        fmt = {"width": 1920, "height": 1080, "rate_numerator": 30, "rate_denominator": 1, "encoding": "video/v210"}
        self.sources = {"cam-4": {"id": "cam-4", "flow_id": V4, "audio_flow_id": A4, "format": fmt},
                        "cam-2": {"id": "cam-2", "flow_id": V2, "audio_flow_id": A2, "format": fmt}}
        self.puts = []
        self.faults = set()

    def __call__(self, base, path, method="GET", body=None, timeout=10.0):
        assert base == CORE, base
        if method == "PUT":
            source = path.rsplit("/", 1)[-1]
            self.puts.append((source, dict(body)))
            self.sources[source].update(body)
            self.revision += 2 if "concurrent_put" in self.faults and body["flow_id"] != V4 else 1
            return {"changed": True, "revision": self.revision}
        if path == executor.STATE_PATH:
            return {"revision": self.revision, "program": self.program, "preview": self.preview}
        if path == executor.SOURCES_PATH:
            return [dict(s) for s in self.sources.values()]
        raise AssertionError(path)


class Lab:
    def __init__(self):
        self.flows = pair(V4, A4, "Output 4") + pair(V2, A2, "Output 2")
        self.fail = False

    def inventory(self):
        if self.fail:
            raise planner.PlanInputError("GET http://easy.invalid failed: URLError")
        return [dict(f) for f in self.flows]

    def rotate(self, v, a, group="Output 4", **kw):
        self.flows = [f for f in self.flows if f["group"] != group] \
            + pair(V4, A4, group, active=False) + pair(v, a, group, **kw)

    def drop(self, group="Output 4"):
        self.flows = [f for f in self.flows if f["group"] != group] + pair(V4, A4, group, active=False)


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.core = FakeCore()
        self.lab = Lab()
        self.clock = Clock()
        self.logs = []
        original = executor._request_json
        executor._request_json = self.core
        self.addCleanup(setattr, executor, "_request_json", original)
        os.environ["SUPERVISOR_TEST_TOKEN"] = TOKEN
        self.addCleanup(os.environ.pop, "SUPERVISOR_TEST_TOKEN", None)
        self.config_path = self.write_config()

    def write_config(self, **overrides):
        config = {
            "version": 1, "core_url": CORE,
            "easy_mxl": {"url": EASY, "domain": "lab", "token_env": "SUPERVISOR_TEST_TOKEN"},
            "interval_s": 1, "dwell_s": 5, "max_backoff_s": 8,
            "journal": str(self.dir / "journal.json"), "status": str(self.dir / "status.json"),
            "policy": {"allow_program_rebind": False},
            "mapping": {"version": 1, "defaults": {"video": {"width": 1920, "height": 1080, "rate": "30/1",
                                                             "media_type": "video/v210"}},
                        "sources": [
                            {"id": "cam-4", "match": {"writer_container": "zoomiso", "group": "Output 4",
                                                      "video_role": "video", "audio_role": "audio"}},
                            {"id": "cam-2", "match": {"writer_container": "zoomiso", "group": "Output 2",
                                                      "video_role": "video", "audio_role": "audio"}}]},
        }
        config.update(overrides)
        path = self.dir / "config.json"
        path.write_text(json.dumps(config))
        return str(path)

    def supervisor(self, apply=False):
        config = sup.load_config(self.config_path)
        return sup.Supervisor(config, apply=apply, inventory_get=self.lab.inventory, clock=self.clock,
                              sleep=self.clock.sleep, log=self.logs.append)

    def ticks(self, s, n, step=1.0):
        for _ in range(n):
            s.tick()
            self.clock.now += step

    def state(self, s, source="cam-4"):
        return s.sources[source].state


class LifecycleTest(Harness):
    def test_stable_pair_is_a_noop(self):
        s = self.supervisor(apply=True)
        self.ticks(s, 3)
        self.assertEqual((self.state(s), self.state(s, "cam-2")), ("steady", "steady"))
        self.assertEqual(self.core.puts, [])

    def test_replacement_dwells_then_plan_only(self):
        s = self.supervisor()
        self.ticks(s, 2)
        self.lab.rotate(V4B, A4B)
        self.ticks(s, 3)
        self.assertEqual(self.state(s), "dwelling")
        self.assertGreater(s.status()["sources"]["cam-4"]["pending_dwell_s"], 0)
        self.ticks(s, 4)
        self.assertEqual(self.state(s), "planned")
        self.assertEqual(s.sources["cam-4"].counters["plans"], 1)
        self.ticks(s, 5)
        self.assertEqual(s.sources["cam-4"].counters["plans"], 1)  # planned stays planned, no repeat
        self.assertEqual(self.core.puts, [])

    def test_confirmed_apply_calls_the_executor_exactly_once(self):
        calls = []
        original = executor.execute

        def counting(*args, **kwargs):
            calls.append(args[2])
            return original(*args, **kwargs)
        executor.execute = counting
        self.addCleanup(setattr, executor, "execute", original)
        s = self.supervisor(apply=True)
        self.ticks(s, 2)
        self.lab.rotate(V4B, A4B)
        self.ticks(s, 10)
        self.assertEqual(calls, [1])
        self.assertEqual(self.core.puts, [("cam-4", {"flow_id": V4B, "audio_flow_id": A4B})])
        self.assertEqual(self.state(s), "steady")
        self.assertEqual(s.status()["sources"]["cam-4"]["last_accepted_rebind"]["revision"], 2)
        states = [e["state"] for e in s.journal.entries]
        self.assertEqual(states, ["applying", "applied"])
        self.assertEqual(self.state(s, "cam-2"), "steady")

    def test_pair_completion_is_not_an_old_pair_refusal(self):
        self.core.sources["cam-4"]["audio_flow_id"] = None  # a config that could carry video only
        s = self.supervisor(apply=True)
        self.ticks(s, 8)
        self.assertEqual(self.core.puts, [("cam-4", {"flow_id": V4, "audio_flow_id": A4})])

    def test_flap_resets_dwell(self):
        s = self.supervisor(apply=True)
        self.ticks(s, 2)
        self.lab.rotate(V4B, A4B)
        self.ticks(s, 3)
        self.lab.rotate(V4C, A4C)  # a different pair replaces the candidate before the dwell ends
        self.ticks(s, 3)
        self.assertEqual(self.core.puts, [])
        self.assertEqual(s.sources["cam-4"].counters["flaps"], 1)
        self.ticks(s, 4)
        self.assertEqual(self.core.puts, [("cam-4", {"flow_id": V4C, "audio_flow_id": A4C})])

    def test_replacement_disappearing_during_dwell_is_a_flap(self):
        s = self.supervisor(apply=True)
        self.ticks(s, 1)
        self.lab.rotate(V4B, A4B)
        self.ticks(s, 2)
        self.lab.drop()
        self.ticks(s, 6)
        self.assertEqual((self.state(s), s.sources["cam-4"].counters["flaps"], self.core.puts), ("outage", 1, []))


class RefusalTest(Harness):
    def refused(self, arrange, reason_contains, source="cam-4"):
        s = self.supervisor(apply=True)
        self.ticks(s, 2)
        arrange()
        self.ticks(s, 10)
        self.assertEqual(self.state(s, source), "refused")
        self.assertIn(reason_contains, s.sources[source].refusal)
        self.assertEqual(self.core.puts, [])
        return s

    def test_ambiguous_replacement(self):
        self.refused(lambda: (self.lab.rotate(V4B, A4B), self.lab.flows.extend(pair(V4C, A4C, "Output 4"))),
                     "ambiguous")

    def test_unknown_writer_state(self):
        self.refused(lambda: self.lab.rotate(V4B, A4B, active=None), "unknown_writer_state")

    def test_missing_writer_attribution(self):
        self.refused(lambda: self.lab.rotate(V4B, A4B, writer=None), "missing_writer_attribution")

    def test_wrong_writer(self):
        self.refused(lambda: self.lab.rotate(V4B, A4B, writer={"id": "another-id", "name": "zoomiso"}),
                     "wrong_writer")

    def test_incompatible_media(self):
        self.refused(lambda: self.lab.rotate(V4B, A4B, width=1280), "incompatible")

    def test_ownership_collision(self):
        self.refused(lambda: self.lab.rotate(V2, A4B), "bound_to_other_source")

    def test_on_air_program_needs_policy(self):
        self.core.program, self.core.preview = "cam-4", "cam-2"
        self.refused(lambda: self.lab.rotate(V4B, A4B), "on_air_program_rebind_not_permitted")

    def test_on_air_refusal_holds_then_releases_when_cut_away(self):
        self.core.program, self.core.preview = "cam-4", "cam-2"
        s = self.supervisor(apply=True)
        self.ticks(s, 2)
        self.lab.rotate(V4B, A4B)
        self.ticks(s, 12)
        self.assertEqual((self.state(s), s.sources["cam-4"].counters["refusals"]), ("refused", 1))  # no cycling
        self.core.program, self.core.preview = "cam-2", "cam-4"  # the operator cuts away
        self.ticks(s, 2)
        self.assertEqual(self.core.puts, [("cam-4", {"flow_id": V4B, "audio_flow_id": A4B})])

    def test_failed_apply_is_not_retried_for_the_same_candidate(self):
        self.core.faults.add("concurrent_put")
        s = self.supervisor(apply=True)
        self.ticks(s, 2)
        self.lab.rotate(V4B, A4B)
        self.ticks(s, 30)
        self.assertEqual(len(self.core.puts), 2)  # one attempt and its rollback, never repeated
        self.assertEqual(self.state(s), "refused")

    def test_on_air_program_with_policy(self):
        self.core.program, self.core.preview = "cam-4", "cam-2"
        self.config_path = self.write_config(policy={"allow_program_rebind": True})
        s = self.supervisor(apply=True)
        self.ticks(s, 2)
        self.lab.rotate(V4B, A4B)
        self.ticks(s, 10)
        self.assertEqual(len(self.core.puts), 1)

    def test_one_broken_mapping_does_not_block_another(self):
        self.core.program, self.core.preview = "cam-4", "cam-2"  # rebind the off-air source
        s = self.supervisor(apply=True)
        self.ticks(s, 2)
        self.lab.rotate(V4B, A4B, active=None)  # cam-4 cannot be judged
        self.lab.rotate(uid(0x2a1), uid(0x2b1), group="Output 2")
        self.ticks(s, 10)
        self.assertEqual(self.state(s), "refused")
        self.assertEqual(self.core.puts, [("cam-2", {"flow_id": uid(0x2a1), "audio_flow_id": uid(0x2b1)})])


class ApplyFailureTest(Harness):
    def test_stale_revision_refuses_without_writing(self):
        original = executor.execute

        def concurrent(base, prepared, revision):
            self.core.revision += 1  # someone else changed Core after the plan was made
            return original(base, prepared, revision)
        executor.execute = concurrent
        self.addCleanup(setattr, executor, "execute", original)
        s = self.supervisor(apply=True)
        self.ticks(s, 2)
        self.lab.rotate(V4B, A4B)
        self.ticks(s, 8)
        self.assertEqual(self.core.puts, [])
        self.assertEqual((self.state(s), s.sources["cam-4"].refusal), ("refused", "apply_failed_rolled_back"))
        self.assertEqual(s.journal.entries[-1]["state"], "failed")

    def test_mid_apply_error_is_rolled_back_by_the_executor(self):
        self.core.faults.add("concurrent_put")
        s = self.supervisor(apply=True)
        self.ticks(s, 2)
        self.lab.rotate(V4B, A4B)
        self.ticks(s, 8)
        self.assertEqual(self.core.puts, [("cam-4", {"flow_id": V4B, "audio_flow_id": A4B}),
                                          ("cam-4", {"flow_id": V4, "audio_flow_id": A4})])
        self.assertEqual(executor._binding(self.core.sources["cam-4"]), {"flow_id": V4, "audio_flow_id": A4})
        self.assertEqual(s.journal.entries[-1]["state"], "failed")
        self.assertEqual(s.sources["cam-4"].counters["apply_failures"], 1)


class RestartTest(Harness):
    def seed(self, previous, desired, state="applying"):
        journal = sup.Journal(str(self.dir / "journal.json"), 200)
        journal.record(t=900.0, source="cam-4", state=state, previous=previous, desired=desired, revision=1,
                       result=None)

    def test_restart_before_apply_completed_does_not_replay(self):
        self.seed({"flow_id": V4, "audio_flow_id": A4}, {"flow_id": V4B, "audio_flow_id": A4B})
        self.lab.drop()
        s = self.supervisor(apply=True)
        self.ticks(s, 1)
        self.assertEqual(self.core.puts, [])
        self.assertIn("not_applied", s.journal.entries[-1]["result"])
        self.assertEqual(self.state(s), "outage")

    def test_restart_after_apply_is_recognised(self):
        self.core.sources["cam-4"].update(flow_id=V4B, audio_flow_id=A4B)
        self.lab.rotate(V4B, A4B)
        self.seed({"flow_id": V4, "audio_flow_id": A4}, {"flow_id": V4B, "audio_flow_id": A4B})
        s = self.supervisor(apply=True)
        self.ticks(s, 2)
        self.assertEqual(self.core.puts, [])
        self.assertEqual(self.state(s), "steady")
        self.assertEqual(s.journal.entries[-1]["state"], "applied")
        self.assertIsNotNone(s.status()["sources"]["cam-4"]["last_accepted_rebind"])

    def test_uncertain_journal_blocks_writes_until_cleared(self):
        self.core.sources["cam-4"].update(flow_id=V4C, audio_flow_id=A4C)  # neither previous nor desired
        self.seed({"flow_id": V4, "audio_flow_id": A4}, {"flow_id": V4B, "audio_flow_id": A4B})
        self.lab.rotate(V4B, A4B)
        s = self.supervisor(apply=True)
        self.ticks(s, 10)
        self.assertEqual(self.state(s), "uncertain")
        self.assertEqual(self.core.puts, [])
        s2 = self.supervisor(apply=True)  # a second restart stays uncertain
        self.ticks(s2, 10)
        self.assertEqual((self.state(s2), self.core.puts), ("uncertain", []))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sup.main(["clear", "--config", self.config_path, "--source", "cam-4"]), 0)
        s3 = self.supervisor(apply=True)
        self.ticks(s3, 2)
        self.assertNotEqual(self.state(s3), "uncertain")

    def test_journal_is_bounded_and_keeps_each_newest_entry(self):
        journal = sup.Journal(str(self.dir / "j.json"), 5)
        journal.record(t=1, source="cam-2", state="applied", previous=None, desired=None, revision=1, result="x")
        for i in range(20):
            journal.record(t=2 + i, source="cam-4", state="applied", previous=None, desired=None, revision=i,
                           result="x")
        self.assertEqual(len(journal.entries), 5)
        self.assertEqual(journal.last("cam-2")["t"], 1)
        self.assertEqual(journal.last("cam-4")["revision"], 19)


class LoopTest(Harness):
    def test_backoff_is_bounded_and_resets(self):
        s = self.supervisor()
        self.lab.fail = True
        delays = []
        for _ in range(6):
            s.tick()
            delays.append(s.delay())
        self.assertEqual(delays, [2, 4, 8, 8, 8, 8])
        self.assertEqual(s.status()["state"], "backoff")
        self.assertEqual(s.status()["last_error"], "PlanInputError")
        self.lab.fail = False
        s.tick()
        self.assertEqual((s.delay(), s.status()["state"]), (1.0, "running"))

    def test_clean_shutdown_writes_a_stopped_status(self):
        s = self.supervisor()
        original = self.clock.sleep

        def stop_soon(seconds):
            original(seconds)
            if self.clock.now > 1_003:
                s.stop = True
        s.sleep = stop_soon
        self.assertEqual(s.run(), 0)
        status = json.loads(pathlib.Path(self.dir / "status.json").read_text())
        self.assertEqual(status["state"], "stopped")

    def test_secrets_and_urls_never_leak(self):
        s = self.supervisor(apply=True)
        self.ticks(s, 2)
        self.lab.rotate(V4B, A4B)
        self.ticks(s, 10)
        self.lab.fail = True
        self.ticks(s, 2)
        s.run(max_ticks=1)
        texts = [pathlib.Path(self.dir / "status.json").read_text(), pathlib.Path(self.dir / "journal.json").read_text(),
                 "\n".join(self.logs)]
        for text in texts:
            for secret in (TOKEN, EASY, "easy.invalid", CORE, "127.0.0.1"):
                self.assertNotIn(secret, text)


class CliTest(Harness):
    def run_main(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = sup.main(list(argv), inventory_get=self.lab.inventory, clock=self.clock, sleep=self.clock.sleep,
                            log=self.logs.append)
        return code, err.getvalue()

    def test_apply_requires_the_literal_confirmation(self):
        self.assertEqual(self.run_main("run", "--config", self.config_path, "--apply")[0], 2)
        self.assertEqual(self.run_main("run", "--config", self.config_path, "--apply", "--confirm", "yes")[0], 2)
        self.assertEqual(self.run_main("run", "--config", self.config_path, "--confirm", sup.CONFIRMATION)[0], 2)
        self.assertEqual(self.run_main("run", "--config", self.config_path, "--max-ticks", "1")[0], 0)

    def test_core_must_be_loopback(self):
        self.config_path = self.write_config(core_url="http://core.invalid:18085")
        code, err = self.run_main("run", "--config", self.config_path, "--max-ticks", "1")
        self.assertEqual(code, 2)
        self.assertIn("loopback", err)

    def test_bad_mapping_is_refused(self):
        self.config_path = self.write_config(mapping={"version": 1, "sources": [{"id": "x", "match": {}}]})
        self.assertEqual(self.run_main("run", "--config", self.config_path)[0], 2)


if __name__ == "__main__":
    unittest.main()
