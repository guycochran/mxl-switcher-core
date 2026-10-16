import contextlib
import io
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import meeting_return_resampler as mrr  # noqa: E402
import meeting_return_resampler_gate as gate  # noqa: E402

SCALE = 0.15  # short schedule: every segment still spans several analysis windows


class PreflightTest(unittest.TestCase):
    def test_missing_dependency_is_refused_not_substituted(self):
        class Missing:
            info = mrr.BackendInfo("missing", True, "x", "x")

            def __init__(self):
                raise ImportError("no module", name="samplerate")

        original = dict(mrr.BACKENDS)
        mrr.BACKENDS["missing"] = Missing
        self.addCleanup(lambda: (mrr.BACKENDS.clear(), mrr.BACKENDS.update(original)))
        with self.assertRaises(mrr.ResamplerUnavailable) as raised:
            mrr.select_backend("missing")
        self.assertIn("refusing rather than substituting", str(raised.exception))

    def test_production_candidate_is_refused_or_qualified_never_downgraded(self):
        try:
            backend, info = mrr.select_backend()
        except mrr.ResamplerUnavailable as error:
            self.assertIn("not installed", str(error))
        else:
            self.assertTrue(info["qualified_candidate"])
            self.assertEqual(info["backend"], "samplerate-sinc-best")

    def test_test_instruments_are_never_production(self):
        for name in ("linear", "drop-hold"):
            with self.subTest(name=name):
                with self.assertRaises(mrr.ResamplerUnavailable):
                    mrr.select_backend(name)
                backend, info = mrr.select_backend(name, allow_unqualified=True)
                self.assertFalse(info["qualified_candidate"])

    def test_unknown_backend(self):
        with self.assertRaises(mrr.ResamplerUnavailable):
            mrr.select_backend("magic")


class AdapterTest(unittest.TestCase):
    def test_exact_output_counts_across_varied_segments(self):
        adapter = mrr.PayloadResampler(mrr.OracleBackend(gate.reference), prime_frames=128)
        position = 0
        for source_n, output_n in ((480, 476), (441, 445), (1024, 1014), (160, 161), (480, 480)):
            src = [gate.reference(position + i) for i in range(source_n)]
            left, right = adapter.process([s[0] for s in src], [s[1] for s in src], source_n, output_n)
            self.assertEqual((len(left), len(right)), (output_n, output_n))
            position += source_n
        self.assertEqual(adapter.status()["prime_frames"], 128)
        self.assertEqual(adapter.counters["source_frames"], position)

    def test_underflow_fails_closed_and_latches(self):
        class Starved(mrr.OracleBackend):
            def process(self, left, right, ratio):
                return [], []

        adapter = mrr.PayloadResampler(Starved(gate.reference), prime_frames=100)
        with self.assertRaises(mrr.ResamplerUnavailable):
            adapter.process([0.0] * 480, [0.0] * 480, 480, 480)
        self.assertEqual(adapter.status()["failed"], "fifo_underflow")
        with self.assertRaises(mrr.ResamplerUnavailable):
            adapter.process([0.0] * 10, [0.0] * 10, 10, 10)  # latched

    def test_overflow_fails_closed(self):
        class Flood(mrr.OracleBackend):
            def process(self, left, right, ratio):
                return [0.0] * 10_000, [0.0] * 10_000

        adapter = mrr.PayloadResampler(Flood(gate.reference), max_fifo_frames=4_800)
        with self.assertRaises(mrr.ResamplerUnavailable):
            adapter.process([0.0] * 480, [0.0] * 480, 480, 480)
        self.assertEqual(adapter.status()["failed"], "fifo_overflow")

    def test_mismatched_input_is_rejected(self):
        adapter = mrr.PayloadResampler(mrr.OracleBackend(gate.reference))
        with self.assertRaises(ValueError):
            adapter.process([0.0] * 10, [0.0] * 9, 10, 10)


