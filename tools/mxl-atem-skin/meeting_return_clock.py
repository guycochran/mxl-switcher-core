#!/usr/bin/env python3
"""Paired-clock scheduler contract for a future meeting-return adapter.

**Offline foundation only. This is not an accepted Zoom meeting return.**

The audience gate showed the A/V pair is exact entering the meeting adapter
and grows apart after it. The virtual camera and virtual microphone pull
media on independent callback clocks, and an audio-only PLL cannot pair them.
This module defines one scheduler that owns **one monotonic output clock** for
both callback domains. It is pure, holds no Zoom or MXL dependency, and uses
integer nanoseconds and sample indices, so the simulator can drive it
deterministically.

Model:

- Input is the accepted Program Master pair, which shares one media timeline:
  video frame ``k`` at media ``k / 30`` s, audio sample ``s`` at media
  ``s / 48000`` s. ``push_video`` and ``push_audio`` fill **bounded** queues;
  overflow drops the oldest data and is counted.
- **One output epoch** ``(t0, m0)`` maps host monotonic time to a media target
  ``m(t) = m0 + (t - t0)``. ``m0`` is chosen at ``start`` as
  ``target_latency`` behind the newest media both queues hold.
- **Video** (camera callback at ``t``): present the newest queued frame with
  media ≤ ``m(t)``. Older unpresented frames are counted ``video_drops``. If
  nothing newer is due, the last frame is repeated (``video_repeats``). An empty
  queue is ``video_underruns``.
- **Audio** (microphone callback at ``t`` for ``n`` samples): the read pointer
  ``r`` is compared with the target ``d = m(t) × 48000``. Outside a deadband,
  up to a bounded correction budget (``max_correction_ppm`` of the samples
  delivered) is spent per callback: dropping samples when audio is behind
  (``audio_dropped``) or repeating the last sample when it is ahead
  (``audio_inserted``). An error beyond ``discontinuity_ms`` is a
  **re-anchor**: ``r`` jumps to ``d``, counted, and limited by
  ``max_reanchors``. Missing input becomes silence (``audio_underrun_samples``).
- **Skew** is audio error minus video error, both measured against the shared
  clock, so it is the lip-sync error the scheduler itself introduces.

Health is fail-closed and latching. The adapter must stop sending, or alarm,
when ``output_allowed`` is false. It goes ``degraded`` while skew exceeds the
soft bound or corrections were saturated in at least half of the recent window,
and ``failed`` when:

- skew stays beyond the hard bound for ``hard_grace_callbacks``; or
- an uncorrectable rate mismatch: over the last ``saturation_callbacks``
  callbacks, corrections were saturated at least 90% of the time and the mean
  residual of the newer half exceeds the older half by more than the
  deadband (losing ground despite full correction); or a re-anchor was
  needed while saturated corrections were not converging; or
- more than ``max_reanchors`` re-anchors occur within ``reanchor_window_s``.
"""

from __future__ import annotations

import collections
import dataclasses

NS = 1_000_000_000
VIDEO_RATE = 30
SAMPLE_RATE = 48_000


def frame_media_ns(index: int) -> int:
    return index * NS // VIDEO_RATE


def sample_media_ns(index: int) -> int:
    return index * NS // SAMPLE_RATE


def media_ns_to_sample(media_ns: int) -> int:
    return media_ns * SAMPLE_RATE // NS


@dataclasses.dataclass(frozen=True)
class Limits:
    video_queue_frames: int = 8
    audio_queue_samples: int = SAMPLE_RATE // 2
    target_latency_ms: float = 80.0
    audio_deadband_ms: float = 5.0
    max_correction_ppm: float = 20_000.0
    discontinuity_ms: float = 100.0
    max_reanchors: int = 5
    reanchor_window_s: float = 60.0
    skew_soft_ms: float = 50.0
    skew_hard_ms: float = 100.0
    hard_grace_callbacks: int = 50
    saturation_callbacks: int = 300
    interval_window: int = 256

    def __post_init__(self) -> None:
        if self.video_queue_frames < 2 or self.audio_queue_samples < 960:
            raise ValueError("queues must hold at least two frames and two audio blocks")
        if not 0 < self.max_correction_ppm < 100_000:
            raise ValueError("max_correction_ppm must be between 0 and 100000")
        if not 0 < self.skew_soft_ms < self.skew_hard_ms:
            raise ValueError("skew bounds must satisfy 0 < soft < hard")


