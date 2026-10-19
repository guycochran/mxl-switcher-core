import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

import server


class AppTests(unittest.TestCase):
    def app(self, helper="/bin/helper"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        return server.App(root, "http://core", helper, root / "state.json")

    def test_prepare_requires_live_scene_engine(self):
        app = self.app(helper=None)
        with self.assertRaisesRegex(RuntimeError, "persistent SuperSource"):
            app.prepare({"count": 2, "sources": [1, 2]})

    def test_state_exposes_program_confidence_url(self):
        app = self.app()
        app.program_url = "http://127.0.0.1:13089/mxl-program/"
        app.core_json = lambda *args, **kwargs: (200, "application/json", b'{"revision":4}')
        self.assertEqual(app.state()["program_url"], app.program_url)

    def test_state_exposes_multiview_url(self):
        app = self.app()
        app.multiview_url = "http://127.0.0.1:13089/mxl-multiview/"
        app.core_json = lambda *args, **kwargs: (200, "application/json", b'{"revision":5}')
        self.assertEqual(app.state()["multiview_url"], app.multiview_url)

    def test_state_exposes_preview_confidence_url(self):
        app = self.app()
        app.preview_url = "http://127.0.0.1:13089/mxl-preview/"
        app.core_json = lambda *args, **kwargs: (200, "application/json", b'{"revision":6}')
        self.assertEqual(app.state()["preview_url"], app.preview_url)

    def test_state_reports_disabled_audio_mixer(self):
        app = self.app()
        app.core_json = lambda *args, **kwargs: (200, "application/json", b'{"revision":7}')
        self.assertEqual(app.state()["audio_mixer"]["state"], "disabled")

    def test_nmos_state_is_neutral_when_not_configured(self):
        app = self.app()
        self.assertEqual(
            app.nmos_state(), {"state": "disabled", "registry": "disabled"}
        )

    def test_nmos_state_reports_inventory_and_registration(self):
        app = self.app()
        app.nmos_node_url = "http://node"
        app.nmos_registry_url = "http://registry"

        def url_json(url, timeout=2):
            if url.endswith("/self"):
                return {
                    "id": "node-1",
                    "label": "MXL Core",
                    "clocks": [{"name": "clk0", "ref_type": "internal"}],
                }
            if url.endswith("/nodes"):
                return [{"id": "node-1"}]
            return {
                "devices": [{"id": "device-1"}],
                "sources": [{"id": "source-1"}, {"id": "source-2"}],
                "flows": [{"id": "flow-1"}, {"id": "flow-2"}],
                "senders": [{"id": "sender-1"}],
                "receivers": [{"id": "receiver-1"}],
            }[url.rsplit("/", 1)[-1]]

        app.url_json = url_json
        state = app.nmos_state()
        self.assertEqual(state["state"], "online")
        self.assertEqual(state["registry"], "registered")
        self.assertEqual(state["sources"], 2)
        self.assertEqual(state["clock"], "internal")

    def test_health_dashboard_aggregates_read_only_services(self):
        app = self.app()
        responses = {
            "/api/mxl/v1/state": b'{"revision":8,"program":"input-1"}',
            "/api/mxl/v1/health": b'{"status":"running","routed":12}',
            "/api/mxl/v1/sources": b'[{"id":"input-1"}]',
        }
        app.core_json = lambda path, *args, **kwargs: (
            200,
            "application/json",
            responses[path],
        )
        dashboard = app.health_dashboard_state()
        self.assertEqual(dashboard["core"]["revision"], 8)
        self.assertEqual(dashboard["health"]["routed"], 12)
        self.assertEqual(dashboard["sources"][0]["id"], "input-1")
        self.assertEqual(dashboard["nmos"]["state"], "disabled")
        self.assertEqual(dashboard["extensions"], [])

    def test_extension_health_is_aggregated_without_exposing_urls(self):
        app = self.app()
        app.extension_health_urls = {
            "meeting-return": "http://127.0.0.1:18120",
            "recorder": "http://127.0.0.1:18110",
        }

        def url_json(url, timeout=2):
            if ":18120/" in url:
                return {
                    "name": "program-meeting-return",
                    "kind": "output",
                    "state": "running",
                    "ready": True,
                    "revision": 9,
                    "restarts": 0,
                    "last_error": None,
                    "metrics": {"secret-free": True},
                }
            raise OSError("connection refused")

        app.url_json = url_json
        states = app.extension_states()
        self.assertEqual(states[0]["name"], "program-meeting-return")
        self.assertTrue(states[0]["ready"])
        self.assertEqual(states[1]["state"], "unavailable")
        self.assertNotIn("url", states[0])
        self.assertNotIn("metrics", states[0])

    def test_extension_health_configuration_is_loopback_only(self):
        self.assertEqual(
            server.parse_extension_health(["recorder=http://127.0.0.1:18110/"]),
            {"recorder": "http://127.0.0.1:18110"},
        )
        with self.assertRaisesRegex(ValueError, "loopback"):
            server.parse_extension_health(["recorder=http://192.0.2.1:18110"])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            server.parse_extension_health([
                "recorder=http://127.0.0.1:18110",
                "recorder=http://127.0.0.1:18111",
            ])

    def recovery_app(self, apply_enabled=False):
        app = self.app()
        app.easy_mxl_url = "http://127.0.0.1:19700"
        app.easy_mxl_domain = "zoomiso"
        app.recovery_apply_enabled = apply_enabled
        for attribute in ("recovery_mapping", "recovery_planner", "recovery_executor"):
            path = app.root / f"{attribute}.json"
            path.write_text("{}")
            setattr(app, attribute, path)
        app.core_json = lambda *args, **kwargs: (
            200, "application/json", b'{"revision":12,"program":"input-1","preview":"input-2"}'
        )
        return app

    def planner_result(self, blocked=False):
        return {
            "planner": "mxl-discovery-planner",
            "plan_version": 1,
            "read_only": True,
            "blocked": blocked,
            "summary": {"rebind": 2, "noop": 0},
            "sources": [
                {"id": "input-1", "status": "rebind", "reasons": [], "current": {}, "desired": {}},
                {"id": "input-2", "status": "rebind", "reasons": [], "current": {}, "desired": {}},
            ],
            "requests": [],
        }

    def test_recovery_is_disabled_without_configuration(self):
        self.assertEqual(self.app().recovery_plan(), {"state": "disabled", "apply_enabled": False})

    @mock.patch.object(subprocess, "run")
    def test_recovery_scan_returns_reviewable_plan_without_token(self, run):
        app = self.recovery_app()
        document = self.planner_result()

        def command(args, **kwargs):
            output = pathlib.Path(args[args.index("--output") + 1])
            output.write_text(json.dumps(document))
            self.assertNotIn("--token", args)
            self.assertEqual(args[args.index("--token-env") + 1], "EASY_MXL_TOKEN")
            return subprocess.CompletedProcess(args, 0, "", "")

        run.side_effect = command
        result = app.recovery_plan()
        self.assertEqual(result["state"], "ready")
        self.assertFalse(result["apply_enabled"])
        self.assertEqual(result["revision"], 12)
        self.assertEqual(result["summary"]["rebind"], 2)
        self.assertEqual(len(result["plan_id"]), 64)

    def test_recovery_preflight_rejects_changed_plan(self):
        app = self.recovery_app()
        app.recovery_plan = lambda: {
            "state": "ready", "plan_id": "new", "plan": self.planner_result(), "apply_enabled": False,
        }
        with self.assertRaisesRegex(ValueError, "inventory changed"):
            app.recovery_preflight({"plan_id": "old"})

    def test_recovery_apply_requires_server_arm(self):
        app = self.recovery_app(apply_enabled=False)
        with self.assertRaisesRegex(RuntimeError, "not armed"):
            app.recovery_apply({"plan_id": "x", "expected_revision": 12, "confirm": "APPLY_REBINDS"})

    @mock.patch.object(subprocess, "run")
    def test_recovery_apply_regenerates_plan_and_passes_guardrails(self, run):
        app = self.recovery_app(apply_enabled=True)
        document = self.planner_result()
        plan_id = app.plan_id(document)
        app.recovery_plan = lambda: {
            "state": "ready", "plan_id": plan_id, "revision": 12,
            "plan": document, "apply_enabled": True,
        }

        def command(args, **kwargs):
            self.assertIn("--apply", args)
            self.assertEqual(args[args.index("--expect-revision") + 1], "12")
            self.assertEqual(args[args.index("--confirm") + 1], "APPLY_REBINDS")
            return subprocess.CompletedProcess(args, 0, '{"applied":true,"final_revision":14}\n', "")

        run.side_effect = command
        result = app.recovery_apply({
            "plan_id": plan_id, "expected_revision": 12, "confirm": "APPLY_REBINDS",
        })
        self.assertTrue(result["applied"])
        self.assertEqual(result["final_revision"], 14)

    def test_recovery_preflight_fails_closed_on_duplicate_audio_ownership(self):
        """The real executor, run by the Skin, refuses a malformed plan and never writes."""

        import http.server
        import threading

        def uid(n):
            return f"00000000-0000-4000-8000-{n:012d}"

        writes = []
        state = {"revision": 12, "program": "input-1", "preview": "input-2"}
        sources = [{"id": "input-1", "flow_id": uid(1), "audio_flow_id": None},
                   {"id": "input-2", "flow_id": uid(2), "audio_flow_id": None}]

        class FakeCore(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = json.dumps(state if self.path.endswith("/state") else sources).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_PUT(self):  # noqa: N802
                writes.append(self.path)
                self.send_response(500)
                self.end_headers()

            def log_message(self, *_):
                return

        core = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeCore)
        threading.Thread(target=core.serve_forever, daemon=True).start()
        self.addCleanup(core.server_close)
        self.addCleanup(core.shutdown)

        app = self.recovery_app()
        app.core = f"http://127.0.0.1:{core.server_address[1]}"
        app.recovery_executor = pathlib.Path(__file__).resolve().parents[1] / "mxl_rebind_apply.py"
        shared = uid(30)
        entries, requests = [], []
        for source_id, old, new in (("input-1", 1, 11), ("input-2", 2, 12)):
            desired = {"flow_id": uid(new), "audio_flow_id": shared}
            entries.append({"id": source_id, "status": "rebind", "reasons": [],
                            "current": {"flow_id": uid(old), "audio_flow_id": None}, "desired": desired})
            requests.append({"source": source_id, "method": "PUT",
                             "path": f"/api/mxl/v1/sources/{source_id}", "body": desired})
        document = {"planner": "mxl-discovery-planner", "plan_version": 1, "read_only": True,
                    "blocked": False, "sources": entries, "requests": requests}
        app.recovery_plan = lambda: {"state": "ready", "plan_id": "reviewed", "revision": 12,
                                     "plan": document, "apply_enabled": True}
        with self.assertRaisesRegex(RuntimeError, "one audio flow to more than one source"):
            app.recovery_preflight({"plan_id": "reviewed"})
        app.recovery_apply_enabled = True
        with self.assertRaisesRegex(RuntimeError, "one audio flow to more than one source"):
            app.recovery_apply({"plan_id": "reviewed", "expected_revision": 12, "confirm": "APPLY_REBINDS"})
        self.assertEqual(writes, [])

    def test_prepare_rejects_duplicate_sources(self):
        app = self.app()
        with self.assertRaisesRegex(ValueError, "different source"):
            app.prepare({"count": 2, "sources": [1, 1]})

    def test_prepare_rejects_undefined_layouts(self):
        app = self.app("/helper")
        with self.assertRaisesRegex(ValueError, "2-up, 3-up, 4-up, 6-up, or 8-up"):
            app.prepare({"count": 5, "sources": [1, 2, 3, 4, 5]})

    @mock.patch.object(subprocess, "run")
    def test_prepare_uses_bank_b_when_a_is_in_preview(self, run):
        app = self.app()
        run.return_value = subprocess.CompletedProcess(
            [], 0, '{"bank":"b","layout":3,"state":"ready","ready_ms":640}\n', ""
        )

        def core_json(path, method="GET", body=None):
            if path.endswith("/state"):
                value = {"program": "zoomiso-1", "preview": "supersource-a"}
                return 200, "application/json", json.dumps(value).encode()
            self.assertEqual(path, "/api/mxl/v1/preview")
            self.assertEqual(body, {"source": "supersource-b"})
            return 202, "application/json", b'{"command_id":7}'

        app.core_json = core_json
        result = app.prepare({"count": 3, "sources": [3, 4, 5]})
        self.assertEqual(result["bank"], "b")
        self.assertEqual(result["engine"]["ready_ms"], 640)
        self.assertEqual(app.banks["b"]["sources"], [3, 4, 5])
        self.assertTrue(app.state_file.is_file())
        run.assert_called_once_with(
            ["/bin/helper", "b", "3", "3", "4", "5"],
            text=True,
            capture_output=True,
            timeout=75,
            check=False,
        )

    @mock.patch.object(subprocess, "run")
    def test_prepare_rebuilds_preview_bank_when_other_bank_is_program(self, run):
        app = self.app()
        run.return_value = subprocess.CompletedProcess(
            [], 0, '{"bank":"b","layout":2,"state":"ready","ready_ms":400}\n', ""
        )

        def core_json(path, method="GET", body=None):
            if path.endswith("/state"):
                value = {"program": "supersource-a", "preview": "supersource-b"}
                return 200, "application/json", json.dumps(value).encode()
            self.assertEqual(path, "/api/mxl/v1/preview")
            self.assertEqual(body, {"source": "supersource-b"})
            return 202, "application/json", b'{"command_id":8}'

        app.core_json = core_json
        result = app.prepare({"count": 2, "sources": [1, 2]})

        self.assertEqual(result["bank"], "b")
        self.assertEqual(app.banks["b"]["sources"], [1, 2])
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
