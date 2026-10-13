import json
import pathlib
import unittest

from program_clock import (
    AdaptiveResampleController,
    ClockRateEstimator,
    ProgramTimestampAccumulator,
    telemetry,
)


class ClockRateEstimatorTest(unittest.TestCase):
    def test_measures_zoomiso_regression_rate_under_arrival_jitter(self):
        estimator = ClockRateEstimator(window_seconds=20, minimum_span_seconds=1)
        rate = 1.000872452
        for index in range(201):
            wall_ns = index * 50_000_000
            jitter_ns = (0, 2_000_000, -1_000_000, 500_000, -1_500_000)[index % 5]
            observed_ns = wall_ns + (jitter_ns if index else 0)
            media_ns = round(wall_ns * rate)
            estimator.observe(media_ns, observed_ns)

        self.assertTrue(estimator.ready)
        self.assertAlmostEqual(estimator.drift_ppm, 872.452, delta=25)

    def test_timestamp_regression_starts_new_epoch(self):
        estimator = ClockRateEstimator(minimum_span_seconds=0.1)
        estimator.observe(0, 0)
        estimator.observe(1_000_000_000, 1_000_000_000)
        estimator.observe(10_000_000, 2_000_000_000)

        self.assertEqual(estimator.resets, 1)
        self.assertFalse(estimator.ready)

    def test_scheduler_burst_does_not_reset_clock_epoch(self):
        estimator = ClockRateEstimator(minimum_span_seconds=0.1)
        estimator.observe(0, 0)
        estimator.observe(50_000_000, 50_000_000)
        estimator.observe(60_000_000, 50_000_000)
        estimator.observe(200_000_000, 200_000_000)

        self.assertEqual(estimator.resets, 0)
        self.assertEqual(estimator.ignored_samples, 1)
        self.assertAlmostEqual(estimator.rate, 1.0)


class AdaptiveResampleControllerTest(unittest.TestCase):
    def test_fast_source_converges_below_unity_without_hard_drop(self):
        controller = AdaptiveResampleController(smoothing=0.2)
        for _ in range(80):
            controller.update(1.000872452, 120.0)

        self.assertAlmostEqual(controller.ratio, 1 / 1.000872452, delta=2e-8)
        self.assertLess(controller.correction_ppm, -850)

    def test_buffer_feedback_is_bounded(self):
        controller = AdaptiveResampleController(max_correction_ppm=2500, smoothing=1.0)
        high = controller.update(1.0, 10_000)
        low = controller.update(1.0, -10_000)

        self.assertGreaterEqual(high, 0.9975)
        self.assertLessEqual(low, 1.0025)


class ProgramTimestampAccumulatorTest(unittest.TestCase):
    def test_exact_long_term_audio_and_video_cadence(self):
        accumulator = ProgramTimestampAccumulator(epoch_ns=7_000_000_000)

        self.assertEqual(accumulator.audio_pts_ns(48_000), 8_000_000_000)
        self.assertEqual(accumulator.video_pts_ns(30_000), 1_008_000_000_000)
        self.assertEqual(accumulator.av_skew_ns(48_048, 30), 0)

    def test_telemetry_is_json_serializable(self):
        estimator = ClockRateEstimator(minimum_span_seconds=0.1)
        estimator.observe(0, 0)
        estimator.observe(1_000_900_000, 1_000_000_000)
        controller = AdaptiveResampleController(smoothing=1.0)
        controller.update(estimator.rate, 120.0)

        encoded = json.dumps(telemetry(estimator, controller, 120.0, 2_000_000))

        self.assertIn("input_drift_ppm", encoded)
        self.assertIn("av_skew_ms", encoded)


class OutputProfileFixtureTest(unittest.TestCase):
    def test_example_is_public_safe_and_matches_v1_contract(self):
        root = pathlib.Path(__file__).resolve().parents[2]
        profile = json.loads((root / "config/output-profiles.example.json").read_text())
        schema = json.loads((root / "config/output-profiles.schema.json").read_text())

        self.assertEqual(profile["schema_version"], schema["properties"]["schema_version"]["const"])
        self.assertEqual(profile["program"]["audio"]["sample_rate"], 48_000)
        self.assertEqual(profile["program"]["clock"]["authority"], "monotonic")
        self.assertEqual({item["kind"] for item in profile["outputs"]}, {"meeting_return", "rtmp"})
        self.assertNotIn("192.168.", json.dumps(profile))
        self.assertNotIn("20.64.", json.dumps(profile))


if __name__ == "__main__":
    unittest.main()
