#!/usr/bin/env python3
"""Deterministic callback-jitter simulator for the paired meeting-return clock.

**Offline only.** It models the boundary the audience gate localized: an
exact Program Master pair, produced on the MXL timeline, is pulled by two
independent consumers. One is a virtual camera at about 30 callbacks/s, the
other a virtual microphone requesting 480 samples about every 10 ms. Each
consumer can independently have:

- a clock-rate error (ppm);
- callback jitter;
- stalls (callbacks bunched after a pause);
- upstream producer stalls and audio input gaps.

Everything runs on integer host nanoseconds from a seeded RNG, with no
sleeping and no wall clock, so a run is reproducible bit for bit.

The audience-side measurement matches the analyzer: for every frame shown,
the time its content is **heard** minus the time it is **first seen**. A
positive value means the audio is late. A fitted slope of that offset over
time gives the drift, in ppm.
"""

from __future__ import annotations

import bisect
import dataclasses
import heapq
import random
import statistics

import meeting_return_clock as clock

NS = clock.NS
BLOCK = 480


@dataclasses.dataclass(frozen=True)
class SimConfig:
    duration_s: float = 20.0
    seed: int = 1
    camera_fps: float = 30.0
    camera_rate_ppm: float = 0.0
    camera_jitter_ms: float = 2.0
    mic_period_ms: float = 10.0
    mic_rate_ppm: float = 0.0
    mic_jitter_ms: float = 1.0
    producer_jitter_ms: float = 0.5
    stalls: tuple = ()        # (start_s, duration_s, "camera" | "mic" | "producer")
    audio_gaps: tuple = ()    # (at_s, missing_samples): an upstream writer gap


class NaiveScheduler:
    """The pass-through baseline: newest frame per camera callback, sequential audio, no shared clock."""

    def __init__(self) -> None:
        self.video, self.audio, self.shown = [], [], None

    def push_video(self, index):
        self.video.append(index)

    def push_audio(self, start, count):
        self.audio.append([start, count])

    def on_video_callback(self, t):
        if self.video:
            self.shown, self.video = self.video[-1], []
        return {"frame": self.shown, "kind": "new" if self.shown is not None else "underrun"}

    def on_audio_callback(self, t, count):
        segments = []
        while count and self.audio:
            head = self.audio[0]
            take = min(count, head[1])
            segments.append(("source", head[0], take))
            head[0] += take
            head[1] -= take
            count -= take
            if not head[1]:
                self.audio.pop(0)
        if count:
            segments.append(("silence", count))
        return {"segments": segments}

    def status(self):
        return {"health": "unknown", "output_allowed": True}


def _times(rng, period_ns, rate_ppm, jitter_ms, end_ns, stalls, kind):
    """Callback times: nominal period scaled by the consumer's clock error, jittered, stall-bunched."""

    scale = 1 + rate_ppm / 1e6
    jitter_ns = int(jitter_ms * 1e6)
    out, n, last = [], 0, 0
    while True:
        nominal = int(n * period_ns * scale)
        if nominal > end_ns:
            return out
        t = max(last, nominal + (rng.randint(-jitter_ns, jitter_ns) if jitter_ns else 0))
        for start_s, duration_s, which in stalls:
            if which == kind and start_s * NS <= t < (start_s + duration_s) * NS:
                t = int((start_s + duration_s) * NS)
        out.append(t)
        last = t
        n += 1


