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

- **Ideal.** An exact, causal, step-mode resampler: each call's ratio takes
  effect once its input is used, minus one constant switch latency (filter
  look-ahead) fitted from the first ramp and reported. That is how any
  correct streaming resampler behaves, so ratio changes are not expected at
  the scheduler's callback boundaries. A single scalar cannot absorb
  distortion, gaps, or pitch error. A backend that *smooths* each ratio change
  across a call does not match, which is why the production backend must
  step the ratio (``src_set_ratio``).
- **Alignment.** The global latency is found coarsely from the first burst.
  Each window then fits a continuous delay spline (a knot every 120 frames,
  curvature-penalised) by Gauss-Newton, warm-started from the previous
  window's end (and a ±8-sample coarse search for the first). So a backend that smooths ratio changes
  across a call, as libsamplerate's ``src_process`` does, is measured for
  distortion, not penalised for a smooth timing excursion. That excursion is
  bounded separately by ``max_timing_deviation_samples``.
- **Per window:** SNR against the ideal (after alignment) and the worst
  error. A click is an error spike.
- **Continuity.** The step in the fitted delay where adjacent windows meet;
  a dropped or duplicated sample shows as about one sample.
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
    # Bounded timing excursion against the per-callback ideal. A backend that smooths each ratio
    # change across a call (libsamplerate's documented src_process behaviour) leads or lags by a
    # few samples after a ramp; one that ignores ratio changes drifts by hundreds.
    "max_timing_deviation_samples": 8.0,
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
    calls: list[tuple[int, float, str]] = []  # (source frames supplied, source:output ratio, segment)
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
                l_out, r_out = adapter.process([s[0] for s in src], [s[1] for s in src], source_n, out_n,
                                               source_per_output=rho)
            except mrr.ResamplerUnavailable as error:
                return {"verdict": "FAIL", "failures": [str(error)], "adapter": adapter.status()}
            out[0].extend(l_out)
            out[1].extend(r_out)
            calls.append((source_n, rho, label))
            source_pos += source_n
            produced_requested += out_n
            done += out_n
    report = analyze(out, calls, prime_frames)
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


def ideal_times(calls: list, switch_latency: float = 0.0) -> tuple[list, list]:
    """Source time of every output of an exact, causal, step-mode resampler, and its segment label.

    Each call supplies input and sets the ratio. The resampler emits outputs
    ``rho`` source samples apart, and only while it has input
    ``switch_latency`` frames beyond the output (filter look-ahead). So a ratio
    change takes effect at the backend's own, latency-shifted point, as in any
    real streaming resampler, rather than at the scheduler's callback
    boundaries.
    """

    position, available = 0.0, 0
    times, labels = [], []
    for source_n, rho, label in calls:
        available += source_n
        while position < available - 1 - switch_latency:
            times.append(position)
            labels.append(label)
            position += rho
    return times, labels


def _fit_switch_latency(out_left: list, calls: list, lag: int) -> float:
    """The backend's constant ratio-switch latency (source frames), fitted over the first ramp."""

    times0, labels0 = ideal_times(calls, 0.0)
    try:
        start = labels0.index("ramp_up")
    except ValueError:
        return 0.0
    span = range(start, min(start + int(0.5 * SR), len(times0) - 1 - lag), 3)

    def error(latency: float) -> float:
        times, _ = ideal_times(calls, latency)
        total = 0.0
        for j in span:
            if j >= len(times) or j + lag >= len(out_left):
                break
            total += (out_left[j + lag] - reference(times[j])[0]) ** 2
        return total

    best = min(range(0, 641, 8), key=error)
    best = min((best + k for k in range(-8, 9) if best + k >= 0), key=error)
    return min((best + k / 4 for k in range(-4, 5) if best + k / 4 >= 0), key=error)


def _coarse_lag(out_left: list, tau: list, search: range) -> int:
    """Integer output-frame lag that best aligns the burst at source time 0.

    That burst is always inside the opening unity segment, where no ratio change
    or switch latency can shift the timing, so the lag is unambiguous.
    """

    lo, hi = 0, min(len(tau), 600)
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


