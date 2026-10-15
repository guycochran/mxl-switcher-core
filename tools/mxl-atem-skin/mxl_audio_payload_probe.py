#!/usr/bin/env python3
"""Payload-aware, read-only probe for one live MXL Float32 audio flow.

``mxl_audio_timing_probe.py`` measures timestamps. That cannot see a writer
that keeps a perfect 48 kHz timeline while inserting silence or re-anchoring.
This probe reads the samples themselves, by absolute MXL index, and reports
per window (default 100 ms):

- RMS and peak per channel;
- the longest digital-silence run (exact ``0.0``) and the longest
  identical-sample run (a held non-zero value);
- abrupt sample-to-sample discontinuities (splices). A jump is counted when
  a step exceeds both an absolute floor and a multiple of the window's mean
  absolute step, so ordinary loud or noisy content is not flagged;
- left/right correlation;
- index continuity: blocks that could not be read are counted, never
  silently skipped.

Each window carries its TAI and UTC time, derived from the MXL index, so
``correlate`` can line windows up with ZoomISO's re-anchor,
silence-insertion, and packet-discard log lines.

Subcommands:

- ``probe``: run in a container with the domain mounted read-only. It never
  writes to MXL.
- ``correlate``: pure and offline. It takes a probe report and an events file
  (``[{"t": ISO-8601 or Unix seconds, "kind": "..."}]``, timestamps and kinds
  only) and reports how often each event kind lands near an anomalous window,
  against the chance rate.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from array import array
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import av_reference as ref  # noqa: E402

SAMPLE_RATE = 48_000
BLOCK = 480
TAI_UTC_OFFSET_S = 37
FLOW_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class ChannelState:
    """Runs and the previous sample carry across block and window boundaries."""

    def __init__(self) -> None:
        self.previous: float | None = None
        self.zero_run = 0
        self.hold_run = 0


class PayloadAnalyzer:
    """Feed planar Float32 blocks by MXL index; emit one summary per window.

    ``silence_ms``: a digital-silence run at least this long inside a window is
    a ``dropout`` when the window otherwise carries signal, or ``silence``
    when the whole window is silent. ``hold_ms``: an identical non-zero run
    this long is a ``hold``. A ``jump`` is a step larger than
    ``max(jump_floor, jump_factor × mean |step|)`` within the window.
    """

    def __init__(self, channels: int = 2, window_ms: float = 100.0, silence_ms: float = 5.0,
                 hold_ms: float = 2.0, jump_floor: float = 0.25, jump_factor: float = 8.0,
                 signal_floor_dbfs: float = -60.0, tai_utc_offset_s: float = TAI_UTC_OFFSET_S) -> None:
        self.channels = channels
        self.window = max(BLOCK, round(window_ms * SAMPLE_RATE / 1000) // BLOCK * BLOCK)
        self.silence_samples = round(silence_ms * SAMPLE_RATE / 1000)
        self.hold_samples = round(hold_ms * SAMPLE_RATE / 1000)
        self.jump_floor, self.jump_factor = jump_floor, jump_factor
        self.signal_floor = 10 ** (signal_floor_dbfs / 20)
        self.tai_utc_offset_s = tai_utc_offset_s
        self.state = [ChannelState() for _ in range(channels)]
        self.windows: list[dict] = []
        self.pending: list[tuple[int, list | None, str | None]] = []
        self.pending_samples = 0
        self.expected_index: int | None = None

    # input -----------------------------------------------------------------------
    def feed(self, end_index: int, channels: list) -> None:
        """Samples ``[end-count, end)`` as one Float32 sequence per channel."""

        count = len(channels[0])
        start = end_index - count
        if self.expected_index is not None and start > self.expected_index:
            self.gap(start, start - self.expected_index, "missing")
        self.expected_index = end_index
        self._push(start, [list(c) for c in channels[: self.channels]], None, count)

    def gap(self, end_index: int, count: int, reason: str) -> None:
        """``count`` samples ending at ``end_index`` could not be read (``late``, ``torn``, ``missing``)."""

        self.expected_index = max(self.expected_index or 0, end_index)
        for state in self.state:  # a gap breaks runs and the jump baseline
            state.previous, state.zero_run, state.hold_run = None, 0, 0
        self._push(end_index - count, None, reason, count)

    def _push(self, start: int, data, reason, count: int) -> None:
        self.pending.append((start, data, reason, count))
        self.pending_samples += count
        while self.pending_samples >= self.window:
            self._emit()

    # analysis --------------------------------------------------------------------
    def _emit(self) -> None:
        taken, needed = [], self.window
        while needed:
            start, data, reason, count = self.pending[0]
            use = min(count, needed)
            taken.append((start, None if data is None else [c[:use] for c in data], reason, use))
            if use == count:
                self.pending.pop(0)
            else:
                rest = None if data is None else [c[use:] for c in data]
                self.pending[0] = (start + use, rest, reason, count - use)
            needed -= use
        self.pending_samples -= self.window
        self.windows.append(self._summarise(taken))

    def _summarise(self, parts) -> dict:
        start = parts[0][0]
        gaps = {}
        for _s, data, reason, count in parts:
            if data is None:
                gaps[reason] = gaps.get(reason, 0) + count
        per_channel = []
        for channel in range(self.channels):
            values = [v for _s, data, _r, _c in parts if data is not None for v in data[channel]]
            per_channel.append(self._channel(channel, parts, values))
        flags = sorted({flag for channel in per_channel for flag in channel.pop("flags")}
                       | ({"gap"} if gaps else set()))
        left = [v for _s, d, _r, _c in parts if d is not None for v in d[0]]
        right = [v for _s, d, _r, _c in parts if d is not None for v in d[min(1, self.channels - 1)]]
        tai_ns = ref.index_to_ns(start, SAMPLE_RATE)
        return {
            "start_index": start,
            "tai_ns": tai_ns,
            "utc": datetime.fromtimestamp(tai_ns / 1e9 - self.tai_utc_offset_s, timezone.utc)
                   .isoformat(timespec="milliseconds"),
            "samples_read": len(left),
            "gaps": gaps,
            "channels": per_channel,
            "lr_correlation": correlation(left, right) if self.channels > 1 else None,
            "flags": flags,
        }

    def _channel(self, channel: int, parts, values: list) -> dict:
        state = self.state[channel]
        rms = math.sqrt(sum(v * v for v in values) / len(values)) if values else 0.0
        peak = max((abs(v) for v in values), default=0.0)
        steps = [abs(b - a) for _s, data, _r, _c in parts if data is not None
                 for a, b in zip(data[channel], data[channel][1:])]
        mean_step = sum(steps) / len(steps) if steps else 0.0
        threshold = max(self.jump_floor, self.jump_factor * mean_step)
        longest_zero = longest_hold = jumps = 0
        largest_jump = 0.0
        silent = 0
        for _s, data, _reason, _count in parts:
            if data is None:
                state.previous, state.zero_run, state.hold_run = None, 0, 0
                continue
            for value in data[channel]:
                if value == 0.0:
                    state.zero_run += 1
                    silent += 1
                else:
                    state.zero_run = 0
                if state.previous is not None and value == state.previous and value != 0.0:
                    state.hold_run += 1
                else:
                    state.hold_run = 1 if value != 0.0 else 0
                if state.previous is not None:
                    step = abs(value - state.previous)
                    largest_jump = max(largest_jump, step)
                    if step > threshold:
                        jumps += 1
                longest_zero = max(longest_zero, state.zero_run)
                longest_hold = max(longest_hold, state.hold_run)
                state.previous = value
        flags = set()
        if longest_zero >= self.silence_samples:
            flags.add("silence" if silent == len(values) else "dropout")
        if longest_hold >= self.hold_samples:
            flags.add("hold")
        if jumps:
            flags.add("jump")
        return {"rms_dbfs": dbfs(rms), "peak_dbfs": dbfs(peak), "signal": rms >= self.signal_floor,
                "longest_zero_run": longest_zero, "longest_hold_run": longest_hold,
                "jumps": jumps, "largest_jump": round(largest_jump, 6), "flags": flags}

    # report ------------------------------------------------------------------------
    def summary(self) -> dict:
        windows = self.windows
        flagged = [w for w in windows if w["flags"]]
        counts: dict[str, int] = {}
        for window in flagged:
            for flag in window["flags"]:
                counts[flag] = counts.get(flag, 0) + 1
        gaps: dict[str, int] = {}
        for window in windows:
            for reason, count in window["gaps"].items():
                gaps[reason] = gaps.get(reason, 0) + count
        return {
            "windows": len(windows),
            "window_ms": self.window * 1000 / SAMPLE_RATE,
            "samples_read": sum(w["samples_read"] for w in windows),
            "gap_samples": gaps,
            "flagged_windows": len(flagged),
            "flag_counts": dict(sorted(counts.items())),
            "longest_zero_run_ms": round(max((c["longest_zero_run"] for w in windows for c in w["channels"]),
                                             default=0) * 1000 / SAMPLE_RATE, 3),
            "signal_windows": sum(1 for w in windows if any(c["signal"] for c in w["channels"])),
        }


def dbfs(value: float) -> float | None:
    return None if value <= 0 else round(20 * math.log10(value), 2)


def correlation(left: list, right: list) -> float | None:
    n = min(len(left), len(right))
    if n < 2:
        return None
    ml, mr = sum(left[:n]) / n, sum(right[:n]) / n
    cov = sum((a - ml) * (b - mr) for a, b in zip(left[:n], right[:n]))
    vl = sum((a - ml) ** 2 for a in left[:n])
    vr = sum((b - mr) ** 2 for b in right[:n])
    if vl == 0 or vr == 0:
        return None
    return round(cov / math.sqrt(vl * vr), 4)


# --- correlation with writer log events -----------------------------------------------
def parse_time(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("Z", "+00:00")
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def correlate(report: dict, events: list, tolerance_s: float = 0.5) -> dict:
    """For each event kind: how many events have an anomalous window within ±tolerance, vs chance."""

    windows = report["windows"]
    if not windows:
        raise ValueError("report has no windows")
    window_s = report["summary"]["window_ms"] / 1000
    times = [(parse_time(w["utc"]), w["flags"]) for w in windows]
    first, last = times[0][0], times[-1][0] + window_s
    flagged_rate = sum(1 for _t, flags in times if flags) / len(times)
    span_windows = max(1, round(2 * tolerance_s / window_s))
    chance = 1 - (1 - flagged_rate) ** span_windows
    by_kind: dict[str, dict] = {}
    for event in events:
        kind = str(event.get("kind") or "unknown")
        moment = parse_time(event["t"])
        entry = by_kind.setdefault(kind, {"events": 0, "in_window": 0, "near_anomaly": 0, "flags": {}})
        entry["events"] += 1
        if not first <= moment <= last:
            continue
        entry["in_window"] += 1
        nearby = [flags for t, flags in times if t - tolerance_s <= moment <= t + window_s + tolerance_s and flags]
        if nearby:
            entry["near_anomaly"] += 1
            for flags in nearby:
                for flag in flags:
                    entry["flags"][flag] = entry["flags"].get(flag, 0) + 1
    for entry in by_kind.values():
        observed = entry["near_anomaly"] / entry["in_window"] if entry["in_window"] else None
        entry["observed_rate"] = None if observed is None else round(observed, 4)
        entry["chance_rate"] = round(chance, 4)
        entry["lift"] = None if observed is None or chance == 0 else round(observed / chance, 2)
        entry["flags"] = dict(sorted(entry["flags"].items()))
    return {"tolerance_s": tolerance_s, "flagged_window_rate": round(flagged_rate, 4),
            "kinds": dict(sorted(by_kind.items()))}


# --- in-container reader ------------------------------------------------------------------
def run_probe(args) -> int:
    import mxl_direct as mxl

    if not FLOW_ID.match(args.flow):
        print(json.dumps({"error": "flow id is not a UUID"}))
        return 2
    if not mxl.flow_exists(args.domain, args.flow):  # the SDK crashes on a missing flow
        print(json.dumps({"error": "flow is not in the domain"}))
        return 1
    api = mxl.load_library(args.library)
    instance = api.mxlCreateInstance(args.domain.encode(), b"")
    probe = mxl.Reader(api, instance, args.flow)
    config = probe.config()
    probe.close()
    rate = config.common.grainRate
    if (rate.numerator, rate.denominator) != (SAMPLE_RATE, 1):
        print(json.dumps({"error": f"flow is {rate.numerator}/{rate.denominator}, not 48 kHz"}))
        return 1
    channels = min(2, config.continuous.channelCount)
    reader = mxl.SampleReader(api, instance, args.flow, config.continuous.bufferLength)
    analyzer = PayloadAnalyzer(channels=channels, window_ms=args.window_ms, silence_ms=args.silence_ms,
                               hold_ms=args.hold_ms, jump_floor=args.jump_floor, jump_factor=args.jump_factor)
    head = reader.head() or BLOCK
    end = head // BLOCK * BLOCK
    stalls = 0
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        status, data, torn = reader.read(end, BLOCK, 200_000_000, channels)
        if status == mxl.MXL_STATUS_OK and not torn and data:
            analyzer.feed(end, [array("f", channel) for channel in data])
            end += BLOCK
        elif status == mxl.MXL_STATUS_OK:
            analyzer.gap(end, BLOCK, "torn")
            end += BLOCK
        elif status == mxl.MXL_ERR_OUT_OF_RANGE_TOO_LATE:
            head = reader.head() or end
            resume = max(end + BLOCK, head // BLOCK * BLOCK)
            analyzer.gap(resume - BLOCK, resume - BLOCK - (end - BLOCK), "late")
            end = resume
        elif status == mxl.MXL_ERR_TIMEOUT:
            stalls += 1
        else:
            analyzer.gap(end, BLOCK, f"status_{status}")
            end += BLOCK
    reader.close()
    api.mxlDestroyInstance(instance)
    report = {"probe": "mxl-audio-payload-probe", "seconds": args.seconds, "channels": channels,
              "stalls": stalls, "summary": analyzer.summary(),
              "windows": analyzer.windows if args.all_windows else [w for w in analyzer.windows if w["flags"]]}
    print(json.dumps(report))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command")
    probe = sub.add_parser("probe", help="(in a read-only container) analyse one audio flow")
    probe.add_argument("--domain", required=True)
    probe.add_argument("--flow", required=True)
    probe.add_argument("--library", default="libmxl.so")
    probe.add_argument("--seconds", type=float, default=60.0)
    probe.add_argument("--window-ms", type=float, default=100.0)
    probe.add_argument("--silence-ms", type=float, default=5.0)
    probe.add_argument("--hold-ms", type=float, default=2.0)
    probe.add_argument("--jump-floor", type=float, default=0.25)
    probe.add_argument("--jump-factor", type=float, default=8.0)
    probe.add_argument("--all-windows", action="store_true", help="report every window, not only flagged ones")
    corr = sub.add_parser("correlate", help="(offline) line probe windows up with writer log events")
    corr.add_argument("--report", required=True, help="probe JSON produced with --all-windows")
    corr.add_argument("--events", required=True, help='JSON list of {"t": ..., "kind": ...}')
    corr.add_argument("--tolerance-s", type=float, default=0.5)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "probe":
        return run_probe(args)
    if args.command == "correlate":
        try:
            report = json.loads(Path(args.report).read_text())
            events = json.loads(Path(args.events).read_text())
            if not isinstance(events, list):
                raise ValueError("events must be a JSON list")
            print(json.dumps(correlate(report, events, args.tolerance_s), indent=2))
        except (OSError, ValueError, KeyError) as error:
            print(json.dumps({"error": str(error)}), file=sys.stderr)
            return 2
        return 0
    build_parser().print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