class IntervalStats:
    """Bounded record of callback intervals (ns): last, min, max, mean, and spread."""

    def __init__(self, window: int) -> None:
        self.last_time: int | None = None
        self.intervals: collections.deque[int] = collections.deque(maxlen=window)

    def note(self, t: int) -> None:
        if self.last_time is not None:
            self.intervals.append(t - self.last_time)
        self.last_time = t

    def view(self) -> dict:
        if not self.intervals:
            return {"count": 0}
        values = sorted(self.intervals)
        mean = sum(values) / len(values)
        return {"count": len(values), "last_ms": round(self.intervals[-1] / 1e6, 3),
                "min_ms": round(values[0] / 1e6, 3), "max_ms": round(values[-1] / 1e6, 3),
                "mean_ms": round(mean / 1e6, 3), "p99_ms": round(values[min(len(values) - 1,
                                                                             int(len(values) * 0.99))] / 1e6, 3)}


class PairedScheduler:
    """One output clock for paired Program video and audio callbacks."""

    def __init__(self, limits: Limits | None = None) -> None:
        self.limits = limits or Limits()
        self.video: collections.deque[int] = collections.deque()  # queued frame indices
        self.audio: collections.deque[list] = collections.deque()  # [start_index, count] segments
        self.audio_queued = 0
        self.audio_end: int | None = None  # next expected input sample
        self.epoch: tuple[int, int] | None = None
        self.read: int | None = None  # next audio sample index to present
        self.shown: int | None = None  # last presented frame index
        self.video_error_ns = 0
        self.audio_error_ns = 0
        self.correction_credit = 0.0
        self.counters = {key: 0 for key in (
            "video_drops", "video_repeats", "video_underruns", "video_overruns",
            "audio_dropped", "audio_inserted", "audio_underrun_samples", "audio_overrun_samples",
            "audio_input_gaps", "audio_input_gap_samples", "audio_input_overlap_samples",
            "reanchors", "discontinuities", "saturated_callbacks",
            "pre_start_video_callbacks", "pre_start_audio_samples")}
        self.skew = {"current_ms": 0.0, "min_ms": 0.0, "max_ms": 0.0}
        self.intervals = {"video": IntervalStats(self.limits.interval_window),
                          "audio": IntervalStats(self.limits.interval_window)}
        self.max_depth = {"video_frames": 0, "audio_samples": 0}
        self.health = "starting"
        self.reason: str | None = None
        self._hard_run = 0
        self._window: collections.deque[tuple[bool, int]] = collections.deque(
            maxlen=self.limits.saturation_callbacks)
        self._reanchors_at: collections.deque[int] = collections.deque()

    # --- input ------------------------------------------------------------------
    def push_video(self, frame_index: int) -> None:
        if self.video and frame_index <= self.video[-1]:
            return  # a duplicate or regressed frame never enters the queue
        self.video.append(frame_index)
        while len(self.video) > self.limits.video_queue_frames:
            self.video.popleft()
            self.counters["video_overruns"] += 1
        self.max_depth["video_frames"] = max(self.max_depth["video_frames"], len(self.video))

    def push_audio(self, start_index: int, count: int) -> None:
        if self.audio_end is not None and start_index != self.audio_end:
            if start_index > self.audio_end:
                self.counters["audio_input_gaps"] += 1
                self.counters["audio_input_gap_samples"] += start_index - self.audio_end
            else:  # overlap or regression: keep only the new tail
                overlap = min(count, self.audio_end - start_index)
                self.counters["audio_input_overlap_samples"] += overlap
                start_index, count = start_index + overlap, count - overlap
                if count <= 0:
                    return
        self.audio.append([start_index, count])
        self.audio_queued += count
        self.audio_end = start_index + count
        while self.audio_queued > self.limits.audio_queue_samples:
            excess = self.audio_queued - self.limits.audio_queue_samples
            head = self.audio[0]
            take = min(excess, head[1])
            head[0] += take
            head[1] -= take
            self.audio_queued -= take
            self.counters["audio_overrun_samples"] += take
            if head[1] == 0:
                self.audio.popleft()
        self.max_depth["audio_samples"] = max(self.max_depth["audio_samples"], self.audio_queued)

    # --- clock ------------------------------------------------------------------
    def start(self, t_ns: int) -> bool:
        """Set the shared epoch once both queues hold data. Returns whether the clock started."""

        if self.epoch is not None:
            return True
        if not self.video or not self.audio:
            return False
        newest = min(frame_media_ns(self.video[-1]), sample_media_ns(self.audio_end))
        oldest = max(frame_media_ns(self.video[0]), sample_media_ns(self.audio[0][0]))
        m0 = newest - int(self.limits.target_latency_ms * 1e6)
        if m0 < oldest:  # not yet holding the target latency of both media: keep buffering
            return False
        self.epoch = (t_ns, m0)
        self.read = media_ns_to_sample(m0)
        self.health = "ok"
        return True

    def target_media_ns(self, t_ns: int) -> int:
        t0, m0 = self.epoch
        return m0 + (t_ns - t0)

    # --- callbacks ----------------------------------------------------------------
    def on_video_callback(self, t_ns: int) -> dict:
        """Choose the frame the virtual camera presents at ``t_ns``."""

        self.intervals["video"].note(t_ns)
        if not self.start(t_ns):  # still filling the latency reservoir; not a starvation
            self.counters["pre_start_video_callbacks"] += 1
            return {"frame": None, "kind": "starting"}
        target = self.target_media_ns(t_ns)
        chosen = None
        while self.video and frame_media_ns(self.video[0]) <= target:
            if chosen is not None:
                self.counters["video_drops"] += 1
            chosen = self.video.popleft()
        if chosen is None:
            if self.shown is None:
                self.counters["video_underruns"] += 1
                return {"frame": None, "kind": "underrun"}
            self.counters["video_repeats"] += 1
            kind = "repeat"
        else:
            self.shown = chosen
            kind = "new"
        self.video_error_ns = frame_media_ns(self.shown) - target
        return {"frame": self.shown, "kind": kind, "error_ms": round(self.video_error_ns / 1e6, 3)}

    def on_audio_callback(self, t_ns: int, count: int) -> dict:
        """Return the presented audio as segments: ``("source", start, n)`` or ``("hold"|"silence", n)``."""

        self.intervals["audio"].note(t_ns)
        if not self.start(t_ns):
            self.counters["pre_start_audio_samples"] += count
            return {"segments": [("silence", count)], "health": self.health}
        limits = self.limits
        target = media_ns_to_sample(self.target_media_ns(t_ns))
        error = self.read - target  # >0: ahead of the clock (repeat), <0: behind (drop)
        deadband = round(limits.audio_deadband_ms * SAMPLE_RATE / 1000)
        discontinuity = round(limits.discontinuity_ms * SAMPLE_RATE / 1000)
        segments: list[tuple] = []
        saturated = False
        residual = error
        per_callback = count * limits.max_correction_ppm / 1e6
        # Credit accrues every callback (capped), so correction capacity is a rate, not a burst.
        self.correction_credit = min(self.correction_credit + per_callback, 2 * per_callback + 1)
        if abs(error) > discontinuity:
            self.counters["discontinuities"] += 1
            self.counters["reanchors"] += 1
            self._reanchors_at.append(t_ns)
            while self._reanchors_at and self._reanchors_at[0] < t_ns - limits.reanchor_window_s * 1e9:
                self._reanchors_at.popleft()
            # Saturated corrections that were not converging, then still lost the clock: a rate the
            # correction budget cannot follow. A transient (a stall) converges and is not counted.
            if (len(self._window) >= limits.saturation_callbacks // 2
                    and sum(s for s, _ in self._window) >= 0.9 * len(self._window)
                    and self._trend() >= 0):
                self._fail("uncorrectable_rate_mismatch")
            self._discard_before(target)
            self.read = target
            residual = 0
            if len(self._reanchors_at) > limits.max_reanchors:
                self._fail("reanchor_budget_exceeded")
        elif abs(error) > deadband:
            budget = int(self.correction_credit)
            step = min(abs(error) - deadband, budget)
            saturated = step == budget and abs(error) - deadband > budget
            self.correction_credit -= step
            if error < 0:  # behind: skip ahead
                self._discard_before(self.read + step)
                self.read += step
                self.counters["audio_dropped"] += step
                residual = error + step
            elif step:  # ahead: repeat the last presented sample
                segments.append(("hold", step))
                self.counters["audio_inserted"] += step
                count -= step
                residual = error - step
        segments.extend(self._take(count))
        self.audio_error_ns = sample_media_ns(self.read) - self.target_media_ns(t_ns)
        self._update_health(saturated, abs(residual))
        return {"segments": segments, "health": self.health}

    def _discard_before(self, index: int) -> None:
        while self.audio and self.audio[0][0] < index:
            head = self.audio[0]
            take = min(head[1], index - head[0])
            head[0] += take
            head[1] -= take
            self.audio_queued -= take
            if head[1] == 0:
                self.audio.popleft()

    def _take(self, count: int) -> list[tuple]:
        self._discard_before(self.read)  # never present samples older than the clock wants
        segments = []
        while count > 0:
            if not self.audio:
                segments.append(("silence", count))
                self.counters["audio_underrun_samples"] += count
                self.read += count
                break
            head = self.audio[0]
            if head[0] > self.read:  # an input gap: present silence until the data resumes
                gap = min(count, head[0] - self.read)
                segments.append(("silence", gap))
                self.counters["audio_underrun_samples"] += gap
                self.read += gap
                count -= gap
                continue
            take = min(count, head[1])
            segments.append(("source", head[0], take))
            head[0] += take
            head[1] -= take
            self.audio_queued -= take
            self.read += take
            count -= take
            if head[1] == 0:
                self.audio.popleft()
        return segments

    # --- health -------------------------------------------------------------------
    def _fail(self, reason: str) -> None:
        if self.health != "failed":
            self.health, self.reason = "failed", reason

    def _update_health(self, saturated: bool, abs_error: int) -> None:
        limits = self.limits
        skew_ms = (self.audio_error_ns - self.video_error_ns) / 1e6
        self.skew["current_ms"] = round(skew_ms, 3)
        self.skew["min_ms"] = round(min(self.skew["min_ms"], skew_ms), 3)
        self.skew["max_ms"] = round(max(self.skew["max_ms"], skew_ms), 3)
        if saturated:
            self.counters["saturated_callbacks"] += 1
        self._window.append((saturated, abs_error))
        self._hard_run = self._hard_run + 1 if abs(skew_ms) > limits.skew_hard_ms else 0
        if self.health == "failed":
            return
        deadband = round(limits.audio_deadband_ms * SAMPLE_RATE / 1000)
        if (len(self._window) == limits.saturation_callbacks
                and sum(s for s, _ in self._window) >= 0.9 * len(self._window)
                and self._trend() > deadband):
            return self._fail("uncorrectable_rate_mismatch")
        if self._hard_run >= limits.hard_grace_callbacks:
            return self._fail("sustained_av_skew")
        busy = sum(s for s, _ in self._window) >= 0.5 * len(self._window) if self._window else False
        self.health = "degraded" if abs(skew_ms) > limits.skew_soft_ms or busy else "ok"

    def _trend(self) -> float:
        """Mean |residual| of the window's newer half minus its older half (samples): >0 is losing ground."""

        values = [error for _, error in self._window]
        half = len(values) // 2
        if half == 0:
            return 0.0
        return sum(values[half:]) / (len(values) - half) - sum(values[:half]) / half

    def status(self) -> dict:
        """Fail-closed health for an EIC dashboard."""

        return {
            "adapter": "paired-meeting-return-clock",
            "foundation_only": True,
            "health": self.health,
            "output_allowed": self.health in ("ok", "degraded"),
            "reason": self.reason,
            "clock_started": self.epoch is not None,
            "av_skew": dict(self.skew),
            "video_error_ms": round(self.video_error_ns / 1e6, 3),
            "audio_error_ms": round(self.audio_error_ns / 1e6, 3),
            "queue_depth": {"video_frames": len(self.video), "audio_samples": self.audio_queued},
            "queue_max_depth": dict(self.max_depth),
            "callback_intervals": {name: stats.view() for name, stats in self.intervals.items()},
            "counters": dict(self.counters),
        }
