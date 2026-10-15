import contextlib
import hashlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import mxl_av_matrix as matrix  # noqa: E402

REVISION = {"git_commit": "0" * 40, "tools_dirty": False, "tools": {}}


def analyzer_report(seconds, failures=(), step=1):
    """An analyzer report whose reference advanced ``seconds`` at 30 grains per second."""

    frames = int(seconds * 30) // step
    return {"video": {"frames": frames, "step_histogram": {str(step): frames}},
            "sync": {"pairs": int(seconds)}, "passed": not failures, "failures": list(failures)}


class FakeAnalyzer:
    """Stands in for ``mxl_av_analyze.py``: ``outcomes`` maps capture file name to (exit, report or text)."""

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.commands = []

    def __call__(self, command):
        self.commands.append(command)
        capture = pathlib.Path(command[command.index("--media") + 1]).name
        status, report = self.outcomes[capture]
        stdout = report if isinstance(report, str) else json.dumps(report)
        return status, stdout, "" if status in (0, 1) else "ffmpeg: invalid data"


class MatrixTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        for name in ("a.mkv", "b.mkv", "c.mkv"):
            (self.root / name).write_bytes(name.encode() * 1000)
        self.results = self.root / "results"

    def manifest(self, **overrides):
        body = {
            "schema": "mxl-av-matrix/1",
            "matrix": "preview1-program-return",
            "stage": "10min",
            "defaults": {"limits": {"max_skipped_grains": -1, "max_freeze_ms": 250}, "event_channel": "left"},
            "cases": [
                {"name": "index-mixer", "configuration": {"name": "index-mixer+clock-recovery", "max_wait_ms": 500},
                 "capture": "a.mkv"},
                {"name": "gstreamer-mixer", "configuration": {"name": "clock-driven-mixer"}, "capture": "b.mkv",
                 "limits": {"audio_lag_ms": 80}},
            ],
        }
        body.update(overrides)
        path = self.root / "manifest.json"
        path.write_text(json.dumps(body))
        return str(path)

    def main(self, *args, executor=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = matrix.main(list(args), executor=executor or FakeAnalyzer({}), revision=REVISION)
        return code, out.getvalue(), err.getvalue()

    def report(self):
        return json.loads((self.results / "report.json").read_text())


class PlanTest(MatrixTest):
    def test_dry_run_shows_every_case_and_invocation_and_writes_nothing(self):
        analyzer = FakeAnalyzer({})
        manifest = self.manifest()
        before = sorted(os.listdir(self.root))
        code, out, _ = self.main(manifest, executor=analyzer)
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertEqual(plan["mode"], "dry_run")
        self.assertEqual(plan["minimum_covered_s"], 600)
        self.assertEqual([case["name"] for case in plan["cases"]], ["index-mixer", "gstreamer-mixer"])
        command = plan["cases"][1]["analyzer"]
        self.assertEqual(command[1], str(matrix.ANALYZER))
        self.assertEqual(command[command.index("--media") + 1], str((self.root / "b.mkv").resolve()))
        for flag, value in (("--audio-lag-ms", "80.0"), ("--max-skipped-grains", "-1"),
                            ("--max-freeze-ms", "250.0"), ("--event-channel", "left")):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertEqual(plan["cases"][0]["capture_bytes"], 5000)
        self.assertEqual(analyzer.commands, [])
        self.assertEqual(sorted(os.listdir(self.root)), before)

    def test_case_limits_override_defaults(self):
        loaded = matrix.load_manifest(self.manifest())
        self.assertEqual(loaded["cases"][1]["limits"],
                         {"max_skipped_grains": -1, "max_freeze_ms": 250.0, "audio_lag_ms": 80.0})

    def test_stage_override(self):
        self.assertEqual(matrix.load_manifest(self.manifest(), "8h")["minimum_covered_s"], 28_800)


class RefusalTest(MatrixTest):
    def assertRefused(self, *args, contains):
        code, out, err = self.main(*args)
        self.assertEqual(code, 2, out)
        self.assertIn(contains, json.loads(err)["refused"])
        self.assertFalse(self.results.exists())

    def test_manifest_problems_are_refused(self):
        good = json.loads(pathlib.Path(self.manifest()).read_text())
        case = good["cases"][0]
        cases = {
            "the matrix has no cases": {"cases": []},
            "duplicate case name index-mixer": {"cases": [case, case]},
            "does not exist": {"cases": [{**case, "capture": "missing.mkv"}]},
            "stage must be one of": {"stage": "1h"},
            "must be a number": {"cases": [{**case, "limits": {"audio_lag_ms": "60"}}]},
            "must be an integer": {"cases": [{**case, "limits": {"min_pairs": 2.5}}]},
            "must be at least 0 (or -1 to disable)": {"cases": [{**case, "limits": {"max_repeats": -5}}]},
            "must be at least 1": {"cases": [{**case, "limits": {"min_pairs": 0}}]},
            "fraction from 0 to 1": {"defaults": {"limits": {"max_undecodable_fraction": 1.5}}},
            "unknown key(s) max_lag": {"cases": [{**case, "limits": {"max_lag": 1}}]},
            "unknown key(s) captre": {"cases": [{**case, "captre": "a.mkv"}]},
            "configuration must be an object with a name": {"cases": [{**case, "configuration": "x"}]},
            "name must match": {"cases": [{**case, "name": "../escape"}]},
            "event_channel must be one of": {"cases": [{**case, "event_channel": "centre"}]},
            "limit audio_lag_ms must be a number": {"cases": [{**case, "limits": {"audio_lag_ms": True}}]},
            "required must be true or false": {"cases": [{**case, "required": "yes"}]},
            "manifest schema must be": {"schema": "other/2"},
        }
        for contains, override in cases.items():
            with self.subTest(contains):
                self.assertRefused(self.manifest(**override), "--apply", "--result-dir", str(self.results),
                                   contains=contains)

    def test_a_directory_is_not_a_capture(self):
        (self.root / "dir.mkv").mkdir()
        case = {"name": "x", "configuration": {"name": "x"}, "capture": "dir.mkv"}
        self.assertRefused(self.manifest(cases=[case]), contains="not a file")

    def test_invalid_json_and_missing_manifest(self):
        bad = self.root / "bad.json"
        bad.write_text("{")
        self.assertRefused(str(bad), contains="invalid JSON")
        self.assertRefused(str(self.root / "absent.json"), contains="manifest")

    def test_existing_result_dir_is_never_reused(self):
        self.results.mkdir()
        (self.results / "keep.txt").write_text("evidence")
        code, _, err = self.main(self.manifest(), "--apply", "--result-dir", str(self.results))
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)
        self.assertEqual(os.listdir(self.results), ["keep.txt"])

    def test_apply_needs_a_named_result_dir_with_an_existing_parent(self):
        self.assertRefused(self.manifest(), "--apply", contains="--apply needs --result-dir")
        code, _, err = self.main(self.manifest(), "--apply", "--result-dir", str(self.root / "no" / "such"))
        self.assertEqual(code, 2)
        self.assertIn("parent", err)
        self.assertFalse((self.root / "no").exists())