def simulate(config: SimConfig, scheduler=None) -> dict:
    rng = random.Random(config.seed)
    scheduler = scheduler or clock.PairedScheduler()
    end = int(config.duration_s * NS)
    events = []
    # Producer: the exact Program Master pair on the MXL timeline (media = host time).
    for k, t in enumerate(_times(rng, NS // clock.VIDEO_RATE, 0, config.producer_jitter_ms, end,
                                 config.stalls, "producer")):
        heapq.heappush(events, (t, 0, "video_in", k))
    offset = 0
    gaps = sorted(config.audio_gaps)
    for j, t in enumerate(_times(rng, BLOCK * NS // clock.SAMPLE_RATE, 0, config.producer_jitter_ms, end,
                                 config.stalls, "producer")):
        while gaps and gaps[0][0] * NS <= t:
            offset += gaps.pop(0)[1]
        heapq.heappush(events, (t, 1, "audio_in", j * BLOCK + offset))
    for t in _times(rng, int(NS / config.camera_fps), config.camera_rate_ppm, config.camera_jitter_ms, end,
                    config.stalls, "camera"):
        heapq.heappush(events, (t, 2, "camera", None))
    for t in _times(rng, int(config.mic_period_ms * 1e6), config.mic_rate_ppm, config.mic_jitter_ms, end,
                    config.stalls, "mic"):
        heapq.heappush(events, (t, 3, "mic", None))

    seen: dict[int, int] = {}   # frame index -> host time it first appeared
    heard: list[tuple[int, int, int]] = []  # (first source sample, count, host time it starts playing)
    new_frames = 0
    health = []
    while events:
        t, _, kind, value = heapq.heappop(events)
        if kind == "video_in":
            scheduler.push_video(value)
        elif kind == "audio_in":
            scheduler.push_audio(value, BLOCK)
        elif kind == "camera":
            result = scheduler.on_video_callback(t)
            if result["kind"] == "new":
                new_frames += 1
                seen.setdefault(result["frame"], t)
        else:
            result = scheduler.on_audio_callback(t, BLOCK)
            health.append(result.get("health"))
            position = 0
            for segment in result["segments"]:
                if segment[0] == "source":
                    heard.append((segment[1], segment[2], t + position * NS // clock.SAMPLE_RATE, segment[2]))
                    position += segment[2]
                elif segment[0] == "resampled":  # source_n samples played over output_n samples
                    heard.append((segment[1], segment[2], t + position * NS // clock.SAMPLE_RATE, segment[3]))
                    position += segment[3]
                else:
                    position += segment[-1]
    return {"offsets_ms": summarize(av_offsets(seen, heard)),
            "unique_video_fps": round(new_frames / config.duration_s, 2),
            "status": scheduler.status(), "health_trace": health}


def av_offsets(seen: dict, heard: list) -> list[tuple[int, float]]:
    """Analyzer-style lip sync for every shown frame: time its content is heard minus time it is seen.

    Positive means the audio is late. Content that was never heard (dropped
    or not yet played) has no pair and is skipped.
    """

    starts = [entry[0] for entry in heard]
    out = []
    for frame, t_seen in sorted(seen.items()):
        sample = frame * clock.SAMPLE_RATE // clock.VIDEO_RATE
        i = bisect.bisect_right(starts, sample) - 1
        if i < 0:
            continue
        first, count, t_start, played = heard[i]
        if not first <= sample < first + count:
            continue
        t_heard = t_start + (sample - first) * played * NS // (count * clock.SAMPLE_RATE)
        out.append((t_seen, (t_heard - t_seen) / 1e6))
    return out


def summarize(offsets: list[tuple[int, float]]) -> dict:
    if len(offsets) < 2:
        return {"samples": len(offsets)}
    values = [v for _, v in offsets]
    times = [t / NS for t, _ in offsets]
    mt, mv = sum(times) / len(times), sum(values) / len(values)
    denom = sum((t - mt) ** 2 for t in times)
    slope_ms_per_s = sum((t - mt) * (v - mv) for t, v in zip(times, values)) / denom if denom else 0.0
    # Skip the first second so the start-up latency choice does not dominate the range.
    settled = [v for t, v in offsets if t - offsets[0][0] >= NS] or values
    return {"samples": len(values), "min": round(min(settled), 3), "max": round(max(settled), 3),
            "median": round(statistics.median(settled), 3), "range": round(max(settled) - min(settled), 3),
            "drift_ppm": round(slope_ms_per_s * 1000, 1)}
