import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import meeting_return_clock as mc  # noqa: E402
import meeting_return_sim as sim  # noqa: E402

NS = mc.NS


def fill(scheduler, seconds):
    for k in range(int(seconds * 30)):
        scheduler.push_video(k)
    for j in range(int(seconds * 100)):
        scheduler.push_audio(j * 480, 480)


class QueueTest(unittest.TestCase):
    def test_queues_are_bounded_and_overruns_counted(self):
        limits = mc.Limits(video_queue_frames=4, audio_queue_samples=4800)
        s = mc.PairedScheduler(limits)
        fill(s, 2.0)  # 60 frames and 96,000 samples into tiny queues
        status = s.status()
        self.assertEqual(status["queue_depth"], {"video_frames": 4, "audio_samples": 4800})
        self.assertLessEqual(status["queue_max_depth"]["video_frames"], 4)
        self.assertLessEqual(status["queue_max_depth"]["audio_samples"], 4800)
        self.assertEqual(status["counters"]["video_overruns"], 56)
        self.assertEqual(status["counters"]["audio_overrun_samples"], 96_000 - 4800)

    def test_clock_waits_for_the_target_latency_of_both_media(self):
        s = mc.PairedScheduler(mc.Limits(target_latency_ms=80))
        s.push_video(0)
        s.push_audio(0, 480)
        self.assertFalse(s.start(0))
        fill(s, 0.2)
        self.assertTrue(s.start(0))
        self.assertGreaterEqual(s.read, 0)
        self.assertEqual(s.status()["health"], "ok")

    def test_input_gaps_and_overlaps_are_accounted(self):
        s = mc.PairedScheduler()
        s.push_audio(0, 480)
        s.push_audio(960, 480)   # 480 samples never arrived
        s.push_audio(1200, 480)  # overlaps the previous block by 240
        counters = s.status()["counters"]
        self.assertEqual((counters["audio_input_gaps"], counters["audio_input_gap_samples"]), (1, 480))
        self.assertEqual(counters["audio_input_overlap_samples"], 240)

    def test_limits_are_validated(self):
        for bad in ({"video_queue_frames": 1}, {"max_correction_ppm": 0}, {"skew_soft_ms": 200}):
            with self.assertRaises(ValueError):
                mc.Limits(**bad)


class SharedClockTest(unittest.TestCase):
    def test_nominal_pair_is_stable_with_no_corrections(self):
        result = sim.simulate(sim.SimConfig(duration_s=20))
        offsets, status = result["offsets_ms"], result["status"]
        self.assertLess(offsets["range"], 10.0)
        self.assertLess(abs(offsets["drift_ppm"]), 50)
        self.assertEqual((status["counters"]["audio_dropped"], status["counters"]["audio_inserted"]), (0, 0))
        self.assertEqual((status["health"], status["output_allowed"]), ("ok", True))

    def test_measured_audience_shape_regression(self):
        """The audience gate: 30 fps nominal, reduced unique cadence, audio lag growing ~+9,400 ppm."""

        shape = sim.SimConfig(duration_s=20, camera_fps=25.0, mic_rate_ppm=9400)
        naive = sim.simulate(shape, sim.NaiveScheduler())["offsets_ms"]
        self.assertGreater(naive["drift_ppm"], 8000)  # the failure as measured
        self.assertGreater(naive["range"], 150)
        paired = sim.simulate(shape)
        offsets, status = paired["offsets_ms"], paired["status"]
        self.assertLess(abs(offsets["drift_ppm"]), 50)
        self.assertLess(offsets["range"], 50)
        self.assertLess(paired["unique_video_fps"], 26)  # cadence reduced by the camera, not hidden
        self.assertEqual((status["health"], status["output_allowed"]), ("ok", True))

    def test_long_run_stays_inside_bounds(self):
        result = sim.simulate(sim.SimConfig(duration_s=120, camera_fps=25.0, mic_rate_ppm=9400))
        self.assertLess(result["offsets_ms"]["range"], 50)
        self.assertEqual(result["status"]["health"], "ok")
        self.assertLess(result["status"]["queue_max_depth"]["audio_samples"], mc.Limits().audio_queue_samples)


class CorrectionAccountingTest(unittest.TestCase):
    def presented(self, status, config):
        return config.duration_s / (config.mic_period_ms / 1000 * (1 + config.mic_rate_ppm / 1e6)) * 480

    def test_slow_microphone_drops_its_rate_error(self):
        config = sim.SimConfig(duration_s=60, mic_rate_ppm=9400)
        status = sim.simulate(config)["status"]
        dropped = status["counters"]["audio_dropped"]
        expected = self.presented(status, config) * 9400 / 1e6
        self.assertAlmostEqual(dropped / expected, 1.0, delta=0.1)
        self.assertEqual(status["counters"]["audio_inserted"], 0)

    def test_fast_microphone_inserts_its_rate_error(self):
        config = sim.SimConfig(duration_s=60, mic_rate_ppm=-5000)
        status = sim.simulate(config)["status"]
        inserted = status["counters"]["audio_inserted"]
        expected = self.presented(status, config) * 5000 / 1e6
        self.assertAlmostEqual(inserted / expected, 1.0, delta=0.1)
        self.assertEqual(status["counters"]["audio_dropped"], 0)


