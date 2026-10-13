#!/usr/bin/env python3
"""Clock measurement and bounded adaptive-resampling control for Program audio."""

from __future__ import annotations

import collections
import dataclasses
import math
from fractions import Fraction


NANOSECONDS = 1_000_000_000


@dataclasses.dataclass(frozen=True)
class ClockSample:
    media_ns: int
    monotonic_ns: int


class ClockRateEstimator:
    """Estimate media-clock rate against monotonic time over a bounded window.

    A ratio above 1.0 means the producer timeline advances faster than the
    consuming host. Regressions and implausible jumps start a new observation
    epoch instead of poisoning the estimate.
    """

    def __init__(
        self,
        window_seconds: float = 10.0,
        minimum_span_seconds: float = 1.0,
        maximum_rate_error_ppm: float = 100_000.0,
    ) -> None:
        if window_seconds <= 0 or minimum_span_seconds <= 0:
            raise ValueError("clock-estimator windows must be positive")
        self.window_ns = int(window_seconds * NANOSECONDS)
        self.minimum_span_ns = int(minimum_span_seconds * NANOSECONDS)
        self.maximum_rate_error = maximum_rate_error_ppm / 1_000_000.0
        self.samples: collections.deque[ClockSample] = collections.deque()
        self.resets = 0
        self.ignored_samples = 0

    def reset(self) -> None:
        self.samples.clear()
        self.resets += 1

    def observe(self, media_ns: int, monotonic_ns: int) -> bool:
        sample = ClockSample(media_ns, monotonic_ns)
        if self.samples:
            previous = self.samples[-1]
            media_delta = media_ns - previous.media_ns
            wall_delta = monotonic_ns - previous.monotonic_ns
            if wall_delta == 0 and media_delta > 0:
                # Multiple buffers may be released in one scheduler burst.
                # Their identical arrival time contains no rate information.
                self.ignored_samples += 1
                return self.ready
            if media_delta <= 0 or wall_delta < 0:
                self.reset()
            else:
                local_rate = media_delta / wall_delta
                if abs(local_rate - 1.0) > self.maximum_rate_error:
                    self.reset()
        self.samples.append(sample)
        cutoff = monotonic_ns - self.window_ns
        while len(self.samples) > 2 and self.samples[1].monotonic_ns < cutoff:
            self.samples.popleft()
        return self.ready

    @property
    def ready(self) -> bool:
        return len(self.samples) >= 2 and self.wall_span_ns >= self.minimum_span_ns

    @property
    def wall_span_ns(self) -> int:
        if len(self.samples) < 2:
            return 0
        return self.samples[-1].monotonic_ns - self.samples[0].monotonic_ns

    @property
    def rate(self) -> float | None:
        if not self.ready:
            return None
        origin = self.samples[0]
        wall = [sample.monotonic_ns - origin.monotonic_ns for sample in self.samples]
        media = [sample.media_ns - origin.media_ns for sample in self.samples]
        wall_mean = sum(wall) / len(wall)
        media_mean = sum(media) / len(media)
        covariance = sum(
            (wall_value - wall_mean) * (media_value - media_mean)
            for wall_value, media_value in zip(wall, media)
        )
        variance = sum((wall_value - wall_mean) ** 2 for wall_value in wall)
        return covariance / variance if variance else None

    @property
    def drift_ppm(self) -> float | None:
        rate = self.rate
        return None if rate is None else (rate - 1.0) * 1_000_000.0


class AdaptiveResampleController:
    """Convert measured input drift and buffer error into a bounded ratio.

    The returned value is output samples per input sample. A fast producer
    therefore produces a value below 1.0. Buffer feedback is deliberately
    slow; it drains or fills the jitter buffer without creating audible pitch
    steps.
    """

    def __init__(
        self,
        target_buffer_ms: float = 120.0,
        max_correction_ppm: float = 2500.0,
        buffer_gain_ppm_per_ms: float = 4.0,
        smoothing: float = 0.05,
    ) -> None:
        if target_buffer_ms <= 0:
            raise ValueError("target buffer must be positive")
        if not 0 < smoothing <= 1:
            raise ValueError("smoothing must be in (0, 1]")
        self.target_buffer_ms = target_buffer_ms
        self.max_correction_ppm = max_correction_ppm
        self.buffer_gain_ppm_per_ms = buffer_gain_ppm_per_ms
        self.smoothing = smoothing
        self.ratio = 1.0
        self.updates = 0

    def update(self, input_rate: float, buffer_ms: float) -> float:
        if not math.isfinite(input_rate) or input_rate <= 0:
            raise ValueError("input rate must be finite and positive")
        buffer_error_ms = buffer_ms - self.target_buffer_ms
        buffer_correction_ppm = buffer_error_ms * self.buffer_gain_ppm_per_ms
        correction_limit = abs(self.max_correction_ppm)
        buffer_correction_ppm = max(
            -correction_limit,
            min(correction_limit, buffer_correction_ppm),
        )
        target = 1.0 / (input_rate * (1.0 + buffer_correction_ppm / 1_000_000.0))
        minimum = 1.0 - correction_limit / 1_000_000.0
        maximum = 1.0 + correction_limit / 1_000_000.0
        target = max(minimum, min(maximum, target))
        self.ratio += self.smoothing * (target - self.ratio)
        self.updates += 1
        return self.ratio

    @property
    def correction_ppm(self) -> float:
        return (self.ratio - 1.0) * 1_000_000.0


class ProgramTimestampAccumulator:
    """Generate exact audio and video PTS from one integer epoch."""

    def __init__(
        self,
        epoch_ns: int,
        audio_sample_rate: int = 48_000,
        video_rate_numerator: int = 30_000,
        video_rate_denominator: int = 1001,
    ) -> None:
        if audio_sample_rate <= 0 or video_rate_numerator <= 0 or video_rate_denominator <= 0:
            raise ValueError("rates must be positive")
        self.epoch_ns = epoch_ns
        self.audio_sample_rate = audio_sample_rate
        self.video_rate = Fraction(video_rate_numerator, video_rate_denominator)

    def audio_pts_ns(self, sample_index: int) -> int:
        return self.epoch_ns + (sample_index * NANOSECONDS) // self.audio_sample_rate

    def video_pts_ns(self, frame_index: int) -> int:
        offset = Fraction(frame_index * NANOSECONDS, 1) / self.video_rate
        return self.epoch_ns + offset.numerator // offset.denominator

    def av_skew_ns(self, sample_index: int, frame_index: int) -> int:
        return self.audio_pts_ns(sample_index) - self.video_pts_ns(frame_index)


def telemetry(
    estimator: ClockRateEstimator,
    controller: AdaptiveResampleController,
    buffer_ms: float,
    av_skew_ns: int,
) -> dict[str, int | float | bool | None]:
    return {
        "clock_ready": estimator.ready,
        "input_rate": estimator.rate,
        "input_drift_ppm": estimator.drift_ppm,
        "resample_ratio": controller.ratio,
        "resample_correction_ppm": controller.correction_ppm,
        "audio_buffer_ms": buffer_ms,
        "av_skew_ms": av_skew_ns / 1_000_000.0,
        "clock_resets": estimator.resets,
        "clock_samples_ignored": estimator.ignored_samples,
        "controller_updates": controller.updates,
    }
