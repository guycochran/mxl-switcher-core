import contextlib
import io
import json
import math
import os
import pathlib
import random
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import mxl_audio_payload_probe as probe  # noqa: E402
import mxl_direct as mxl  # noqa: E402

RATE = probe.SAMPLE_RATE
BLOCK = probe.BLOCK
START = 480 * 3_000_000  # an arbitrary block-aligned MXL index


def sine(n0, count, freq=1000.0, amp=0.5, phase=0.0):
    return [amp * math.sin(2 * math.pi * freq * (n0 + i) / RATE + phase) for i in range(count)]


def feed_signal(analyzer, samples_left, samples_right=None, start=START):
    samples_right = samples_left if samples_right is None else samples_right
    for offset in range(0, len(samples_left), BLOCK):
        end = start + offset + BLOCK
        analyzer.feed(end, [samples_left[offset:offset + BLOCK], samples_right[offset:offset + BLOCK]])


class AnalyzerTest(unittest.TestCase):
    def analyzer(self, **kwargs):
        return probe.PayloadAnalyzer(channels=2, window_ms=100.0, **kwargs)

    def test_clean_tone_has_no_flags(self):
        a = self.analyzer()
        feed_signal(a, sine(0, 4800 * 5))
        self.assertEqual(len(a.windows), 5)
        for window in a.windows:
            self.assertEqual(window["flags"], [])
            self.assertEqual(window["lr_correlation"], 1.0)
            self.assertAlmostEqual(window["channels"][0]["rms_dbfs"], -9.03, places=1)
            self.assertEqual(window["samples_read"], 4800)
        self.assertEqual(a.summary()["flagged_windows"], 0)

    def test_inserted_silence_inside_signal_is_a_dropout(self):
        a = self.analyzer()
        signal = sine(0, 4800 * 3)
        signal[4800 + 1000:4800 + 1480] = [0.0] * 480  # 10 ms of writer-inserted silence
        feed_signal(a, signal)
        flagged = a.windows[1]
        self.assertIn("dropout", flagged["flags"])
        self.assertEqual(flagged["channels"][0]["longest_zero_run"], 480)
        self.assertEqual(a.windows[0]["flags"], [])
        self.assertEqual(a.windows[2]["flags"], [])

    def test_whole_window_silence_is_silence_not_dropout(self):
        a = self.analyzer()
        feed_signal(a, [0.0] * 4800)
        self.assertEqual(a.windows[0]["flags"], ["silence"])
        self.assertIsNone(a.windows[0]["channels"][0]["rms_dbfs"])

    def test_short_zero_crossings_are_not_silence(self):
        a = self.analyzer()
        signal = sine(0, 4800)
        signal[100:104] = [0.0] * 4  # well under 5 ms
        feed_signal(a, signal)
        self.assertNotIn("dropout", a.windows[0]["flags"])

    def test_held_value_is_a_hold(self):
        a = self.analyzer()
        signal = sine(0, 4800)
        signal[2000:2000 + 144] = [signal[2000]] * 144  # 3 ms frozen sample
        feed_signal(a, signal)
        self.assertIn("hold", a.windows[0]["flags"])
        self.assertGreaterEqual(a.windows[0]["channels"][0]["longest_hold_run"], 144)

    def test_phase_splice_is_a_jump(self):
        a = self.analyzer()
        first = sine(0, 2412)  # 2412 is a tone peak (a quarter period past a zero crossing)
        second = sine(2412, 2388, phase=math.pi)  # a re-anchor that lands half a cycle off
        feed_signal(a, first + second)
        self.assertIn("jump", a.windows[0]["flags"])
        self.assertGreater(a.windows[0]["channels"][0]["largest_jump"], 0.5)

    def test_noise_is_not_mistaken_for_jumps(self):
        rng = random.Random(7)
        a = self.analyzer()
        noise = [rng.gauss(0, 0.1) for _ in range(4800 * 3)]
        feed_signal(a, noise, [rng.gauss(0, 0.1) for _ in range(4800 * 3)])
        self.assertEqual(sum("jump" in w["flags"] for w in a.windows), 0)
        self.assertLess(abs(a.windows[0]["lr_correlation"]), 0.1)

    def test_zero_run_spanning_blocks_is_measured_in_full(self):
        a = self.analyzer()
        signal = sine(0, 4800)
        signal[BLOCK - 200:BLOCK + 200] = [0.0] * 400  # crosses a 480-sample block boundary
        feed_signal(a, signal)
        self.assertEqual(a.windows[0]["channels"][0]["longest_zero_run"], 400)

    def test_missing_blocks_are_counted_and_break_runs(self):
        a = self.analyzer()
        zeros = [0.0] * BLOCK
        a.feed(START + BLOCK, [zeros, zeros])
        # one block never read: the next block arrives one block later
        for k in range(3, 11):
            a.feed(START + k * BLOCK, [zeros, zeros])
        window = a.windows[0]
        self.assertEqual(window["gaps"], {"missing": BLOCK})
        self.assertIn("gap", window["flags"])
        self.assertEqual(window["samples_read"], 4800 - BLOCK)
        self.assertLess(window["channels"][0]["longest_zero_run"], 9 * BLOCK)  # not merged across the gap

    def test_explicit_late_gap(self):
        a = self.analyzer()
        a.feed(START + BLOCK, [sine(0, BLOCK), sine(0, BLOCK)])
        a.gap(START + 4 * BLOCK, 3 * BLOCK, "late")
        feed_signal(a, sine(4 * BLOCK, 6 * BLOCK), start=START + 4 * BLOCK)
        self.assertEqual(a.windows[0]["gaps"], {"late": 3 * BLOCK})
        self.assertEqual(a.summary()["gap_samples"], {"late": 3 * BLOCK})

    def test_window_times_follow_the_mxl_index(self):
        a = self.analyzer()
        feed_signal(a, sine(0, 4800 * 2))
        tai = a.windows[1]["tai_ns"]
        self.assertEqual(tai, probe.ref.index_to_ns(START + 4800, RATE))
        utc = probe.parse_time(a.windows[1]["utc"])
        self.assertAlmostEqual(utc, tai / 1e9 - 37, places=2)


