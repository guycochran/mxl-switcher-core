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

    def test_prepare_rejects_duplicate_sources(self):
        app = self.app()
        with self.assertRaisesRegex(ValueError, "different source"):
            app.prepare({"count": 2, "sources": [1, 1]})

    @mock.patch.object(subprocess, "run")
    def test_prepare_uses_bank_b_when_a_is_in_preview(self, run):
        app = self.app()
        run.return_value = subprocess.CompletedProcess([], 0, "", "")

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
        self.assertEqual(app.banks["b"]["sources"], [3, 4, 5])
        self.assertTrue(app.state_file.is_file())
        run.assert_called_once_with(
            ["/bin/helper", "b", "3", "3", "4", "5"],
            text=True,
            capture_output=True,
            timeout=75,
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
