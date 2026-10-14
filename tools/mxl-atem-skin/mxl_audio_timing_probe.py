#!/usr/bin/env python3
"""Measure one live MXL Float32 audio flow without modifying it."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import threading
import time


CAPS = (
    "audio/x-raw,format=F32LE,layout=interleaved,rate=48000,"
    "channels=2,channel-mask=(bitmask)0x3"
)
BYTES_PER_SAMPLE_FRAME = 2 * 4


def pipeline_description(domain: str, flow: str) -> str:
    return (
        f"mxlsrc domain={domain} audio-flow-id={flow} automatic-eos=false ! "
        f"{CAPS} ! "
        "appsink name=sink emit-signals=true sync=false "
        "max-buffers=16 drop=true"
    )


def percentile(values: list[float], proportion: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * proportion)
    return ordered[index]


def regression_rate(buffers: list[tuple[int, int, int, int]]) -> float:
    origin_pts = buffers[0][0]
    origin_wall = buffers[0][3]
    pts = [item[0] - origin_pts for item in buffers]
    wall = [item[3] - origin_wall for item in buffers]
    pts_mean = sum(pts) / len(pts)
    wall_mean = sum(wall) / len(wall)
    covariance = sum(
        (wall_value - wall_mean) * (pts_value - pts_mean)
        for wall_value, pts_value in zip(wall, pts)
    )
    variance = sum((wall_value - wall_mean) ** 2 for wall_value in wall)
    return covariance / variance


class Probe:
    def __init__(self, domain: str, flow: str) -> None:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
        self.Gst = Gst
        self.lock = threading.Lock()
        self.buffers: list[tuple[int, int, int, int]] = []
        self.pipeline = Gst.parse_launch(pipeline_description(domain, flow))
        self.sink = self.pipeline.get_by_name("sink")
        self.sink.connect("new-sample", self._sample)

    def _sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return self.Gst.FlowReturn.ERROR
        buffer = sample.get_buffer()
        if buffer.pts != self.Gst.CLOCK_TIME_NONE:
            duration = 0 if buffer.duration == self.Gst.CLOCK_TIME_NONE else int(buffer.duration)
            if duration <= 0:
                duration = buffer.get_size() * 1_000_000_000 // (
                    48_000 * BYTES_PER_SAMPLE_FRAME
                )
            with self.lock:
                self.buffers.append(
                    (int(buffer.pts), duration, buffer.get_size(), time.monotonic_ns())
                )
        return self.Gst.FlowReturn.OK

    def run(self, duration_seconds: float, warmup_seconds: float = 5.0) -> dict[str, object]:
        if self.pipeline.set_state(self.Gst.State.PLAYING) == self.Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("audio timing probe failed to start")
        time.sleep(warmup_seconds)
        with self.lock:
            self.buffers.clear()
        time.sleep(duration_seconds)
        self.pipeline.set_state(self.Gst.State.NULL)
        with self.lock:
            buffers = list(self.buffers)
        if len(buffers) < 2:
            raise RuntimeError(f"audio timing probe received only {len(buffers)} buffers")

        pts_deltas_ms = [
            (current[0] - previous[0]) / 1_000_000.0
            for previous, current in zip(buffers, buffers[1:])
        ]
        arrival_deltas_ms = [
            (current[3] - previous[3]) / 1_000_000.0
            for previous, current in zip(buffers, buffers[1:])
        ]
        discontinuities_ns = []
        for previous, current in zip(buffers, buffers[1:]):
            expected = previous[0] + previous[1]
            discontinuity = current[0] - expected
            if abs(discontinuity) > 1_000:
                discontinuities_ns.append(discontinuity)

        first = buffers[0]
        last = buffers[-1]
        wall_span_ns = last[3] - first[3]
        pts_span_ns = last[0] - first[0]
        rate = regression_rate(buffers)
        endpoint_rate = pts_span_ns / wall_span_ns
        sample_frames = sum(item[2] // BYTES_PER_SAMPLE_FRAME for item in buffers)
        nonfinite = not all(math.isfinite(value) for value in pts_deltas_ms)
        return {
            "state": "ok" if not nonfinite else "invalid",
            "buffers": len(buffers),
            "sample_frames": sample_frames,
            "pts_vs_wall_rate": rate,
            "drift_ppm": (rate - 1.0) * 1_000_000.0,
            "endpoint_rate": endpoint_rate,
            "pts_delta_ms": {
                "minimum": min(pts_deltas_ms),
                "median": statistics.median(pts_deltas_ms),
                "p95": percentile(pts_deltas_ms, 0.95),
                "maximum": max(pts_deltas_ms),
            },
            "arrival_delta_ms": {
                "minimum": min(arrival_deltas_ms),
                "median": statistics.median(arrival_deltas_ms),
                "p95": percentile(arrival_deltas_ms, 0.95),
                "maximum": max(arrival_deltas_ms),
            },
            "discontinuities": len(discontinuities_ns),
            "discontinuity_ns_range": (
                [min(discontinuities_ns), max(discontinuities_ns)]
                if discontinuities_ns
                else [0, 0]
            ),
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", default="/mxl-domain")
    parser.add_argument("--flow", required=True)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--warmup", type=float, default=5.0)
    args = parser.parse_args()
    print(
        json.dumps(
            Probe(args.domain, args.flow).run(args.duration, args.warmup), indent=2
        )
    )


if __name__ == "__main__":
    main()