class CorrelateTest(unittest.TestCase):
    def report(self):
        a = probe.PayloadAnalyzer(channels=2)
        signal = sine(0, 4800 * 50)
        for k in (10, 30):  # dropouts in windows 10 and 30
            signal[4800 * k + 100:4800 * k + 700] = [0.0] * 600
        feed_signal(a, signal)
        return {"summary": a.summary(), "windows": a.windows}

    def test_events_near_anomalies_show_lift(self):
        report = self.report()
        w10, w30, w20 = (probe.parse_time(report["windows"][k]["utc"]) for k in (10, 30, 20))
        events = [{"t": w10 + 0.02, "kind": "silence_insert"}, {"t": w30 + 0.05, "kind": "silence_insert"},
                  {"t": w20, "kind": "packet_discard"}, {"t": w20 - 9999, "kind": "packet_discard"}]
        result = probe.correlate(report, events, tolerance_s=0.1)
        silence = result["kinds"]["silence_insert"]
        self.assertEqual((silence["events"], silence["in_window"], silence["near_anomaly"]), (2, 2, 2))
        self.assertGreater(silence["lift"], 5)
        self.assertEqual(silence["flags"].get("dropout"), 2)
        discard = result["kinds"]["packet_discard"]
        self.assertEqual((discard["events"], discard["in_window"], discard["near_anomaly"]), (2, 1, 0))

    def test_time_formats(self):
        self.assertEqual(probe.parse_time("2026-10-15T23:29:16Z"), probe.parse_time("2026-10-15T23:29:16+00:00"))
        self.assertEqual(probe.parse_time(1.5), 1.5)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            report, events = pathlib.Path(tmp, "r.json"), pathlib.Path(tmp, "e.json")
            report.write_text(json.dumps(self.report()))
            events.write_text(json.dumps([{"t": 0, "kind": "x"}]))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(probe.main(["correlate", "--report", str(report), "--events", str(events)]), 0)
            self.assertEqual(json.loads(out.getvalue())["kinds"]["x"]["in_window"], 0)
            events.write_text(json.dumps({"not": "a list"}))
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(probe.main(["correlate", "--report", str(report), "--events", str(events)]), 2)


class ReaderGuardTest(unittest.TestCase):
    def test_missing_flow_never_reaches_the_sdk(self):
        original = mxl.load_library
        mxl.load_library = lambda *_: self.fail("the SDK was loaded for a missing flow")
        self.addCleanup(setattr, mxl, "load_library", original)
        with tempfile.TemporaryDirectory() as domain:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = probe.run_probe(SimpleNamespace(flow="00000000-0000-4000-8000-0000000000e1", domain=domain,
                                                       library="x"))
        self.assertEqual(code, 1)
        self.assertIn("not in the domain", out.getvalue())

    def test_non_uuid_flow_is_refused(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(probe.run_probe(SimpleNamespace(flow="../x", domain="/nowhere", library="x")), 2)


if __name__ == "__main__":
    unittest.main()
