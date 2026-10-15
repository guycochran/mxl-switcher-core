import contextlib
import importlib.util
import io
import json
import pathlib
import random
import sys
import tempfile
import unittest
from array import array

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))
ref = importlib.import_module("av_reference")
SPEC = importlib.util.spec_from_file_location("mxl_av_analyze", HERE / "mxl_av_analyze.py")
analyzer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analyzer)

# A realistic absolute grain on an event boundary (MXL index scale, 2026).
BASE_GRAIN = 53_760_658_260
assert BASE_GRAIN % ref.EVENT_PERIOD_FRAMES == 0


def render_frame(grain, width=480, height=270, white=235, black=16, noise=0, seed=0):
    """Draw only what the decoder looks at, scaled like a downscaled capture."""

    frame = bytearray([90]) * (width * height)
    sx = width / ref.REFERENCE_WIDTH
    sy = height / ref.REFERENCE_HEIGHT

    def fill(rect, value):
        x0, y0, x1, y1 = rect
        left, right = round(x0 * sx), round(x1 * sx)
        for y in range(round(y0 * sy), round(y1 * sy)):
            frame[y * width + left:y * width + right] = bytes([value]) * (right - left)

    fill(ref.FLASH_RECT, white if ref.flash_on(grain) else black)
    for rect, bit in ref.code_blocks(grain):
        fill(rect, white if bit else black)
    if noise:
        rng = random.Random(seed)
        for index in range(0, len(frame), 7):
            frame[index] = max(0, min(255, frame[index] + rng.randint(-noise, noise)))
    return bytes(frame)


def capture_audio(first_sample, count, mode="meeting", gain=1.0, noise=0.0, seed=1):
    samples = ref.audio_samples(first_sample, count, mode)
    rng = random.Random(seed)
    if gain != 1.0 or noise:
        for index in range(len(samples)):
            samples[index] = samples[index] * gain + (rng.uniform(-noise, noise) if noise else 0.0)
    return samples


class TimelineTest(unittest.TestCase):
    def test_sample_grid_and_event_codes(self):
        self.assertEqual(ref.SAMPLES_PER_FRAME, 1600)
        self.assertEqual(ref.grain_to_sample(BASE_GRAIN) % ref.EVENT_PERIOD_SAMPLES, 0)
        self.assertTrue(ref.is_event_grain(BASE_GRAIN))
        self.assertFalse(ref.is_event_grain(BASE_GRAIN + 1))
        codes = [ref.event_code(BASE_GRAIN + 30 * n) for n in range(5)]
        self.assertEqual(sorted(set(codes)), [0, 1, 2, 3])
        self.assertEqual(codes[0], codes[4])

    def test_code_wrap_preserves_event_identity(self):
        self.assertEqual(ref.CODE_MODULUS % 120, 0)
        self.assertLess(ref.CODE_MODULUS, 1 << 32)
        last = ref.CODE_MODULUS - 1
        self.assertEqual(ref.wrap_delta(1, last), 2)
        self.assertEqual(ref.wrap_delta(last, 1), -2)
        for grain in (BASE_GRAIN, BASE_GRAIN + 30, BASE_GRAIN + 31, 7 * ref.CODE_MODULUS + 60):
            code = grain % ref.CODE_MODULUS
            self.assertEqual(ref.is_event_grain(code), ref.is_event_grain(grain))
            self.assertEqual(ref.event_code(code), ref.event_code(grain))

    def test_timecode(self):
        self.assertEqual(ref.timecode(30 * 3661 + 7), "01:01:01:07")


class FrameCodeTest(unittest.TestCase):
    def test_round_trip_at_capture_sizes(self):
        for width, height in ((480, 270), (640, 360), (1920, 1080)):
            for grain in (BASE_GRAIN, BASE_GRAIN + 17, ref.CODE_MODULUS - 1):
                with self.subTest(width=width, grain=grain):
                    frame = render_frame(grain, width, height)
                    self.assertEqual(
                        ref.decode_frame_code(frame, width, height), grain % ref.CODE_MODULUS
                    )

    def test_survives_compressed_contrast_and_noise(self):
        frame = render_frame(BASE_GRAIN + 5, 640, 360, white=180, black=70, noise=20, seed=4)
        self.assertEqual(ref.decode_frame_code(frame, 640, 360), (BASE_GRAIN + 5) % ref.CODE_MODULUS)

    def test_rejects_corrupted_bit_and_foreign_picture(self):
        frame = bytearray(render_frame(BASE_GRAIN + 2))
        (x0, y0, x1, y1), bit = ref.code_blocks(BASE_GRAIN + 2)[10]
        sx, sy = 480 / 1920, 270 / 1080
        for y in range(round(y0 * sy), round(y1 * sy)):
            for x in range(round(x0 * sx), round(x1 * sx)):
                frame[y * 480 + x] = 16 if bit else 235
        self.assertIsNone(ref.decode_frame_code(bytes(frame), 480, 270))
        self.assertIsNone(ref.decode_frame_code(bytes([128]) * (480 * 270), 480, 270))


