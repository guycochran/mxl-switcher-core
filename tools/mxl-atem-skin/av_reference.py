#!/usr/bin/env python3
"""Objective A/V reference timeline, encoders, and capture analyzer.

The reference ties one 30 fps video grain index ``g`` to one 48 kHz audio
sample index ``s`` through ``s == g * 1600``. Every frame carries a binary
frame code (the low 32 bits of ``g`` plus CRC-8) that survives scaling and
lossy encoding. The code is ``g mod CODE_MODULUS``, the largest multiple of
120 below 2**32, so ``g % 30`` and the burst code survive the wrap. Once per second, on frames where ``g % 30 == 0``, the video
shows a white flash patch and the audio starts a 20 ms tone burst at exactly
that frame's first sample. The burst frequency cycles through four values so
an analyzer can pair audio and video events unambiguously across +/-2 s.

Two audio modes exist:

- ``meeting``: the burst is on both channels and nothing else is present.
  Use it for any path that may downmix, encode, or apply AGC.
- ``lossless``: the burst is on the left channel; the right channel carries
  an exact sample counter (``(s mod 2**20 - 2**19) / 2**22`` in Float32) so
  gain-only MXL paths can be checked for sample gaps, duplicates, and
  regressions. Never route this mode into a meeting.

This module only uses the standard library so its tests run anywhere.
``mxl_av_reference_source.py`` publishes it into MXL and
``mxl_av_analyze.py`` analyzes captures.
"""

from __future__ import annotations

import math
import statistics
from array import array
from dataclasses import dataclass, field

VIDEO_RATE = 30
SAMPLE_RATE = 48_000
SAMPLES_PER_FRAME = SAMPLE_RATE // VIDEO_RATE
EVENT_PERIOD_FRAMES = VIDEO_RATE
EVENT_PERIOD_SAMPLES = EVENT_PERIOD_FRAMES * SAMPLES_PER_FRAME

BURST_SAMPLES = 960
BURST_RAMP_SAMPLES = 96
BURST_AMPLITUDE = 0.5
BURST_FREQUENCIES_HZ = (600, 900, 1200, 1500)

COUNTER_MODULUS = 1 << 20
COUNTER_SCALE = 1.0 / (1 << 22)

CODE_BITS = 32
CODE_MODULUS = (1 << CODE_BITS) // 120 * 120
CRC_BITS = 8
REFERENCE_WIDTH = 1920
REFERENCE_HEIGHT = 1080
CODE_BLOCK_WIDTH = 80
CODE_BLOCK_HEIGHT = 56
CODE_LEFT = 80
CODE_TOP = 936
CODE_BITS_PER_ROW = 20
FLASH_RECT = (1680, 60, 1860, 240)
MIN_CODE_CONTRAST = 40


# --- timeline -------------------------------------------------------------


def grain_to_sample(grain: int) -> int:
    return grain * SAMPLES_PER_FRAME


