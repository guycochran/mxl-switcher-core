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

    def test_prepare_rejects_duplicate_sources(self):
        app = self.app()
        with self.assertRaisesRegex(ValueError, "different source"):
            app.prepare({"count": 2, "sources": [1, 1]})

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
    def test_prepare_refuses_to_overwrite_program_or_preview_bank(self, run):
        app = self.app()
        app.core_json = lambda *args, **kwargs: (
            200,
            "application/json",
            b'{"program":"supersource-a","preview":"supersource-b"}',
        )

        with self.assertRaisesRegex(RuntimeError, "No safe off-air ME bank"):
            app.prepare({"count": 2, "sources": [1, 2]})

        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
