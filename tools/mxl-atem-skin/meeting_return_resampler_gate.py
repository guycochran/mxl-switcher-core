#!/usr/bin/env python3
"""Objective offline quality gate for a meeting-return payload resampler.

**Offline qualification of a resampler backend, not of a Zoom meeting
return.** The audience-side A/V gate remains the acceptance boundary.

The gate drives a backend through ``PayloadResampler`` with a synthetic
stereo reference and a time-varying source:output ratio. The schedule obeys
the scheduler's slew contract and runs unity, a ramp to +9,400 ppm, a hold, a
ramp to −5,000 ppm, a hold, and a ramp back. It varies callback sizes too.
The reference is analytic: a 997 Hz tone on the left and a 3,001 Hz tone on
the right, plus Hann-windowed bursts (7 kHz on the left, 5 kHz on the right,
offset in time). The ideal output is therefore known exactly at every
fractional source time.

Measurements, all machine-readable:

- **Alignment.** The global latency is found coarsely from the first burst.
  A fractional delay is then refined per window by linearised least squares.
- **Per window:** SNR against the ideal (after alignment) and the worst
  error. A click is an error spike.
- **Continuity.** The jump in the fitted delay between windows; a dropped or
  duplicated sample shows as about one sample.
- **Pitch.** The delay slope in steady segments, in ppm; any slope is
  unexpected pitch movement.
- **Whole run:** left/right crosstalk, DC offset, clipping, and exact
  frame accounting.

Exit status: 0 when every threshold passes, 1 when any fails, 2 when the
backend is unavailable (refused).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import meeting_return_resampler as mrr  # noqa: E402

SR = mrr.SAMPLE_RATE
CALLBACK_SIZES = (480, 441, 1024, 160)
SLEW_PPM_PER_S = 20_000.0
THRESHOLDS = {
    "min_window_snr_db": 90.0,
    "max_abs_error": 0.001,           # -60 dBFS: anything above is a click
    "max_delay_jump_samples": 0.25,   # a dropped or duplicated sample is about 1.0
    "max_steady_pitch_ppm": 5.0,
    "max_crosstalk_db": -80.0,
    "max_dc": 1e-4,
    "max_peak": 0.99,
}


def reference(t: float) -> tuple[float, float]:
    """The source signal at fractional source sample ``t``."""

    left = 0.4 * math.sin(2 * math.pi * 997 * t / SR) + _burst(t, 0, 7000, 0.2)
    right = 0.3 * math.sin(2 * math.pi * 3001 * t / SR) + _burst(t, 12_000, 5000, 0.2)
    return left, right


def _burst(t: float, offset: int, freq: float, amp: float, period: int = 24_000, width: int = 96) -> float:
    phase = (t - offset) % period
    if not 0 <= phase < width:
        return 0.0
    window = 0.5 - 0.5 * math.cos(2 * math.pi * phase / width)
    return amp * window * math.sin(2 * math.pi * freq * phase / SR)


def _derivative(t: float) -> tuple[float, float]:
    h = 1e-3
    a, b = reference(t + h), reference(t - h)
    return (a[0] - b[0]) / (2 * h), (a[1] - b[1]) / (2 * h)


def schedule(scale: float = 1.0) -> list[tuple[str, float, float]]:
    """``(label, seconds, ratio_ppm_at_end)``; ramps obey the slew contract (linear in time)."""

    def ramp(ppm_from, ppm_to):
        return abs(ppm_to - ppm_from) / SLEW_PPM_PER_S

    return [
        ("unity", 1.5 * scale, 0.0),
        ("ramp_up", ramp(0, 9400), 9400.0),
        ("steady_plus_9400", 2.5 * scale, 9400.0),
        ("ramp_down", ramp(9400, -5000), -5000.0),
        ("steady_minus_5000", 2.5 * scale, -5000.0),
        ("ramp_back", ramp(-5000, 0), 0.0),
        ("unity_end", 1.0 * scale, 0.0),
    ]


def run(backend, scale: float = 1.0, prime_frames: int = 256) -> dict:
    """Drive ``backend`` through the schedule and measure it against the analytic ideal."""

    adapter = mrr.PayloadResampler(backend, prime_frames=prime_frames)
    out = [[], []]
    tau: list[float] = []      # ideal fractional source time of each output frame (pre-latency)
    labels: list[str] = []
    source_pos = 0
    carry = 0.0
    ppm = 0.0
    produced_requested = 0
    callback = 0
    for label, seconds, ppm_end in schedule(scale):
        ppm_start = ppm
        frames_total = int(seconds * SR)
        done = 0
        while done < frames_total:
            out_n = min(CALLBACK_SIZES[callback % len(CALLBACK_SIZES)], frames_total - done)
            callback += 1
            fraction = (done + out_n) / frames_total
            ppm = ppm_start + (ppm_end - ppm_start) * fraction if label.startswith("ramp") else ppm_end
            rho = 1 + ppm / 1e6  # source samples per output sample
            carry += out_n * rho
            source_n = int(carry)
            carry -= source_n
            src = [reference(source_pos + i) for i in range(source_n)]
            try:
                l_out, r_out = adapter.process([s[0] for s in src], [s[1] for s in src], source_n, out_n)
            except mrr.ResamplerUnavailable as error:
                return {"verdict": "FAIL", "failures": [str(error)], "adapter": adapter.status()}
            out[0].extend(l_out)
            out[1].extend(r_out)
            step = source_n / out_n
            tau.extend(source_pos + k * step for k in range(out_n))
            labels.extend([label] * out_n)
            source_pos += source_n
            produced_requested += out_n
            done += out_n
    report = analyze(out, tau, labels, prime_frames)
    accounting_ok = (adapter.counters["output_frames"] == produced_requested == len(out[0])
                     and adapter.counters["source_frames"] == source_pos)
    report["accounting"] = {"requested_output_frames": produced_requested,
                            "produced_output_frames": len(out[0]),
                            "source_frames_consumed": source_pos, "exact": accounting_ok}
    if not accounting_ok:
        report["failures"].append("frame accounting is not exact")
    report["adapter"] = adapter.status()
    report["verdict"] = "PASS" if not report["failures"] else "FAIL"
    return report


def _coarse_lag(out_left: list, tau: list, search: range) -> int:
    """Integer output-frame lag that best aligns the first burst."""

    centre = next(i for i, t in enumerate(tau) if t >= 24_000) if tau[-1] > 24_000 else 0
    lo, hi = max(0, centre - 200), min(len(tau), centre + 400)
    best, best_err = 0, float("inf")
    for lag in search:
        if hi + lag >= len(out_left):
            break
        err = 0.0
        for i in range(lo, hi):
            err += (out_left[i + lag] - reference(tau[i])[0]) ** 2
        if err < best_err:
            best, best_err = lag, err
    return best


def analyze(out: list, tau: list, labels: list, prime_frames: int, window: int = 2_400) -> dict:
    failures: list[str] = []
    lag = _coarse_lag(out[0], tau, range(0, prime_frames + 512))
    n = len(tau) - lag
    windows = []
    previous_delay = None
    worst_snr, worst_err = float("inf"), 0.0
    delay_jumps = []
    steady: dict[str, list[tuple[float, float]]] = {}
    start = window * 2  # let the backend settle after start-up
    for w0 in range(start, n - window, window):
        idx = range(w0, w0 + window)
        delay = 0.0
        for _ in range(2):  # linearised least squares for a fractional delay (in source samples)
            num = den = 0.0
            for i in idx:
                ideal = reference(tau[i] - delay)
                slope = _derivative(tau[i] - delay)
                for c in range(2):
                    e = out[c][i + lag] - ideal[c]
                    num += -e * slope[c]
                    den += slope[c] * slope[c]
            delay += num / den if den else 0.0
        sig = noise = 0.0
        max_err = 0.0
        for i in idx:
            ideal = reference(tau[i] - delay)
            for c in range(2):
                e = out[c][i + lag] - ideal[c]
                sig += ideal[c] * ideal[c]
                noise += e * e
                max_err = max(max_err, abs(e))
        snr = 10 * math.log10(sig / noise) if noise else 200.0
        label = labels[w0]
        windows.append({"start": w0, "label": label, "snr_db": round(snr, 2), "max_abs_error": round(max_err, 7),
                        "delay_samples": round(delay, 5)})
        worst_snr, worst_err = min(worst_snr, snr), max(worst_err, max_err)
        if previous_delay is not None:
            delay_jumps.append(abs(delay - previous_delay))
        previous_delay = delay
        if label.startswith(("unity", "steady")):
            steady.setdefault(label, []).append((tau[w0], delay))
    pitch = {}
    for label, points in steady.items():
        if len(points) >= 3:
            mt = sum(t for t, _ in points) / len(points)
            md = sum(d for _, d in points) / len(points)
            den = sum((t - mt) ** 2 for t, _ in points)
            pitch[label] = round(sum((t - mt) * (d - md) for t, d in points) / den * 1e6, 3) if den else 0.0
    left, right = out[0][lag:lag + n], out[1][lag:lag + n]
    dc = [sum(ch) / len(ch) for ch in (left, right)]
    peak = max(max(abs(v) for v in left), max(abs(v) for v in right))
    crosstalk = _crosstalk_db(left, right, tau[:n])
    metrics = {
        "latency_output_frames": lag,
        "latency_ms": round((lag + (windows[0]["delay_samples"] if windows else 0)) * 1000 / SR, 3),
        "windows": len(windows),
        "worst_window_snr_db": round(worst_snr, 2),
        "worst_abs_error": round(worst_err, 7),
        "max_delay_jump_samples": round(max(delay_jumps, default=0.0), 5),
        "steady_pitch_ppm": pitch,
        "crosstalk_db": round(crosstalk, 2),
        "dc": [round(v, 7) for v in dc],
        "peak": round(peak, 6),
    }
    t = THRESHOLDS
    if worst_snr < t["min_window_snr_db"]:
        failures.append(f"window SNR {worst_snr:.1f} dB below {t['min_window_snr_db']} dB")
    if worst_err > t["max_abs_error"]:
        failures.append(f"error spike {worst_err:.5f} above {t['max_abs_error']} (click, gap, or duplicate)")
    if metrics["max_delay_jump_samples"] > t["max_delay_jump_samples"]:
        failures.append(f"delay jump {metrics['max_delay_jump_samples']} samples (gap or duplicate)")
    for label, value in pitch.items():
        if abs(value) > t["max_steady_pitch_ppm"]:
            failures.append(f"{label}: unexpected pitch movement {value} ppm")
    if crosstalk > t["max_crosstalk_db"]:
        failures.append(f"crosstalk {crosstalk:.1f} dB above {t['max_crosstalk_db']} dB")
    if max(abs(v) for v in dc) > t["max_dc"]:
        failures.append("DC offset above limit")
    if peak > t["max_peak"]:
        failures.append("clipping")
    worst = sorted(windows, key=lambda w: w["snr_db"])[:5]
    return {"metrics": metrics, "thresholds": THRESHOLDS, "failures": failures, "worst_windows": worst}


def _crosstalk_db(left: list, right: list, tau: list) -> float:
    """How much of the left-only 997 Hz tone appears in the right output (and the reverse), in dB."""

    def projection(signal, freq):
        s = c = 0.0
        for v, t in zip(signal, tau):
            phase = 2 * math.pi * freq * t / SR
            s += v * math.sin(phase)
            c += v * math.cos(phase)
        return math.hypot(s, c) * 2 / len(signal)

    leak = max(projection(right, 997) / 0.4, projection(left, 3001) / 0.3)
    return 20 * math.log10(leak) if leak > 0 else -200.0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--backend", default="samplerate-sinc-best",
                        help="samplerate-sinc-best (production candidate), or a test instrument: "
                             "oracle, linear, drop-hold")
    parser.add_argument("--scale", type=float, default=1.0, help="stretch the steady segments")
    args = parser.parse_args(argv)
    try:
        if args.backend == "oracle":
            backend, info = mrr.OracleBackend(reference), {"backend": "oracle", "test_instrument": True}
        else:
            backend, info = mrr.select_backend(args.backend, allow_unqualified=args.backend in ("linear", "drop-hold"))
    except mrr.ResamplerUnavailable as error:
        print(json.dumps({"verdict": "REFUSED", "reason": str(error)}, indent=2))
        return 2
    report = run(backend, args.scale)
    report["backend"] = info
    print(json.dumps(report, indent=2))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