def index_to_ns(index: int, rate_numerator: int, rate_denominator: int = 1) -> int:
    """MXL's index -> TAI nanoseconds mapping (round half up, as mxlIndexToTimestamp)."""

    return (index * rate_denominator * 1_000_000_000 + rate_numerator // 2) // rate_numerator


def ns_to_index(timestamp_ns: int, rate_numerator: int, rate_denominator: int = 1) -> int:
    """Nearest index for a TAI timestamp; inverts :func:`index_to_ns` exactly."""

    period = rate_denominator * 1_000_000_000
    return (timestamp_ns * rate_numerator + period // 2) // period


def tightest_clock_offset(pipeline_now, mxl_now, samples: int = 50) -> int:
    """``mxl_now - pipeline_now`` from the narrowest of ``samples`` bracketed reads."""

    best = None
    for _ in range(samples):
        before = pipeline_now()
        middle = mxl_now()
        after = pipeline_now()
        window = after - before
        if best is None or window < best[0]:
            best = (window, middle - (before + after) // 2)
    return best[1]


def is_event_grain(grain: int) -> bool:
    return grain % EVENT_PERIOD_FRAMES == 0


def event_code(grain: int) -> int:
    """Return the burst-frequency index carried by the event at ``grain``."""

    return (grain // EVENT_PERIOD_FRAMES) % len(BURST_FREQUENCIES_HZ)


def timecode(grain: int) -> str:
    frames = grain % VIDEO_RATE
    seconds = grain // VIDEO_RATE
    return (
        f"{(seconds // 3600) % 24:02d}:{(seconds // 60) % 60:02d}:"
        f"{seconds % 60:02d}:{frames:02d}"
    )


def wrap_delta(current: int, previous: int, modulus: int = CODE_MODULUS) -> int:
    """Signed difference of two wrapped counters."""

    delta = (current - previous) % modulus
    return delta - modulus if delta >= modulus // 2 else delta


# --- video frame code -----------------------------------------------------


def crc8(value: int) -> int:
    crc = 0
    for byte in value.to_bytes(4, "big"):
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def frame_code_bits(grain: int) -> list[int]:
    value = grain % CODE_MODULUS
    word = (value << CRC_BITS) | crc8(value)
    total = CODE_BITS + CRC_BITS
    return [(word >> (total - 1 - index)) & 1 for index in range(total)]


def code_blocks(grain: int) -> list[tuple[tuple[int, int, int, int], int]]:
    """Return ``((x0, y0, x1, y1), luma_bit)`` blocks in 1920x1080 coordinates.

    Each row starts with two reference blocks (row 0 white/black, row 1
    black/white) so a decoder can threshold every row independently.
    """

    bits = frame_code_bits(grain)
    blocks = []
    for row in range(2):
        refs = (1, 0) if row == 0 else (0, 1)
        values = list(refs) + bits[row * CODE_BITS_PER_ROW:(row + 1) * CODE_BITS_PER_ROW]
        top = CODE_TOP + row * CODE_BLOCK_HEIGHT
        for column, value in enumerate(values):
            left = CODE_LEFT + column * CODE_BLOCK_WIDTH
            blocks.append(
                ((left, top, left + CODE_BLOCK_WIDTH, top + CODE_BLOCK_HEIGHT), value)
            )
    return blocks


def flash_on(grain: int) -> bool:
    return is_event_grain(grain)


def _sample_block(pixel, width: int, height: int, rect: tuple[int, int, int, int]) -> float:
    """Mean luma of a 3x3 grid inside the central half of a reference rect."""

    scale_x = width / REFERENCE_WIDTH
    scale_y = height / REFERENCE_HEIGHT
    x0, y0, x1, y1 = rect
    total = 0
    for fy in (0.3, 0.5, 0.7):
        y = min(height - 1, int((y0 + (y1 - y0) * fy) * scale_y))
        for fx in (0.3, 0.5, 0.7):
            x = min(width - 1, int((x0 + (x1 - x0) * fx) * scale_x))
            total += pixel(x, y)
    return total / 9


def _decode_with(pixel, width: int, height: int) -> int | None:
    layout = code_blocks(0)
    per_row = 2 + CODE_BITS_PER_ROW
    bits = []
    for row in range(2):
        row_blocks = layout[row * per_row:(row + 1) * per_row]
        first = _sample_block(pixel, width, height, row_blocks[0][0])
        second = _sample_block(pixel, width, height, row_blocks[1][0])
        white, black = (first, second) if row == 0 else (second, first)
        if white - black < MIN_CODE_CONTRAST:
            return None
        threshold = (white + black) / 2
        for rect, _ in row_blocks[2:]:
            bits.append(1 if _sample_block(pixel, width, height, rect) >= threshold else 0)
    word = 0
    for bit in bits:
        word = (word << 1) | bit
    value = word >> CRC_BITS
    if crc8(value) != word & 0xFF or value >= CODE_MODULUS:
        return None
    return value


def decode_frame_code(
    frame: bytes | bytearray | memoryview, width: int, height: int
) -> int | None:
    """Decode ``grain % CODE_MODULUS`` from one 8-bit luma frame, or ``None``."""

    if len(frame) < width * height:
        raise ValueError("frame is smaller than width*height")
    return _decode_with(lambda x, y: frame[y * width + x], width, height)


# v210 packs six 4:2:2 pixels into four little-endian 32-bit words. For each
# pixel position within a group: (word, bit shift) of its 10-bit luma.
_V210_LUMA = ((0, 10), (1, 0), (1, 20), (2, 10), (3, 0), (3, 20))


def v210_stride(width: int) -> int:
    return (width + 47) // 48 * 128


def v210_luma(frame, stride: int, x: int, y: int) -> int:
    """Return the 8-bit luma of pixel ``(x, y)`` from a v210 frame."""

    word, shift = _V210_LUMA[x % 6]
    offset = y * stride + (x // 6) * 16 + word * 4
    value = int.from_bytes(frame[offset:offset + 4], "little")
    return ((value >> shift) & 0x3FF) >> 2


def decode_v210_frame_code(frame, width: int = REFERENCE_WIDTH, height: int = REFERENCE_HEIGHT) -> int | None:
    """Decode the frame code straight from a v210 grain, reading ~400 pixels."""

    stride = v210_stride(width)
    if len(frame) < stride * height:
        raise ValueError("frame is smaller than the v210 stride*height")
    return _decode_with(lambda x, y: v210_luma(frame, stride, x, y), width, height)


def v210_from_luma(luma, width: int, height: int) -> bytearray:
    """Pack 8-bit luma (neutral chroma) into v210. For tests and small frames."""

    stride = v210_stride(width)
    out = bytearray(stride * height)
    for y in range(height):
        for group in range(0, width, 6):
            words = [512, 512 << 10, 512, 512 << 10]
            for position in range(6):
                x = group + position
                value = (luma[y * width + x] if x < width else 16) << 2
                word, shift = _V210_LUMA[position]
                words[word] |= value << shift
            offset = y * stride + (group // 6) * 16
            for index, word in enumerate(words):
                out[offset + 4 * index:offset + 4 * index + 4] = word.to_bytes(4, "little")
    return out


# --- audio ----------------------------------------------------------------


def _burst_value(offset: int, frequency_index: int, amplitude: float) -> float:
    if offset < 0 or offset >= BURST_SAMPLES:
        return 0.0
    if offset < BURST_RAMP_SAMPLES:
        envelope = 0.5 - 0.5 * math.cos(math.pi * offset / BURST_RAMP_SAMPLES)
    elif offset >= BURST_SAMPLES - BURST_RAMP_SAMPLES:
        remaining = BURST_SAMPLES - offset
        envelope = 0.5 - 0.5 * math.cos(math.pi * remaining / BURST_RAMP_SAMPLES)
    else:
        envelope = 1.0
    frequency = BURST_FREQUENCIES_HZ[frequency_index]
    return amplitude * envelope * math.sin(2 * math.pi * frequency * offset / SAMPLE_RATE)


def counter_value(sample: int) -> float:
    return ((sample % COUNTER_MODULUS) - COUNTER_MODULUS // 2) * COUNTER_SCALE


def audio_samples(
    start_sample: int,
    count: int,
    mode: str = "meeting",
    amplitude: float = BURST_AMPLITUDE,
) -> array:
    """Return interleaved stereo Float32 samples for ``[start, start+count)``."""

    if mode not in ("meeting", "lossless"):
        raise ValueError("mode must be 'meeting' or 'lossless'")
    out = array("f", bytes(8 * count))
    for index in range(count):
        sample = start_sample + index
        offset = sample % EVENT_PERIOD_SAMPLES
        burst = 0.0
        if offset < BURST_SAMPLES:
            burst = _burst_value(
                offset,
                (sample // EVENT_PERIOD_SAMPLES) % len(BURST_FREQUENCIES_HZ),
                amplitude,
            )
        out[2 * index] = burst
        out[2 * index + 1] = counter_value(sample) if mode == "lossless" else burst
    return out


def _template_half_peak_offset(frequency_index: int) -> int:
    peak = max(abs(_burst_value(k, frequency_index, 1.0)) for k in range(BURST_SAMPLES))
    for k in range(BURST_SAMPLES):
        if abs(_burst_value(k, frequency_index, 1.0)) >= 0.5 * peak:
            return k
    return 0


TEMPLATE_HALF_PEAK_OFFSET = tuple(
    _template_half_peak_offset(index) for index in range(len(BURST_FREQUENCIES_HZ))
)


def _goertzel_power(samples, start: int, end: int, frequency: float) -> float:
    coefficient = 2 * math.cos(2 * math.pi * frequency / SAMPLE_RATE)
    previous = previous2 = 0.0
    for index in range(start, end):
        current = samples[index] + coefficient * previous - previous2
        previous2, previous = previous, current
    return previous2 * previous2 + previous * previous - coefficient * previous * previous2


@dataclass
class AudioEvent:
    sample: int
    frequency_index: int
    peak: float


def detect_audio_events(samples, window: int = 48, refractory_s: float = 0.5) -> list[AudioEvent]:
    """Find burst onsets in a mono Float sequence.

    ``sample`` is the estimated first sample of the burst (the template's
    half-peak offset is removed), in the capture's own sample numbering.
    """

    total = len(samples)
    if total < BURST_SAMPLES:
        return []
    envelope = []
    for start in range(0, total - window + 1, window):
        peak = 0.0
        for index in range(start, start + window):
            value = samples[index]
            if value < 0:
                value = -value
            if value > peak:
                peak = value
        envelope.append(peak)
    ordered = sorted(envelope)
    reference = ordered[min(len(ordered) - 1, int(len(ordered) * 0.995))]
    if reference <= 1e-4:
        return []
    threshold = 0.4 * reference
    refractory_windows = int(refractory_s * SAMPLE_RATE / window)
    events = []
    last = -refractory_windows
    for index, value in enumerate(envelope):
        if value < threshold or index - last < refractory_windows:
            continue
        last = index
        if index == 0:
            continue  # capture began inside a burst; its onset is unknown
        begin = (index - 1) * window
        end = min(total, begin + BURST_SAMPLES + 2 * window)
        peak = max(abs(samples[i]) for i in range(begin, end))
        half = 0.5 * peak
        onset = begin
        while onset < end and abs(samples[onset]) < half:
            onset += 1
        body_start = onset + BURST_RAMP_SAMPLES
        body_end = min(total, onset + BURST_SAMPLES - 2 * BURST_RAMP_SAMPLES)
        if body_end - body_start < 240:
            continue
        powers = [
            _goertzel_power(samples, body_start, body_end, frequency)
            for frequency in BURST_FREQUENCIES_HZ
        ]
        frequency_index = max(range(len(powers)), key=powers.__getitem__)
        events.append(
            AudioEvent(
                sample=onset - TEMPLATE_HALF_PEAK_OFFSET[frequency_index],
                frequency_index=frequency_index,
                peak=peak,
            )
        )
    return events


@dataclass
class CounterReport:
    samples: int = 0
    step: float | None = None
    continuous: int = 0
    duplicates: int = 0
    gap_events: int = 0
    gap_samples: int = 0
    regressions: int = 0
    discontinuities: list[tuple[int, int]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "samples": self.samples,
            "gain": None if self.step is None else self.step / COUNTER_SCALE,
            "continuous_steps": self.continuous,
            "duplicate_samples": self.duplicates,
            "gap_events": self.gap_events,
            "gap_samples": self.gap_samples,
            "regressions": self.regressions,
            "first_discontinuities": self.discontinuities[:20],
        }


def analyze_counter(samples, max_listed: int = 100) -> CounterReport:
    """Classify each step of a lossless-mode counter channel.

    The channel may pass through a constant gain. The step size is estimated
    from the median of small positive differences.
    """

    report = CounterReport(samples=len(samples))
    if len(samples) < 2:
        return report
    probe = []
    for index in range(1, min(len(samples), 48_001)):
        delta = samples[index] - samples[index - 1]
        if 0 < delta < 4 * COUNTER_SCALE * 4:
            probe.append(delta)
    if not probe:
        return report
    rough = statistics.median(probe)
    near = [delta for delta in probe if abs(delta - rough) < 0.25 * rough]
    step = statistics.fmean(near)
    report.step = step
    half = COUNTER_MODULUS // 2
    for index in range(1, len(samples)):
        units = round((samples[index] - samples[index - 1]) / step)
        units = (units + half) % COUNTER_MODULUS - half
        if units == 1:
            report.continuous += 1
            continue
        if units == 0:
            report.duplicates += 1
        elif units > 1:
            report.gap_events += 1
            report.gap_samples += units - 1
        else:
            report.regressions += 1
        if len(report.discontinuities) < max_listed:
            report.discontinuities.append((index, units))
    return report


# --- capture analysis ------------------------------------------------------


@dataclass
class VideoReport:
    frames: int = 0
    decoded: int = 0
    undecodable: int = 0
    step_histogram: dict[int, int] = field(default_factory=dict)
    repeats: int = 0
    skip_events: int = 0
    skipped_grains: int = 0
    regressions: int = 0
    longest_freeze_frames: int = 0
    longest_freeze_s: float = 0.0
    first_grain: int | None = None
    last_grain: int | None = None

    def as_dict(self) -> dict:
        return {
            "frames": self.frames,
            "decoded": self.decoded,
            "undecodable": self.undecodable,
            "step_histogram": {str(k): v for k, v in sorted(self.step_histogram.items())},
            "repeats": self.repeats,
            "skip_events": self.skip_events,
            "skipped_grains": self.skipped_grains,
            "regressions": self.regressions,
            "longest_freeze_frames": self.longest_freeze_frames,
            "longest_freeze_s": round(self.longest_freeze_s, 4),
            "first_grain_code": self.first_grain,
            "last_grain_code": self.last_grain,
        }


def analyze_video(decoded: list[tuple[float, int | None]]) -> tuple[VideoReport, dict[int, float]]:
    """Summarize ``(capture_time_s, grain_or_None)`` frames.

    Returns the report and the first capture time of every event grain seen.
    """

    report = VideoReport(frames=len(decoded))
    events: dict[int, float] = {}
    previous = None
    freeze_start = None
    freeze_frames = 0
    undecodable_between = 0
    for capture_time, grain in decoded:
        if grain is None:
            report.undecodable += 1
            undecodable_between += 1
            continue
        report.decoded += 1
        if report.first_grain is None:
            report.first_grain = grain
        report.last_grain = grain
        if is_event_grain(grain) and grain not in events:
            events[grain] = capture_time
        if previous is not None:
            step = wrap_delta(grain, previous[1])
            report.step_histogram[step] = report.step_histogram.get(step, 0) + 1
            if step == 0:
                report.repeats += 1
                if freeze_start is None:
                    freeze_start = previous[0]
                    freeze_frames = 1
                freeze_frames += 1
                if freeze_frames > report.longest_freeze_frames:
                    report.longest_freeze_frames = freeze_frames
                    report.longest_freeze_s = capture_time - freeze_start
            else:
                freeze_start = None
                freeze_frames = 0
                # Frames captured but undecodable occupied grains in the gap;
                # they are limited separately and are not missing.
                missing = step - 1 - undecodable_between
                if missing > 0:
                    report.skip_events += 1
                    report.skipped_grains += missing
                elif step < 0:
                    report.regressions += 1
        previous = (capture_time, grain)
        undecodable_between = 0
    return report, events


@dataclass
class SyncPair:
    grain: int
    video_s: float
    audio_s: float

    @property
    def offset_ms(self) -> float:
        return (self.audio_s - self.video_s) * 1000


def pair_events(
    video_events: dict[int, float],
    audio_events: list[AudioEvent],
    audio_start_s: float = 0.0,
    max_offset_s: float = 2.0,
) -> tuple[list[SyncPair], int]:
    """Pair each audio burst with the nearest video flash of the same code."""

    pairs = []
    unmatched = 0
    for event in audio_events:
        audio_time = audio_start_s + event.sample / SAMPLE_RATE
        best = None
        for grain, video_time in video_events.items():
            if event_code(grain) != event.frequency_index:
                continue
            distance = abs(audio_time - video_time)
            if distance <= max_offset_s and (best is None or distance < best[0]):
                best = (distance, grain, video_time)
        if best is None:
            unmatched += 1
            continue
        pairs.append(SyncPair(grain=best[1], video_s=best[2], audio_s=audio_time))
    return pairs, unmatched


def sync_summary(pairs: list[SyncPair]) -> dict:
    if not pairs:
        return {"pairs": 0}
    offsets = [pair.offset_ms for pair in pairs]
    summary = {
        "pairs": len(pairs),
        "offset_ms_min": round(min(offsets), 3),
        "offset_ms_median": round(statistics.median(offsets), 3),
        "offset_ms_max": round(max(offsets), 3),
        "offset_ms_range": round(max(offsets) - min(offsets), 3),
        "drift_ppm": None,
    }
    if len(pairs) >= 3:
        times = [pair.video_s for pair in pairs]
        mean_t = statistics.fmean(times)
        mean_o = statistics.fmean(offsets)
        denominator = sum((t - mean_t) ** 2 for t in times)
        if denominator > 0:
            slope_ms_per_s = sum(
                (t - mean_t) * (o - mean_o) for t, o in zip(times, offsets)
            ) / denominator
            summary["drift_ppm"] = round(slope_ms_per_s * 1000, 3)
    return summary


@dataclass
class Thresholds:
    audio_lead_ms: float = 40.0
    audio_lag_ms: float = 60.0
    max_offset_range_ms: float = 20.0
    max_drift_ppm: float = 100.0
    max_undecodable_fraction: float = 0.01
    min_pairs: int = 3
    # Capture completeness is part of the verdict. A path that legitimately
    # reduces frame rate (a meeting) must say so with a non-zero allowance;
    # None disables the check.
    max_skipped_grains: int | None = 0
    max_repeats: int | None = None
    # Bursts whose flash frame fell just outside the capture window.
    max_unmatched_events: int = 2


def verdict(report: dict, thresholds: Thresholds) -> list[str]:
    """Return failure reasons; an empty list means the capture passed."""

    failures = []
    video = report["video"]
    sync = report["sync"]
    if video["frames"] == 0:
        return ["no video frames"]
    if video["undecodable"] / video["frames"] > thresholds.max_undecodable_fraction:
        failures.append("undecodable frame fraction above limit")
    if video["regressions"]:
        failures.append("video grain regressions")
    if thresholds.max_skipped_grains is not None and video["skipped_grains"] > thresholds.max_skipped_grains:
        failures.append("video grains missing from capture")
    if thresholds.max_repeats is not None and video["repeats"] > thresholds.max_repeats:
        failures.append("video repeats above limit")
    audio = report.get("audio")
    if audio and audio["unmatched_events"] > thresholds.max_unmatched_events:
        failures.append("unmatched audio events above limit")
    if sync.get("pairs", 0) < thresholds.min_pairs:
        failures.append("too few paired A/V events")
    else:
        if sync["offset_ms_min"] < -thresholds.audio_lead_ms:
            failures.append("audio leads video beyond limit")
        if sync["offset_ms_max"] > thresholds.audio_lag_ms:
            failures.append("audio lags video beyond limit")
        if sync["offset_ms_range"] > thresholds.max_offset_range_ms:
            failures.append("A/V offset range above limit")
        drift = sync.get("drift_ppm")
        if drift is not None and abs(drift) > thresholds.max_drift_ppm:
            failures.append("A/V drift above limit")
    counter = report.get("counter")
    if counter and counter.get("gain") is not None:
        if counter["gap_events"] or counter["duplicate_samples"] or counter["regressions"]:
            failures.append("audio sample counter discontinuities")
    return failures