class ScheduleTest(unittest.TestCase):
    def test_ramps_obey_the_scheduler_slew_contract(self):
        segments = {label: (seconds, ppm) for label, seconds, ppm in gate.schedule()}
        self.assertAlmostEqual(segments["ramp_up"][0], 9400 / gate.SLEW_PPM_PER_S)
        self.assertAlmostEqual(segments["ramp_down"][0], 14_400 / gate.SLEW_PPM_PER_S)
        self.assertEqual(segments["steady_plus_9400"][1], 9400)
        self.assertEqual(segments["steady_minus_5000"][1], -5000)


class GateTest(unittest.TestCase):
    def run_gate(self, backend):
        return gate.run(backend, SCALE)

    def test_oracle_passes_exactly(self):
        report = self.run_gate(mrr.OracleBackend(gate.reference))
        self.assertEqual(report["verdict"], "PASS", report["failures"])
        metrics = report["metrics"]
        self.assertEqual(metrics["worst_abs_error"], 0.0)
        self.assertEqual(metrics["max_delay_jump_samples"], 0.0)
        self.assertTrue(report["accounting"]["exact"])
        self.assertAlmostEqual(metrics["latency_ms"], 256 * 1000 / 48_000, places=2)
        self.assertTrue({"steady_plus_9400", "steady_minus_5000"} <= set(metrics["steady_pitch_ppm"]))

    def test_linear_interpolation_fails_on_quality(self):
        report = self.run_gate(mrr.LinearBackend())
        self.assertEqual(report["verdict"], "FAIL")
        self.assertLess(report["metrics"]["worst_window_snr_db"], 60)
        self.assertTrue(any("SNR" in f for f in report["failures"]))

    def test_drop_hold_fails_on_gaps_and_clicks(self):
        report = self.run_gate(mrr.DropHoldBackend())
        self.assertEqual(report["verdict"], "FAIL")
        reasons = " ".join(report["failures"])
        self.assertIn("gap or duplicate", reasons)
        self.assertIn("error spike", reasons)

    def test_channel_swap_is_detected(self):
        class Swap(mrr.OracleBackend):
            def process(self, left, right, ratio):
                l_out, r_out = super().process(left, right, ratio)
                return r_out, l_out

        report = self.run_gate(Swap(gate.reference))
        self.assertEqual(report["verdict"], "FAIL")
        self.assertTrue(any("crosstalk" in f or "SNR" in f for f in report["failures"]))

    def test_dc_offset_and_clipping_are_detected(self):
        class Offset(mrr.OracleBackend):
            def process(self, left, right, ratio):
                l_out, r_out = super().process(left, right, ratio)
                return [v + 0.01 for v in l_out], r_out

        class Hot(mrr.OracleBackend):
            def process(self, left, right, ratio):
                l_out, r_out = super().process(left, right, ratio)
                return [max(-1.0, min(1.0, v * 2)) for v in l_out], r_out

        self.assertIn("DC offset above limit", self.run_gate(Offset(gate.reference))["failures"])
        self.assertIn("clipping", self.run_gate(Hot(gate.reference))["failures"])

    def test_unexpected_pitch_movement_is_detected(self):
        class Sharp(mrr.OracleBackend):
            def process(self, left, right, ratio):
                return super().process(left, right, ratio * (1 + 20e-6))  # runs 20 ppm fast

        report = self.run_gate(Sharp(gate.reference))
        self.assertEqual(report["verdict"], "FAIL")

    def test_reports_are_deterministic(self):
        a = self.run_gate(mrr.LinearBackend())["metrics"]
        b = self.run_gate(mrr.LinearBackend())["metrics"]
        self.assertEqual(a, b)

    def test_cli_refuses_the_absent_production_backend(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = gate.main(["--backend", "samplerate-sinc-best", "--scale", str(SCALE)])
        report = json.loads(out.getvalue())
        if report["verdict"] == "REFUSED":
            self.assertEqual(code, 2)
            self.assertIn("not installed", report["reason"])
        else:  # installed on this runtime: the gate result is authoritative either way
            self.assertIn(code, (0, 1))


if __name__ == "__main__":
    unittest.main()
