import copy
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import mxl_rebind_apply as apply_tool  # noqa: E402


def uid(n):
    return f"00000000-0000-4000-8000-{n:012d}"


def source(source_id, video, audio):
    return {"id": source_id, "flow_id": video, "audio_flow_id": audio}


def request(source_id, current_video, current_audio, desired_video, desired_audio):
    path = "/api/mxl/v1/sources/" + source_id
    current = {"flow_id": current_video, "audio_flow_id": current_audio}
    desired = {"flow_id": desired_video, "audio_flow_id": desired_audio}
    return (
        {"id": source_id, "status": "rebind", "current": current, "desired": desired},
        {"source": source_id, "method": "PUT", "path": path, "body": desired},
    )


def plan(*specs):
    entries, requests = zip(*specs) if specs else ([], [])
    return {
        "planner": "mxl-discovery-planner", "plan_version": 1, "read_only": True,
        "blocked": False, "sources": list(entries), "requests": list(requests),
    }


class PreflightTest(unittest.TestCase):
    def test_preview_then_off_air_then_program(self):
        specs = [
            request("program", uid(1), uid(2), uid(11), uid(12)),
            request("off-air", uid(3), uid(4), uid(13), uid(14)),
            request("preview", uid(5), uid(6), uid(15), uid(16)),
        ]
        live = [source("program", uid(1), uid(2)), source("off-air", uid(3), uid(4)),
                source("preview", uid(5), uid(6))]
        prepared = apply_tool.preflight(plan(*specs), {"revision": 7, "program": "program", "preview": "preview"}, live)
        self.assertEqual([item["source"] for item in prepared["requests"]], ["preview", "off-air", "program"])
        self.assertEqual(prepared["strategy"], "preview_off_air_program")

    def test_stale_plan_is_rejected(self):
        spec = request("cam", uid(1), uid(2), uid(11), uid(12))
        with self.assertRaisesRegex(apply_tool.ApplyError, "live binding changed"):
            apply_tool.preflight(plan(spec), {"revision": 1, "program": "cam", "preview": "other"},
                                 [source("cam", uid(99), uid(2))])

    def test_blocked_plan_is_rejected(self):
        document = plan(request("cam", uid(1), uid(2), uid(11), uid(12)))
        document["blocked"] = True
        with self.assertRaisesRegex(apply_tool.ApplyError, "blocked"):
            apply_tool.preflight(document, {"revision": 1}, [source("cam", uid(1), uid(2))])

    def test_missing_rebind_request_is_rejected(self):
        document = plan(request("cam", uid(1), uid(2), uid(11), uid(12)))
        document["requests"] = []
        with self.assertRaisesRegex(apply_tool.ApplyError, "exactly cover"):
            apply_tool.preflight(document, {"revision": 1, "program": "cam", "preview": "other"},
                                 [source("cam", uid(1), uid(2))])

    def test_program_dependency_before_off_air_is_rejected(self):
        # off-air wants Program's current flow, so Program must release it first.
        specs = [
            request("program", uid(1), uid(2), uid(11), uid(12)),
            request("off-air", uid(3), uid(4), uid(1), uid(14)),
        ]
        live = [source("program", uid(1), uid(2)), source("off-air", uid(3), uid(4))]
        with self.assertRaisesRegex(apply_tool.ApplyError, "Program before"):
            apply_tool.preflight(plan(*specs), {"revision": 4, "program": "program", "preview": "off-air"}, live)

    def test_non_loopback_core_is_rejected(self):
        with self.assertRaisesRegex(apply_tool.ApplyError, "loopback"):
            apply_tool._loopback_base("http://192.0.2.10:18085")


class ExecuteTest(unittest.TestCase):
    def setUp(self):
        self.specs = [
            request("preview", uid(1), uid(2), uid(11), uid(12)),
            request("program", uid(3), uid(4), uid(13), uid(14)),
        ]
        live = [source("preview", uid(1), uid(2)), source("program", uid(3), uid(4))]
        self.prepared = apply_tool.preflight(
            plan(*self.specs), {"revision": 9, "program": "program", "preview": "preview"}, live)
        self.bindings = {item["id"]: apply_tool._binding(item) for item in live}
        self.revision = 9

    def fake_request(self, _base, path, method="GET", body=None, timeout=10.0):
        if path == apply_tool.STATE_PATH:
            return {"revision": self.revision, "program": "program", "preview": "preview"}
        if path == apply_tool.SOURCES_PATH:
            return [{"id": source_id, **binding} for source_id, binding in self.bindings.items()]
        source_id = path.rsplit("/", 1)[-1]
        previous = self.bindings[source_id]
        self.bindings[source_id] = copy.deepcopy(body)
        self.revision += 1
        return {"changed": previous != body, "revision": self.revision, **body}

    def test_execute_verifies_both_rebinds(self):
        original = apply_tool._request_json
        apply_tool._request_json = self.fake_request
        try:
            result = apply_tool.execute("http://127.0.0.1:18085", self.prepared, 9)
        finally:
            apply_tool._request_json = original
        self.assertTrue(result["applied"])
        self.assertEqual(result["final_revision"], 11)
        self.assertEqual([item["source"] for item in result["receipts"]], ["preview", "program"])
        self.assertEqual(self.bindings["program"], {"flow_id": uid(13), "audio_flow_id": uid(14)})

    def test_revision_change_prevents_first_write(self):
        self.revision = 10
        original = apply_tool._request_json
        apply_tool._request_json = self.fake_request
        try:
            with self.assertRaisesRegex(apply_tool.ApplyError, "revision changed"):
                apply_tool.execute("http://127.0.0.1:18085", self.prepared, 9)
        finally:
            apply_tool._request_json = original
        self.assertEqual(self.bindings["preview"], {"flow_id": uid(1), "audio_flow_id": uid(2)})

    def test_second_write_failure_rolls_back_first(self):
        calls = 0

        def fail_second(base, path, method="GET", body=None, timeout=10.0):
            nonlocal calls
            if method == "PUT":
                calls += 1
                if calls == 2:
                    raise apply_tool.ApplyError("injected failure")
            return self.fake_request(base, path, method=method, body=body, timeout=timeout)

        original = apply_tool._request_json
        apply_tool._request_json = fail_second
        try:
            with self.assertRaisesRegex(apply_tool.ApplyError, "injected failure"):
                apply_tool.execute("http://127.0.0.1:18085", self.prepared, 9)
        finally:
            apply_tool._request_json = original
        self.assertEqual(self.bindings["preview"], {"flow_id": uid(1), "audio_flow_id": uid(2)})
        self.assertEqual(self.bindings["program"], {"flow_id": uid(3), "audio_flow_id": uid(4)})


