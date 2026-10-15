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


if __name__ == "__main__":
    unittest.main()
