#!/usr/bin/env python3
"""Publish the paired MXL A/V reference source (video + 48 kHz audio).

Video: 1080p30 v210 with moving timecode, a once-per-second flash patch, and
a machine-readable frame code. Audio: 48 kHz stereo Float32 with a 20 ms
tone burst starting on the first sample of every flash frame. See
``av_reference.py`` for the timeline and the two audio modes.

Both flows are written by one GStreamer pipeline from one frame counter.
Buffer timestamps are anchored to MXL (TAI) time, so the sink writes video
grain ``g`` at MXL index ``g`` and audio sample ``g * 1600`` at index
``g * 1600``. The frame code therefore equals the MXL index, and the pair's
alignment is deterministic. Without the anchor, the sink quantizes each run's
arbitrary video phase to a grain, giving a constant but run-dependent offset
of up to half a grain.

The picture is greyscale and packed to v210 here, updating only the regions
that change, so the pipeline has no scaler, colour converter, or leaky queue
that could silently drop grains. Every pushed frame is counted, and any frame
that misses its deadline is logged.
"""

from __future__ import annotations

import argparse
import json
import signal
import time
import urllib.request

import av_reference as ref
import cv2
import gi
import numpy as np

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

WIDTH = ref.REFERENCE_WIDTH
HEIGHT = ref.REFERENCE_HEIGHT
GROUPS = WIDTH // 6
AUDIO_CAPS = (
    "audio/x-raw,format=F32LE,layout=interleaved,rate=48000,"
    "channels=2,channel-mask=(bitmask)0x3"
)
AUDIO_CYCLE_SAMPLES = ref.EVENT_PERIOD_SAMPLES * len(ref.BURST_FREQUENCIES_HZ)
BACKGROUND = 40
WHITE = 235
BLACK = 16
PANEL = 24
TEXT = 235
# Regions redrawn every frame: (y0, y1, x0, x1); x is a multiple of 6.
BAR_REGION = (330, 400, 0, WIDTH)
TIMECODE_REGION = (720, 900, 300, 1620)
CODE_REGION = (920, 1060, 60, 1860)
FLASH_REGION = (ref.FLASH_RECT[1], ref.FLASH_RECT[3], ref.FLASH_RECT[0], ref.FLASH_RECT[2])
DYNAMIC_REGIONS = (BAR_REGION, TIMECODE_REGION, CODE_REGION, FLASH_REGION)
RUNNING = True


def stop(_signum: int, _frame: object) -> None:
    global RUNNING
    RUNNING = False


TAI = getattr(time, "CLOCK_TAI", time.CLOCK_REALTIME)
START_LEAD_NS = 300_000_000


def mxl_clock_offset(pipeline, samples: int = 50) -> int:
    """Estimate ``D = mxl_now - pipeline_clock_now`` as gst-mxl-rs samples it.

    Uses the tightest of several bracketed reads so a preemption between the
    two clock reads on a loaded host cannot skew the estimate.
    """

    return ref.tightest_clock_offset(pipeline.get_clock().get_time, lambda: time.clock_gettime_ns(TAI), samples)


def grain_pts(grain: int, base_time: int, offset: int) -> int:
    """Buffer PTS that the sink maps to MXL index ``grain`` (and sample ``grain * 1600``)."""

    return ref.index_to_ns(grain, ref.VIDEO_RATE) - base_time - offset


def pack_v210(luma: np.ndarray) -> np.ndarray:
    """Pack an (H, W) uint8 luma array (W % 6 == 0) into (H, W // 6 * 4) words."""

    y = luma.astype(np.uint32).reshape(luma.shape[0], -1, 6) << 2
    words = np.empty((luma.shape[0], y.shape[1], 4), dtype=np.uint32)
    words[:, :, 0] = 512 | (y[:, :, 0] << 10) | (512 << 20)
    words[:, :, 1] = y[:, :, 1] | (512 << 10) | (y[:, :, 2] << 20)
    words[:, :, 2] = 512 | (y[:, :, 3] << 10) | (512 << 20)
    words[:, :, 3] = y[:, :, 4] | (512 << 10) | (y[:, :, 5] << 20)
    return words.reshape(luma.shape[0], -1)