class AudioOwnershipPreflightTest(unittest.TestCase):
    STATE = {"revision": 4, "program": "cam-a", "preview": "cam-b"}

    def test_malformed_plan_claiming_one_audio_flow_twice_is_rejected(self):
        # Supplied directly: blocked=False and statuses look actionable, but two
        # sources would own the same audio flow.
        document = plan(request("cam-a", uid(1), None, uid(11), uid(30)),
                        request("cam-b", uid(2), None, uid(12), uid(30)))
        live = [source("cam-a", uid(1), None), source("cam-b", uid(2), None)]
        with self.assertRaisesRegex(apply_tool.ApplyError, "plan assigns one audio flow to more than one source"):
            apply_tool.preflight(document, self.STATE, live)

    def test_noop_entry_sharing_audio_with_a_rebind_is_rejected(self):
        entry, req = request("cam-b", uid(2), None, uid(12), uid(30))
        noop = {"id": "cam-a", "status": "noop", "current": {"flow_id": uid(1), "audio_flow_id": uid(30)},
                "desired": {"flow_id": uid(1), "audio_flow_id": uid(30)}}
        document = plan((entry, req))
        document["sources"].append(noop)
        live = [source("cam-a", uid(1), uid(30)), source("cam-b", uid(2), None)]
        with self.assertRaisesRegex(apply_tool.ApplyError, "one audio flow to more than one source"):
            apply_tool.preflight(document, self.STATE, live)

    def test_audio_held_by_a_source_outside_the_plan_is_rejected(self):
        document = plan(request("cam-a", uid(1), None, uid(11), uid(30)))
        live = [source("cam-a", uid(1), None), source("program-audio", uid(9), uid(30))]
        with self.assertRaisesRegex(apply_tool.ApplyError, "applying cam-a assigns one audio flow"):
            apply_tool.preflight(document, self.STATE, live)

    def test_audio_transfer_must_release_before_claim(self):
        # cam-b releases audio 30 to cam-a. The executor must order cam-b first.
        claim = request("cam-a", uid(1), None, uid(11), uid(30))
        release = request("cam-b", uid(2), uid(30), uid(12), uid(31))
        live = [source("cam-a", uid(1), None), source("cam-b", uid(2), uid(30))]
        state = {"revision": 4, "program": "other", "preview": "none"}
        prepared = apply_tool.preflight(plan(release, claim), state, live)
        self.assertEqual([item["source"] for item in prepared["requests"]], ["cam-b", "cam-a"])
        with self.assertRaisesRegex(apply_tool.ApplyError, "violates a live flow dependency"):
            apply_tool.preflight(plan(claim, release), state, live)

    def test_optional_null_audio_on_several_sources_is_allowed(self):
        document = plan(request("gfx-1", uid(1), uid(20), uid(11), None),
                        request("gfx-2", uid(2), uid(21), uid(12), None))
        live = [source("gfx-1", uid(1), uid(20)), source("gfx-2", uid(2), uid(21))]
        prepared = apply_tool.preflight(document, {"revision": 4, "program": "gfx-1", "preview": "gfx-2"}, live)
        self.assertEqual([item["source"] for item in prepared["requests"]], ["gfx-2", "gfx-1"])

    def test_distinct_audio_pairs_pass(self):
        document = plan(request("cam-a", uid(1), None, uid(11), uid(30)),
                        request("cam-b", uid(2), None, uid(12), uid(31)))
        live = [source("cam-a", uid(1), None), source("cam-b", uid(2), None)]
        prepared = apply_tool.preflight(document, self.STATE, live)
        self.assertEqual([item["source"] for item in prepared["requests"]], ["cam-b", "cam-a"])


if __name__ == "__main__":
    unittest.main()