class V210Test(unittest.TestCase):
    def test_luma_round_trip_through_v210_packing(self):
        width, height = 30, 2
        luma = bytes((16 + 7 * index) % 236 for index in range(width * height))
        packed = ref.v210_from_luma(luma, width, height)
        self.assertEqual(len(packed), ref.v210_stride(width) * height)
        stride = ref.v210_stride(width)
        for y in range(height):
            for x in range(width):
                self.assertEqual(ref.v210_luma(packed, stride, x, y), luma[y * width + x])

    def test_decodes_frame_code_from_v210(self):
        width, height = 480, 270
        grain = BASE_GRAIN + 11
        packed = ref.v210_from_luma(render_frame(grain, width, height), width, height)
        self.assertEqual(ref.decode_v210_frame_code(packed, width, height), grain % ref.CODE_MODULUS)
        self.assertEqual(ref.v210_stride(1920), 5120)


class AudioTest(unittest.TestCase):
    def test_burst_starts_on_event_frame_first_sample(self):
        start = ref.grain_to_sample(BASE_GRAIN) - 100
        samples = ref.audio_samples(start, 2000, "meeting")
        left = samples[0::2]
        self.assertTrue(all(value == 0.0 for value in left[:101]))
        self.assertTrue(any(abs(value) > 0.4 for value in left[100:100 + ref.BURST_SAMPLES]))
        self.assertTrue(all(value == 0.0 for value in left[100 + ref.BURST_SAMPLES:]))
        self.assertEqual(list(samples[0::2]), list(samples[1::2]))

    def test_meeting_audio_repeats_every_four_events(self):
        # The publisher caches one four-event cycle and indexes it by sample.
        cycle = ref.EVENT_PERIOD_SAMPLES * len(ref.BURST_FREQUENCIES_HZ)
        start = ref.grain_to_sample(BASE_GRAIN) - 50
        self.assertEqual(
            ref.audio_samples(start, 3000), ref.audio_samples(start % cycle, 3000)
        )

    def test_lossless_counter_is_exact_float32(self):
        start = ref.grain_to_sample(BASE_GRAIN)
        samples = ref.audio_samples(start, 64, "lossless")
        for index in range(64):
            expected = ((start + index) % ref.COUNTER_MODULUS - ref.COUNTER_MODULUS // 2)
            self.assertEqual(samples[2 * index + 1] / ref.COUNTER_SCALE, expected)

    def test_detects_onsets_and_codes_through_gain_and_noise(self):
        lead = 12_345
        first = ref.grain_to_sample(BASE_GRAIN) - lead
        mono = capture_audio(first, 4 * ref.SAMPLE_RATE, gain=0.3, noise=0.01)[0::2]
        events = ref.detect_audio_events(mono)
        self.assertEqual(len(events), 4)
        for n, event in enumerate(events):
            self.assertLessEqual(abs(event.sample - (lead + n * ref.EVENT_PERIOD_SAMPLES)), 2)
            self.assertEqual(event.frequency_index, ref.event_code(BASE_GRAIN + 30 * n))

    def test_skips_burst_already_running_at_capture_start(self):
        first = ref.grain_to_sample(BASE_GRAIN) + 200
        mono = capture_audio(first, 2 * ref.SAMPLE_RATE)[0::2]
        events = ref.detect_audio_events(mono)
        self.assertEqual(len(events), 1)
        self.assertLessEqual(abs(events[0].sample - (ref.EVENT_PERIOD_SAMPLES - 200)), 2)

    def test_counter_classifies_gaps_duplicates_and_regressions(self):
        first = ref.grain_to_sample(BASE_GRAIN) + 777
        right = list(capture_audio(first, 20_000, "lossless", gain=0.9)[1::2])
        clean = ref.analyze_counter(array("f", right))
        self.assertEqual(clean.continuous, 19_999)
        self.assertAlmostEqual(clean.as_dict()["gain"], 0.9, places=4)

        impaired = right[:5000] + right[5100:9000] + [right[8999]] + right[9000:12000] + right[11000:]
        report = ref.analyze_counter(array("f", impaired))
        self.assertEqual(report.gap_events, 1)
        self.assertEqual(report.gap_samples, 100)
        self.assertEqual(report.duplicates, 1)
        self.assertEqual(report.regressions, 1)

    def test_counter_wraps_without_false_discontinuity(self):
        first = ref.COUNTER_MODULUS * 7 - 500
        right = capture_audio(first, 1000, "lossless")[1::2]
        report = ref.analyze_counter(right)
        self.assertEqual(report.continuous, 999)


class VideoCadenceTest(unittest.TestCase):
    def test_repeats_skips_regressions_and_freeze(self):
        grains = [10, 11, 12, 12, 12, 12, 13, 15, None, 16, 14, 17]
        decoded = [(index / 30, grain) for index, grain in enumerate(grains)]
        report, events = ref.analyze_video(decoded)
        self.assertEqual(report.undecodable, 1)
        self.assertEqual(report.repeats, 3)
        self.assertEqual(report.longest_freeze_frames, 4)
        self.assertAlmostEqual(report.longest_freeze_s, 3 / 30)
        self.assertEqual(report.skip_events, 2)
        self.assertEqual(report.skipped_grains, 3)
        self.assertEqual(report.regressions, 1)
        self.assertEqual(events, {})


class SyncTest(unittest.TestCase):
    def test_pairs_by_code_and_measures_drift(self):
        video = {BASE_GRAIN + 30 * n: 10.0 + n for n in range(60)}
        # Audio 100 ms late, growing 200 ppm over the minute.
        audio = [
            ref.AudioEvent(
                sample=round((10.0 + n + 0.1 + n * 200e-6) * ref.SAMPLE_RATE),
                frequency_index=ref.event_code(BASE_GRAIN + 30 * n),
                peak=0.5,
            )
            for n in range(60)
        ]
        pairs, unmatched = ref.pair_events(video, audio)
        self.assertEqual(unmatched, 0)
        summary = ref.sync_summary(pairs)
        self.assertEqual(summary["pairs"], 60)
        self.assertAlmostEqual(summary["offset_ms_min"], 100.0, delta=0.05)
        self.assertAlmostEqual(summary["drift_ppm"], 200.0, delta=1.0)

    def test_pairing_is_unambiguous_beyond_half_a_second(self):
        video = {BASE_GRAIN + 30 * n: float(n) for n in range(8)}
        audio = [
            ref.AudioEvent(round((n + 1.3) * ref.SAMPLE_RATE), ref.event_code(BASE_GRAIN + 30 * n), 0.5)
            for n in range(4)
        ]
        pairs, _ = ref.pair_events(video, audio)
        self.assertTrue(all(abs(p.offset_ms - 1300) < 0.1 for p in pairs))


def write_capture(directory, grains, audio_first_sample, audio_seconds, audio_start_s,
                  mode="meeting", width=480, height=270):
    video_path = pathlib.Path(directory, "video.y8")
    pts_path = pathlib.Path(directory, "video.pts")
    audio_path = pathlib.Path(directory, "audio.f32")
    with open(video_path, "wb") as handle:
        for grain in grains:
            handle.write(render_frame(grain, width, height))
    pts_path.write_text("".join(f"{index / 30:.6f}\n" for index in range(len(grains))))
    samples = ref.audio_samples(audio_first_sample, int(audio_seconds * ref.SAMPLE_RATE), mode)
    audio_path.write_bytes(samples.tobytes())
    return [
        "--video-raw", str(video_path), "--video-pts", str(pts_path),
        "--width", str(width), "--height", str(height),
        "--audio-raw", str(audio_path), "--audio-start-s", str(audio_start_s),
    ]


def run_cli(args):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        status = analyzer.main(args)
    return status, json.loads(output.getvalue())


class CompletenessVerdictTest(unittest.TestCase):
    def report(self, skipped=0, repeats=0, unmatched=0):
        return {
            "video": {"frames": 100, "undecodable": 0, "regressions": 0,
                      "skipped_grains": skipped, "repeats": repeats},
            "audio": {"unmatched_events": unmatched},
            "sync": {"pairs": 5, "offset_ms_min": 0.0, "offset_ms_max": 0.0,
                     "offset_ms_range": 0.0, "drift_ppm": 0.0},
        }

    def test_missing_grains_fail_by_default(self):
        self.assertEqual(ref.verdict(self.report(), ref.Thresholds()), [])
        self.assertIn("video grains missing from capture",
                      ref.verdict(self.report(skipped=1), ref.Thresholds()))

    def test_reduced_rate_paths_must_opt_in(self):
        allowance = ref.Thresholds(max_skipped_grains=None, max_repeats=2)
        self.assertEqual(ref.verdict(self.report(skipped=500), allowance), [])
        self.assertIn("video repeats above limit", ref.verdict(self.report(repeats=3), allowance))

    def test_unmatched_events_limited_to_edges(self):
        self.assertEqual(ref.verdict(self.report(unmatched=2), ref.Thresholds()), [])
        self.assertIn("unmatched audio events above limit",
                      ref.verdict(self.report(unmatched=3), ref.Thresholds()))


class EndToEndTest(unittest.TestCase):
    def test_aligned_capture_passes(self):
        start = BASE_GRAIN - 7
        grains = list(range(start, start + 150))
        with tempfile.TemporaryDirectory() as directory:
            args = write_capture(directory, grains, ref.grain_to_sample(start), 5.0, 0.0)
            status, report = run_cli(args)
        self.assertEqual(status, 0, report)
        self.assertEqual(report["video"]["decoded"], 150)
        self.assertEqual(report["sync"]["pairs"], 5)
        self.assertLess(abs(report["sync"]["offset_ms_median"]), 0.1)

    def test_late_audio_and_frozen_video_fail_with_reasons(self):
        start = BASE_GRAIN - 7
        grains = list(range(start, start + 60)) + [start + 59] * 15 + list(range(start + 75, start + 150))
        with tempfile.TemporaryDirectory() as directory:
            # The capture's audio represents the source 100 ms later than its timestamp claims.
            args = write_capture(directory, grains, ref.grain_to_sample(start) - 4800, 5.0, 0.0)
            status, report = run_cli(args + ["--series"])
        self.assertEqual(status, 1)
        self.assertAlmostEqual(report["sync"]["offset_ms_median"], 100.0, delta=0.1)
        self.assertIn("audio lags video beyond limit", report["failures"])
        self.assertEqual(report["video"]["longest_freeze_frames"], 16)
        self.assertEqual(report["video"]["skipped_grains"], 15)
        self.assertTrue(report["sync_series"])

    def test_pre_decoded_codes_input(self):
        start = BASE_GRAIN - 7
        with tempfile.TemporaryDirectory() as directory:
            codes = pathlib.Path(directory, "video.codes")
            lines = [f"{index / 30:.9f} {(start + index) % ref.CODE_MODULUS}" for index in range(150)]
            lines[40] = f"{40 / 30:.9f} -"
            codes.write_text("\n".join(lines) + "\n")
            audio = pathlib.Path(directory, "audio.f32")
            audio.write_bytes(ref.audio_samples(ref.grain_to_sample(start), 5 * ref.SAMPLE_RATE).tobytes())
            status, report = run_cli([
                "--video-codes", str(codes), "--audio-raw", str(audio), "--audio-start-s", "0",
            ])
        self.assertEqual(status, 0, report)
        self.assertEqual(report["video"]["undecodable"], 1)
        self.assertEqual(report["video"]["skipped_grains"], 0)  # present, just undecodable
        self.assertEqual(report["sync"]["pairs"], 5)

    def test_lossless_counter_reported(self):
        start = BASE_GRAIN - 7
        grains = list(range(start, start + 120))
        with tempfile.TemporaryDirectory() as directory:
            args = write_capture(directory, grains, ref.grain_to_sample(start), 4.0, 0.0, mode="lossless")
            status, report = run_cli(args + ["--counter-channel", "right"])
        self.assertEqual(status, 0, report)
        self.assertEqual(report["counter"]["continuous_steps"], 4 * ref.SAMPLE_RATE - 1)


if __name__ == "__main__":
    unittest.main()