class VerdictTest(MatrixTest):
    def apply(self, outcomes, *extra):
        analyzer = FakeAnalyzer(outcomes)
        code, out, _ = self.main(self.manifest(*extra) if not extra else extra[0], "--apply",
                                 "--result-dir", str(self.results), executor=analyzer)
        return code, analyzer

    def test_all_required_cases_pass(self):
        code, analyzer = self.apply({"a.mkv": (0, analyzer_report(605)), "b.mkv": (0, analyzer_report(620))})
        self.assertEqual(code, 0)
        report = self.report()
        self.assertEqual((report["verdict"], report["counts"]["passed"]), ("PASS", 2))
        case = report["cases"][0]
        self.assertEqual(case["capture"]["sha256"], hashlib.sha256(b"a.mkv" * 1000).hexdigest())
        self.assertEqual(case["capture"]["bytes"], 5000)
        self.assertEqual(case["configuration"]["max_wait_ms"], 500)
        self.assertEqual(case["covered_s"], 605.0)
        self.assertEqual(case["analyzer_exit"], 0)
        self.assertEqual(case["report"]["sync"]["pairs"], 605)
        self.assertEqual(case["limits"], {"max_skipped_grains": -1, "max_freeze_ms": 250.0})
        self.assertEqual(report["source"], REVISION)
        self.assertEqual(len(analyzer.commands), 2)
        self.assertTrue((self.results / "index-mixer.analyzer.json").is_file())
        self.assertEqual(report["configurations"]["index-mixer+clock-recovery"],
                         {"cases": ["index-mixer"], "verdict": "PASS"})

    def test_any_required_failure_fails_the_matrix(self):
        code, _ = self.apply({"a.mkv": (0, analyzer_report(605)),
                              "b.mkv": (1, analyzer_report(605, ["video freeze above limit"]))})
        self.assertEqual(code, 1)
        report = self.report()
        self.assertEqual((report["verdict"], report["required_failed"]), ("FAIL", ["gstreamer-mixer"]))
        self.assertEqual(report["cases"][1]["failures"], ["video freeze above limit"])
        self.assertEqual(report["configurations"]["clock-driven-mixer"]["verdict"], "FAIL")

    def test_a_short_capture_never_satisfies_a_longer_stage(self):
        code, _ = self.apply({"a.mkv": (0, analyzer_report(599)), "b.mkv": (0, analyzer_report(1900))})
        self.assertEqual(code, 1)
        case = self.report()["cases"][0]
        self.assertEqual(case["verdict"], "FAIL")
        self.assertIn("stage 10min needs at least 600 s", case["failures"][0])

    def test_coverage_comes_from_the_reference_not_the_frame_count(self):
        # A reduced-rate path (every other grain) still covers the full reference time.
        code, _ = self.apply({"a.mkv": (0, analyzer_report(610, step=2)), "b.mkv": (0, analyzer_report(610))})
        self.assertEqual(code, 0)
        self.assertEqual(self.report()["cases"][0]["covered_s"], 610.0)
        # A long but frozen file advances the reference by almost nothing.
        frozen = {"video": {"frames": 18_000, "step_histogram": {"0": 17_990, "1": 9}}, "passed": True,
                  "failures": [], "sync": {}}
        self.assertEqual(matrix.covered_seconds(frozen), 0.3)

    def test_optional_case_failure_does_not_fail_the_matrix(self):
        manifest = self.manifest()
        body = json.loads(pathlib.Path(manifest).read_text())
        body["cases"][1]["required"] = False
        pathlib.Path(manifest).write_text(json.dumps(body))
        code, _ = self.apply({"a.mkv": (0, analyzer_report(605)),
                              "b.mkv": (1, analyzer_report(605, ["A/V drift above limit"]))}, manifest)
        self.assertEqual(code, 0)
        report = self.report()
        self.assertEqual((report["verdict"], report["counts"]["failed"]), ("PASS", 1))

    def test_no_required_cases_is_not_a_pass(self):
        manifest = self.manifest(defaults={"required": False})
        code, _ = self.apply({"a.mkv": (0, analyzer_report(605)), "b.mkv": (0, analyzer_report(605))}, manifest)
        self.assertEqual(code, 1)
        self.assertEqual(self.report()["required_failed"], ["no required cases"])

    def test_analyzer_errors_are_recorded_and_fail(self):
        code, _ = self.apply({"a.mkv": (2, "not json"), "b.mkv": (0, analyzer_report(605))})
        self.assertEqual(code, 1)
        case = self.report()["cases"][0]
        self.assertEqual((case["verdict"], case["analyzer_exit"]), ("ERROR", 2))
        self.assertIn("ffmpeg: invalid data", case["failures"][0])
        self.assertEqual((self.results / "index-mixer.analyzer.stderr.txt").read_text(), "ffmpeg: invalid data")
        self.assertEqual(self.report()["counts"]["errors"], 1)

    def test_exit_status_must_agree_with_the_report(self):
        code, _ = self.apply({"a.mkv": (0, analyzer_report(605, ["video freeze above limit"])),
                              "b.mkv": (0, analyzer_report(605))})
        self.assertEqual(code, 1)
        self.assertIn("analyzer exit status disagrees with its report", self.report()["cases"][0]["failures"])

    def test_only_the_named_result_dir_is_created(self):
        manifest = self.manifest()
        before = set(os.listdir(self.root))
        self.apply({"a.mkv": (0, analyzer_report(605)), "b.mkv": (0, analyzer_report(605))}, manifest)
        self.assertEqual(set(os.listdir(self.root)) - before, {"results"})


class RealAnalyzerTest(unittest.TestCase):
    def test_command_line_is_accepted_by_the_real_analyzer_parser(self):
        import mxl_av_analyze

        case = {"capture_path": "/x.mkv", "limits": {key: (-1 if spec[3] is not None else spec[2] + 1)
                                                      for key, spec in matrix.LIMITS.items()},
                "event_channel": "mix", "counter_channel": "right"}
        command = matrix.analyzer_command(case, python="python3")

        def capture(*args, **kwargs):
            raise SystemExit(0)

        original = mxl_av_analyze.media_inputs
        mxl_av_analyze.media_inputs = capture
        self.addCleanup(setattr, mxl_av_analyze, "media_inputs", original)
        parser_args = command[2:]
        with self.assertRaises(SystemExit) as stop:
            mxl_av_analyze.main(parser_args)
        self.assertEqual(stop.exception.code, 0)  # parsed, then stopped before ffmpeg

    def test_source_revision_names_the_tools(self):
        revision = matrix.source_revision()
        self.assertIn("mxl_av_analyze.py", revision["tools"])
        self.assertEqual(len(revision["tools"]["av_reference.py"]), 64)


if __name__ == "__main__":
    unittest.main()
