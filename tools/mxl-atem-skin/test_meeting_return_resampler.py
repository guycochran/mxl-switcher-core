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


class _Causal:
    """Test-local exact resampler with step ratio changes and a filter look-ahead (no module helpers)."""

    def __init__(self, latency=0, smooth=False, gap_at=None):
        self.latency, self.smooth, self.gap_at = latency, smooth, gap_at
        self.position, self.available, self.last = 0.0, 0, None
        self.ratios = []

    def reset(self):
        self.__init__(self.latency, self.smooth, self.gap_at)

    def process(self, left, right, ratio):
        self.ratios.append(ratio)
        if self.gap_at is not None and self.available + len(left) > self.gap_at:
            self.position += 1  # one source sample never played
            self.gap_at = None
        self.available += len(left)
        start = self.last if (self.smooth and self.last is not None) else ratio
        expected = max(1, round(len(left) * ratio))
        out = [[], []]
        k = 0
        while self.position < self.available - 1 - self.latency:
            current = start + (ratio - start) * min(1.0, k / expected)
            left_v, right_v = gate.reference(self.position)
            out[0].append(left_v)
            out[1].append(right_v)
            self.position += 1.0 / current
            k += 1
        self.last = ratio
        return out[0], out[1]


class DynamicRatioRegressionTest(unittest.TestCase):
    """Root cause of the isolated libsamplerate FAIL at 225b5b9 (9 dB, 2.3-sample jumps).

    1. The adapter passed the backend output_n/source_n, quantised to whole samples per
       callback, so its pitch jittered about ±0.2% call to call.
    2. The gate's ideal changed ratio at the scheduler's callback boundaries and refit a
       small delay from zero each window. A correct resampler changes ratio at its own
       latency-shifted point, so the old gate failed even an exact one.
    """

    def test_exact_step_resampler_with_filter_latency_passes(self):
        for latency in (143, 230):
            with self.subTest(latency=latency):
                report = gate.run(_Causal(latency=latency), SCALE)
                self.assertEqual(report["verdict"], "PASS", report["failures"])
                self.assertAlmostEqual(report["metrics"]["switch_latency_source_frames"], latency, delta=0.5)

    def test_backend_receives_the_continuous_ratio(self):
        backend = _Causal()
        gate.run(backend, SCALE)
        steady = backend.ratios[-8:]  # inside the closing unity segment
        self.assertEqual(len(set(steady)), 1)  # no per-callback quantisation jitter
        self.assertEqual(steady[0], 1.0)
        plus = {round(r, 12) for r in backend.ratios if abs(r - 1 / 1.0094) < 1e-9}
        self.assertEqual(len(plus), 1)

    def test_quantised_ratio_is_only_a_fallback(self):
        recorded = []

        class Recorder(mrr.OracleBackend):
            def process(self, left, right, ratio):
                recorded.append(ratio)
                return super().process(left, right, ratio)

        adapter = mrr.PayloadResampler(Recorder(gate.reference))
        adapter.process([0.0] * 485, [0.0] * 485, 485, 480)
        adapter.process([0.0] * 484, [0.0] * 484, 484, 480, source_per_output=1.0094)
        self.assertAlmostEqual(recorded[0], 480 / 485)
        self.assertAlmostEqual(recorded[1], 1 / 1.0094)

    def test_smoothing_backend_fails_honestly(self):
        report = gate.run(_Causal(latency=143, smooth=True), SCALE)
        self.assertEqual(report["verdict"], "FAIL")
        self.assertTrue(any("SNR" in f for f in report["failures"]))

    def test_a_single_sample_gap_still_fails(self):
        report = gate.run(_Causal(latency=143, gap_at=int(48_000 * 1.6 * SCALE / 0.4)), SCALE)
        self.assertEqual(report["verdict"], "FAIL")
        self.assertTrue(any("error spike" in f for f in report["failures"]))

    def test_scheduler_segments_carry_the_continuous_ratio(self):
        import meeting_return_clock as mc
        import meeting_return_sim as sim

        scheduler = mc.PairedScheduler(mc.Limits(correction="ratio"))
        seen = []
        original = scheduler.on_audio_callback

        def capture(t, n):
            result = original(t, n)
            seen.extend(seg for seg in result["segments"] if seg[0] == "resampled")
            return result

        scheduler.on_audio_callback = capture
        sim.simulate(sim.SimConfig(duration_s=3, mic_rate_ppm=9400), scheduler)
        self.assertTrue(seen)
        self.assertTrue(all(len(seg) == 5 for seg in seen))
        self.assertAlmostEqual(seen[-1][4], 1 + scheduler.rate.applied_ppm / 1e6)


class SamplerateBackendTest(unittest.TestCase):
    """The production adapter, against a fake python-samplerate (no dependency installed)."""

    class FakeArray:
        def __init__(self, rows):
            self.rows = rows

        def __getitem__(self, key):
            _, column = key
            return [row[column] for row in self.rows]

    class FakeNumpy:
        float32 = "float32"

        @staticmethod
        def asarray(values, dtype=None):
            return list(values)

        @classmethod
        def column_stack(cls, columns):
            return list(zip(*columns))

    def fake_module(self, with_set_ratio=True):
        calls = []
        outer = self

        class Resampler:
            def __init__(self, converter, channels):
                calls.append(("init", converter, channels))

            def process(self, frames, ratio, end_of_input=False):
                calls.append(("process", ratio, len(frames)))
                return outer.FakeArray([row for row in frames][: int(len(frames) * ratio)])

        if with_set_ratio:
            Resampler.set_ratio = lambda self, ratio: calls.append(("set_ratio", ratio))
        return type("samplerate", (), {"Resampler": Resampler}), calls

    def test_ratio_is_stepped_before_every_process(self):
        module, calls = self.fake_module()
        backend = mrr.SamplerateBackend(module=module, numpy_module=self.FakeNumpy)
        backend.process([0.1] * 480, [0.2] * 480, 0.99)
        backend.process([0.1] * 480, [0.2] * 480, 0.98)
        sequence = [c[0] for c in calls]
        self.assertEqual(sequence, ["init", "set_ratio", "process", "set_ratio", "process"])
        self.assertEqual([c[1] for c in calls if c[0] == "set_ratio"], [0.99, 0.98])
        self.assertEqual(calls[0][1:], ("sinc_best", 2))

    def test_wrapper_without_step_ratio_is_refused(self):
        module, _ = self.fake_module(with_set_ratio=False)
        with self.assertRaises(ImportError):
            mrr.SamplerateBackend(module=module, numpy_module=self.FakeNumpy)


if __name__ == "__main__":
    unittest.main()