def analyze(out: list, calls: list, prime_frames: int, window: int = 2_400) -> dict:
    failures: list[str] = []
    times0, _ = ideal_times(calls, 0.0)
    lag = _coarse_lag(out[0], times0, range(0, prime_frames + 512))
    switch_latency = _fit_switch_latency(out[0], calls, lag)
    tau, labels = ideal_times(calls, switch_latency)
    n = min(len(tau), len(out[0]) - lag)
    windows = []
    worst_snr, worst_err = float("inf"), 0.0
    delay_jumps = []
    steady: dict[str, list[tuple[float, float]]] = {}
    start = window * 2  # let the backend settle after start-up
    previous = None  # (offset, trend) of the previous window
    for w0 in range(start, n - window, window):
        idx = range(w0, w0 + window)
        if previous is None:
            knots = [_coarse_delay(out, tau, lag, idx)] * (KNOTS + 1)
        else:  # warm start at the previous window's end: timing is continuous unless something broke
            knots = [previous[-1]] * (KNOTS + 1)
        knots = _fit_delay(out, tau, lag, idx, knots)
        offset, trend = sum(knots) / len(knots), knots[-1] - knots[0]
        sig = noise = 0.0
        max_err = 0.0
        for i in idx:
            ideal = reference(tau[i] - _spline_at(knots, i, w0, window))
            for c in range(2):
                e = out[c][i + lag] - ideal[c]
                sig += ideal[c] * ideal[c]
                noise += e * e
                max_err = max(max_err, abs(e))
        snr = 10 * math.log10(sig / noise) if noise else 200.0
        label = labels[w0]
        windows.append({"start": w0, "label": label, "snr_db": round(snr, 2), "max_abs_error": round(max_err, 7),
                        "delay_samples": round(offset, 5), "delay_trend_samples": round(trend, 5)})
        worst_snr, worst_err = min(worst_snr, snr), max(worst_err, max_err)
        if previous is not None:  # continuity where the windows meet: a gap or duplicate is a step here
            delay_jumps.append(abs(knots[0] - previous[-1]))
        previous = knots
        if label.startswith(("unity", "steady")):
            steady.setdefault(label, []).append((tau[w0], offset))
    unity = sorted(d for _, d in steady.get("unity", [])) or [w["delay_samples"] for w in windows[:1]]
    reference_delay = unity[len(unity) // 2] if unity else 0.0
    timing_deviation = max((abs(w["delay_samples"] - reference_delay) for w in windows), default=0.0)
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
        "switch_latency_source_frames": switch_latency,
        "latency_ms": round((lag + (windows[0]["delay_samples"] if windows else 0)) * 1000 / SR, 3),
        "windows": len(windows),
        "worst_window_snr_db": round(worst_snr, 2),
        "worst_abs_error": round(worst_err, 7),
        "max_delay_jump_samples": round(max(delay_jumps, default=0.0), 5),
        "max_timing_deviation_samples": round(timing_deviation, 5),
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
    if timing_deviation > t["max_timing_deviation_samples"]:
        failures.append(f"timing deviation {timing_deviation:.2f} samples from the ratio schedule")
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


KNOTS = 20            # delay knots per 2,400-frame window: one every 120 frames
SMOOTHNESS = 1.0      # curvature penalty: smooth timing is free, a step (gap, duplicate) is not


def _spline_at(knots: list, i: int, w0: int, window: int) -> float:
    """Delay (source samples) at output frame ``i``: linear between equally spaced knots."""

    x = (i - w0) / window * KNOTS
    k = min(int(x), KNOTS - 1)
    f = x - k
    return knots[k] * (1 - f) + knots[k + 1] * f


def _window_error(out, tau, lag, idx, delay: float, stride=1) -> float:
    total = 0.0
    for i in range(idx.start, idx.stop, stride):
        ideal = reference(tau[i] - delay)
        total += (out[0][i + lag] - ideal[0]) ** 2 + (out[1][i + lag] - ideal[1]) ** 2
    return total


def _coarse_delay(out, tau, lag, idx) -> float:
    """First window only: search ±8 samples in 1/4 steps so the fit starts inside its linear range."""

    candidates = [k / 4 for k in range(-32, 33)]
    return min(candidates, key=lambda d: _window_error(out, tau, lag, idx, d, stride=4))


def _solve(matrix: list, rhs: list) -> list:
    """Gaussian elimination with partial pivoting (small dense systems)."""

    n = len(rhs)
    a = [row[:] + [rhs[r]] for r, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if not a[pivot][col]:
            return [0.0] * n
        a[col], a[pivot] = a[pivot], a[col]
        for r in range(col + 1, n):
            factor = a[r][col] / a[col][col]
            if factor:
                for c in range(col, n + 1):
                    a[r][c] -= factor * a[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (a[r][n] - sum(a[r][c] * x[c] for c in range(r + 1, n))) / a[r][r]
    return x


def _fit_delay(out, tau, lag, idx, knots: list, iterations: int = 4) -> list:
    """Gauss-Newton fit of a continuous, curvature-penalised delay spline over one window.

    Timing that varies smoothly, such as a backend switching ratio at its own
    latency-shifted boundary, is fitted; the waveform error that remains is
    distortion. A 1-sample step (gap or duplicate) would need a large
    curvature and cannot be absorbed, so it stays as error and as a boundary
    jump.
    """

    w0, window = idx.start, len(idx)
    n = KNOTS + 1
    knots = list(knots)
    for _ in range(iterations):
        h = [[0.0] * n for _ in range(n)]
        g = [0.0] * n
        for i in idx:
            x = (i - w0) / window * KNOTS
            k = min(int(x), KNOTS - 1)
            f = x - k
            t = tau[i] - (knots[k] * (1 - f) + knots[k + 1] * f)
            ideal, slope = reference(t), _derivative(t)
            for c in range(2):
                e = out[c][i + lag] - ideal[c]
                d = slope[c]  # d(error)/d(delay)
                wa, wb = d * (1 - f), d * f
                h[k][k] += wa * wa
                h[k][k + 1] += wa * wb
                h[k + 1][k] += wa * wb
                h[k + 1][k + 1] += wb * wb
                g[k] -= e * wa
                g[k + 1] -= e * wb
        scale = SMOOTHNESS * max(1e-12, sum(h[j][j] for j in range(n)) / n)
        for j in range(1, n - 1):  # penalise second differences (curvature) of the knots
            row = {j - 1: 1.0, j: -2.0, j + 1: 1.0}
            curvature = knots[j - 1] - 2 * knots[j] + knots[j + 1]
            for a_idx, a_w in row.items():
                g[a_idx] -= scale * a_w * curvature
                for b_idx, b_w in row.items():
                    h[a_idx][b_idx] += scale * a_w * b_w
        step = _solve(h, g)
        knots = [kv + sv for kv, sv in zip(knots, step)]
        if max(abs(sv) for sv in step) < 1e-7:
            break
    return knots


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