class Picture:
    """A luma canvas and its v210 image, repacked only where it changes."""

    def __init__(self) -> None:
        if ref.v210_stride(WIDTH) != GROUPS * 16:
            raise ValueError("v210 stride must equal the packed row size")
        self.luma = np.full((HEIGHT, WIDTH), BACKGROUND, dtype=np.uint8)
        cv2.rectangle(self.luma, (90, 72), (1560, 290), PANEL, -1)
        text(self.luma, "MXL A/V REFERENCE", (140, 175), 1.9, 5)
        text(self.luma, "48 kHz burst on every flash frame", (145, 255), 0.8, 2)
        self.v210 = pack_v210(self.luma)

    def draw(self, grain: int) -> None:
        luma = self.luma
        y0, y1, x0, x1 = BAR_REGION
        luma[y0:y1, x0:x1] = BACKGROUND
        phase = (grain * 8) % (WIDTH + 200) - 100
        cv2.rectangle(luma, (phase - 40, y0), (phase + 40, y1 - 1), 200, -1)
        y0, y1, x0, x1 = TIMECODE_REGION
        luma[y0:y1, x0:x1] = PANEL
        text(luma, ref.timecode(grain), (360, 865), 3.0, 6)
        y0, y1, x0, x1 = CODE_REGION
        luma[y0:y1, x0:x1] = BLACK
        for (bx0, by0, bx1, by1), bit in ref.code_blocks(grain):
            luma[by0:by1, bx0:bx1] = WHITE if bit else BLACK
        y0, y1, x0, x1 = FLASH_REGION
        luma[y0:y1, x0:x1] = WHITE if ref.flash_on(grain) else BLACK
        for y0, y1, x0, x1 in DYNAMIC_REGIONS:
            self.v210[y0:y1, x0 // 6 * 4:x1 // 6 * 4] = pack_v210(luma[y0:y1, x0:x1])

    def payload(self) -> bytes:
        return self.v210.tobytes()


def text(luma: np.ndarray, value: str, origin: tuple[int, int], scale: float, thickness: int) -> None:
    cv2.putText(luma, value, origin, cv2.FONT_HERSHEY_DUPLEX, scale, TEXT, thickness, cv2.LINE_AA)


class AudioSynth:
    """Produce reference samples by absolute sample index."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        cycle = ref.audio_samples(0, AUDIO_CYCLE_SAMPLES, "meeting")
        self.cycle = np.frombuffer(cycle.tobytes(), dtype=np.float32)[0::2].copy()

    def block(self, start_sample: int, count: int) -> bytes:
        samples = np.arange(count, dtype=np.int64) + start_sample
        out = np.empty((count, 2), dtype=np.float32)
        out[:, 0] = self.cycle[samples % AUDIO_CYCLE_SAMPLES]
        if self.mode == "lossless":
            counter = (samples % ref.COUNTER_MODULUS) - ref.COUNTER_MODULUS // 2
            out[:, 1] = counter.astype(np.float32) * np.float32(ref.COUNTER_SCALE)
        else:
            out[:, 1] = out[:, 0]
        return out.tobytes()


def pipeline_description(
    domain: str, video_flow: str, audio_flow: str, group: str, label: str
) -> str:
    return (
        "appsrc name=video is-live=true block=true format=time "
        f"caps=video/x-raw,format=v210,width={WIDTH},height={HEIGHT},"
        f"framerate={ref.VIDEO_RATE}/1 "
        f'! mxlsink domain={domain} flow-id={video_flow} label="{label} Video" '
        f'description="{label} frame-coded video" group-hint="{group}:Video" '
        "sync=true async=false max-lateness=-1 "
        f"appsrc name=audio is-live=true block=true format=time caps={AUDIO_CAPS} "
        f'! mxlsink domain={domain} flow-id={audio_flow} label="{label} Audio" '
        f'description="{label} 48 kHz event audio" group-hint="{group}:Audio" '
        "sync=true async=false max-lateness=-1"
    )


def push(source, payload: bytes, pts: int, duration: int) -> None:
    buffer = Gst.Buffer.new_wrapped(payload)
    buffer.pts = pts
    buffer.dts = pts
    buffer.duration = duration
    result = source.emit("push-buffer", buffer)
    if result != Gst.FlowReturn.OK:
        raise RuntimeError(f"A/V reference push failed: {result.value_nick}")


def register_source(core_url: str, source_id: str, video_flow: str, audio_flow: str, label: str) -> None:
    payload = json.dumps(
        {
            "id": source_id,
            "flow_id": video_flow,
            "label": label,
            "kind": "reference",
            "capabilities": ["video", "audio", "timecode", "reference", "health"],
            "audio_flow_id": audio_flow,
        }
    ).encode()
    request = urllib.request.Request(
        core_url.rstrip("/") + "/api/mxl/v1/sources",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        if response.status not in (200, 201):
            raise RuntimeError(f"Core rejected source registration with HTTP {response.status}")


class Stats:
    """Render cost and true lateness.

    ``late`` counts buffers handed to the sink after their running time had
    already passed on the pipeline clock; the sink then writes them late (it
    never drops them). Time spent blocked in ``push`` is the sink pacing to
    the clock and is reported separately, not as work.
    """

    def __init__(self) -> None:
        self.frames = 0
        self.late = 0
        self.max_late_ns = 0
        self.min_headroom_ns = None
        self.render_s = 0.0
        self.max_render_s = 0.0

    def observe(self, render_s: float, headroom_ns: int) -> None:
        self.frames += 1
        self.render_s += render_s
        self.max_render_s = max(self.max_render_s, render_s)
        if headroom_ns < 0:
            self.late += 1
            self.max_late_ns = max(self.max_late_ns, -headroom_ns)
        if self.min_headroom_ns is None or headroom_ns < self.min_headroom_ns:
            self.min_headroom_ns = headroom_ns

    def line(self, grain: int) -> str:
        average = self.render_s / self.frames if self.frames else 0.0
        headroom = (self.min_headroom_ns or 0) / 1e6
        return (
            f"stats frames={self.frames} last_grain={grain} late={self.late} "
            f"max_late_ms={self.max_late_ns / 1e6:.1f} min_headroom_ms={headroom:.1f} "
            f"avg_render_ms={average * 1000:.2f} max_render_ms={self.max_render_s * 1000:.2f}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--domain", default="/mxl-domain")
    parser.add_argument("--video-flow", required=True)
    parser.add_argument("--audio-flow", required=True)
    parser.add_argument("--group", default="MXLAVReference")
    parser.add_argument("--label", default="MXL AV Reference")
    parser.add_argument("--audio-mode", choices=("meeting", "lossless"), default="meeting")
    parser.add_argument("--core-url", help="optionally register with Core at runtime")
    parser.add_argument("--source-id", default="av-reference")
    parser.add_argument("--stats-every-s", type=float, default=10.0)
    args = parser.parse_args()

    Gst.init(None)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    pipeline = Gst.parse_launch(
        pipeline_description(args.domain, args.video_flow, args.audio_flow, args.group, args.label)
    )
    video = pipeline.get_by_name("video")
    audio = pipeline.get_by_name("audio")
    if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        raise RuntimeError("A/V reference pipeline failed to start")
    pipeline.get_state(5 * Gst.SECOND)
    base_time = pipeline.get_base_time()
    clock_offset = mxl_clock_offset(pipeline)
    if args.core_url:
        register_source(args.core_url, args.source_id, args.video_flow, args.audio_flow, args.label)

    picture = Picture()
    synth = AudioSynth(args.audio_mode)
    stats = Stats()
    first_grain = ref.ns_to_index(time.clock_gettime_ns(TAI) + START_LEAD_NS, ref.VIDEO_RATE) + 1
    clock = pipeline.get_clock()
    offset = 0
    next_stats = time.monotonic() + args.stats_every_s
    print(
        f"av_reference=ready mode={args.audio_mode} first_grain={first_grain} "
        f"rate={ref.VIDEO_RATE}/1 sample_rate={ref.SAMPLE_RATE} "
        f"base_time={base_time} clock_offset={clock_offset}",
        flush=True,
    )
    grain = first_grain
    try:
        while RUNNING:
            # The sinks pace the loop: push() blocks until each buffer is due.
            started = time.monotonic()
            grain = first_grain + offset
            pts = grain_pts(grain, base_time, clock_offset)
            duration = grain_pts(grain + 1, base_time, clock_offset) - pts
            picture.draw(grain)
            payload = picture.payload()
            samples = synth.block(ref.grain_to_sample(grain), ref.SAMPLES_PER_FRAME)
            render = time.monotonic() - started
            headroom = base_time + pts - clock.get_time()
            push(video, payload, pts, duration)
            push(audio, samples, pts, duration)
            stats.observe(render, headroom)
            if headroom < -500_000_000:
                # Content stays continuous; the sink writes late rather than skipping.
                print(f"late_ms={-headroom / 1e6:.1f} grain={grain}", flush=True)
            offset += 1
            now = time.monotonic()
            if now >= next_stats:
                print(stats.line(grain), flush=True)
                next_stats = now + args.stats_every_s
    finally:
        video.emit("end-of-stream")
        audio.emit("end-of-stream")
        pipeline.set_state(Gst.State.NULL)
    print(stats.line(grain), flush=True)
    print(f"frames={offset} last_grain={first_grain + offset - 1}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