class FailClosedTest(unittest.TestCase):
    def test_uncorrectable_rate_mismatch_fails_closed_in_both_directions(self):
        for ppm in (30_000, -30_000, 25_000):
            with self.subTest(ppm=ppm):
                result = sim.simulate(sim.SimConfig(duration_s=20, mic_rate_ppm=ppm))
                status = result["status"]
                self.assertEqual((status["health"], status["output_allowed"]), ("failed", False))
                self.assertEqual(status["reason"], "uncorrectable_rate_mismatch")
                first_fail = result["health_trace"].index("failed")
                self.assertLess(first_fail, 500)  # within about five seconds of microphone callbacks
                self.assertTrue(all(h == "failed" for h in result["health_trace"][first_fail:]))  # latched

    def test_near_capacity_is_degraded_not_failed(self):
        status = sim.simulate(sim.SimConfig(duration_s=60, mic_rate_ppm=19_000))["status"]
        self.assertEqual((status["health"], status["output_allowed"]), ("degraded", True))

    def test_single_stalls_recover_but_repeated_stalls_exhaust_the_reanchor_budget(self):
        single = sim.simulate(sim.SimConfig(duration_s=20, stalls=((5.0, 0.3, "mic"),)))["status"]
        self.assertEqual(single["health"], "ok")
        self.assertGreater(single["counters"]["reanchors"], 0)
        self.assertEqual(single["counters"]["discontinuities"], single["counters"]["reanchors"])
        storm = sim.simulate(sim.SimConfig(duration_s=40, stalls=tuple((3.0 + 4 * i, 0.3, "mic")
                                                                         for i in range(8))))["status"]
        self.assertEqual((storm["health"], storm["reason"]), ("failed", "reanchor_budget_exceeded"))

    def test_reanchor_budget_is_a_rolling_window(self):
        status = sim.simulate(sim.SimConfig(duration_s=130, stalls=((5.0, 0.3, "mic"), (70.0, 0.3, "mic"))))["status"]
        self.assertEqual(status["health"], "ok")
        self.assertGreater(status["counters"]["reanchors"], mc.Limits().max_reanchors)  # over the whole run


class DisturbanceTest(unittest.TestCase):
    def test_camera_stall_is_counted_and_sync_holds(self):
        result = sim.simulate(sim.SimConfig(duration_s=20, stalls=((5.0, 0.3, "camera"),)))
        counters = result["status"]["counters"]
        self.assertGreater(counters["video_drops"], 0)
        self.assertLess(result["offsets_ms"]["range"], 10)
        self.assertEqual(result["status"]["health"], "ok")

    def test_upstream_audio_gap_becomes_counted_silence(self):
        result = sim.simulate(sim.SimConfig(duration_s=20, audio_gaps=((6.0, 2400),)))
        counters = result["status"]["counters"]
        self.assertEqual(counters["audio_input_gap_samples"], 2400)
        self.assertGreaterEqual(counters["audio_underrun_samples"], 2400)
        self.assertEqual(result["status"]["health"], "ok")

    def test_producer_stall_is_absorbed_by_the_latency_reservoir(self):
        result = sim.simulate(sim.SimConfig(duration_s=20, stalls=((5.0, 0.05, "producer"),)))
        self.assertEqual(result["status"]["counters"]["audio_underrun_samples"], 0)
        self.assertLess(result["offsets_ms"]["range"], 10)


class ContractTest(unittest.TestCase):
    def test_runs_are_deterministic(self):
        config = sim.SimConfig(duration_s=10, camera_fps=25.0, mic_rate_ppm=9400, seed=7)
        self.assertEqual(sim.simulate(config)["status"], sim.simulate(config)["status"])
        other = sim.simulate(sim.SimConfig(duration_s=10, camera_fps=25.0, mic_rate_ppm=9400, seed=8))
        self.assertNotEqual(sim.simulate(config)["status"]["callback_intervals"],
                            other["status"]["callback_intervals"])

    def test_health_contract(self):
        status = sim.simulate(sim.SimConfig(duration_s=5))["status"]
        for key in ("health", "output_allowed", "reason", "clock_started", "av_skew", "queue_depth",
                    "queue_max_depth", "callback_intervals", "counters", "foundation_only"):
            self.assertIn(key, status)
        self.assertTrue(status["foundation_only"])
        for counter in ("video_drops", "video_repeats", "video_underruns", "audio_dropped", "audio_inserted",
                        "audio_underrun_samples", "audio_overrun_samples", "reanchors", "discontinuities"):
            self.assertIn(counter, status["counters"])
        intervals = status["callback_intervals"]
        self.assertAlmostEqual(intervals["video"]["mean_ms"], 33.33, delta=1)
        self.assertAlmostEqual(intervals["audio"]["mean_ms"], 10.0, delta=0.5)

    def test_before_the_clock_starts_output_is_explicit_underrun(self):
        s = mc.PairedScheduler()
        self.assertEqual(s.on_video_callback(0)["kind"], "starting")
        self.assertEqual(s.on_audio_callback(0, 480)["segments"], [("silence", 480)])
        status = s.status()
        self.assertEqual((status["health"], status["output_allowed"]), ("starting", False))
        self.assertEqual(status["counters"]["pre_start_audio_samples"], 480)
        self.assertEqual(status["counters"]["audio_underrun_samples"], 0)


if __name__ == "__main__":
    unittest.main()
