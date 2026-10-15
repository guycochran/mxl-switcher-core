#!/usr/bin/env python3
"""Publish the paired MXL A/V reference source (video + 48 kHz audio).

Video: 1080p30 v210 with moving timecode, a once-per-second flash patch, and
a machine-readable frame code. Audio: 48 kHz stereo Float32 with a 20 ms
tone burst starting on the first sample of every flash frame. See
``av_reference.py`` for the timeline and the two audio modes.

Both flows are written by one GStreamer pipeline from one frame counter, so
their buffers share running time. The frame counter starts at the current
grain of the host's TAI clock, so the frame code also identifies wall time.
"""

from __future__ import annotations

import argparse
import json
import signal
import time
import urllib.request
from array import array

import av_reference as ref
import mxl_demo_sources as draw
import numpy as np
from mxl_demo_sources import CANVAS_HEIGHT, CANVAS_WIDTH, OUTPUT_HEIGHT, OUTPUT_WIDTH

from gi.repository import Gst

AUDIO_CAPS = (
    "audio/x-raw,format=F32LE,layout=interleaved,rate=48000,"
    "channels=2,channel-mask=(bitmask)0x3"
)
AUDIO_CYCLE_SAMPLES = ref.EVENT_PERIOD_SAMPLES * len(ref.BURST_FREQUENCIES_HZ)
WHITE = (235, 235, 235)
BLACK = (16, 16, 16)
RUNNING = True


def stop(_signum: int, _frame: object) -> None:
    global RUNNING
    RUNNING = False


def current_grain() -> int:
    clock = getattr(time, "CLOCK_TAI", time.CLOCK_REALTIME)
    return time.clock_gettime_ns(clock) * ref.VIDEO_RATE // 1_000_000_000


def frame_pts(frame_offset: int) -> int:
    return frame_offset * 1_000_000_000 // ref.VIDEO_RATE


def draw_reference(template: np.ndarray, grain: int) -> np.ndarray:
    frame = template.copy()
    phase = (grain * 8) % (OUTPUT_WIDTH + 200) - 100
    draw.rectangle(frame, (phase - 40, 330), (phase + 40, 700), (40, 200, 255), -1)
    draw.rectangle(frame, (90, 72), (1560, 290), (12, 14, 24), -1)
    draw.put_text(frame, "MXL A/V REFERENCE", (140, 170), 1.9, (255, 255, 255), 5)
    draw.put_text(
        frame,
        f"48 kHz burst on every flash  |  event {ref.event_code(grain)}",
        (145, 250),
        0.8,
        (80, 220, 255),
        2,
    )
    x0, y0, x1, y1 = ref.FLASH_RECT
    draw.rectangle(frame, (x0, y0), (x1, y1), WHITE if ref.flash_on(grain) else BLACK, -1)
    draw.rectangle(frame, (300, 720), (1620, 900), (8, 10, 17), -1)
    draw.put_text(frame, ref.timecode(grain), (360, 865), 3.0, (90, 255, 125), 6)
    draw.rectangle(frame, (60, 920), (1860, 1060), BLACK, -1)
    for (bx0, by0, bx1, by1), bit in ref.code_blocks(grain):
        draw.rectangle(frame, (bx0, by0), (bx1 - 1, by1 - 1), WHITE if bit else BLACK, -1)
    return frame


def base_frame() -> np.ndarray:
    frame = np.empty((CANVAS_HEIGHT, CANVAS_WIDTH, 3), dtype=np.uint8)
    frame[:] = (48, 44, 40)
    return frame


class AudioSynth:
    """Produce reference samples by absolute sample index."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.cycle = ref.audio_samples(0, AUDIO_CYCLE_SAMPLES, "meeting")

    def block(self, start_sample: int, count: int) -> bytes:
        out = array("f", bytes(8 * count))
        for index in range(count):
            sample = start_sample + index
            burst = self.cycle[2 * (sample % AUDIO_CYCLE_SAMPLES)]
            out[2 * index] = burst
            out[2 * index + 1] = (
                ref.counter_value(sample) if self.mode == "lossless" else burst
            )
        return out.tobytes()


def pipeline_description(
    domain: str, video_flow: str, audio_flow: str, group: str, label: str
) -> str:
    return (
        "appsrc name=video is-live=true block=true format=time "
        f"caps=video/x-raw,format=BGR,width={CANVAS_WIDTH},height={CANVAS_HEIGHT},"
        f"framerate={ref.VIDEO_RATE}/1 "
        "! queue max-size-buffers=2 leaky=downstream "
        "! videoscale n-threads=4 ! videoconvert n-threads=4 "
        f"! video/x-raw,format=v210,width={OUTPUT_WIDTH},height={OUTPUT_HEIGHT},"
        f"framerate={ref.VIDEO_RATE}/1 "
        f'! mxlsink domain={domain} flow-id={video_flow} label="{label} Video" '
        f'description="{label} frame-coded video" group-hint="{group}:Video" '
        "sync=true async=false max-lateness=-1 "
        f"appsrc name=audio is-live=true block=true format=time caps={AUDIO_CAPS} "
        "! queue max-size-time=200000000 max-size-buffers=0 max-size-bytes=0 "
        f'! mxlsink domain={domain} flow-id={audio_flow} label="{label} Audio" '
        f'description="{label} 48 kHz event audio" group-hint="{group}:Audio" '
        "sync=true async=false"
    )


def push(source, payload: bytes, pts: int, duration: int) -> None:
    buffer = Gst.Buffer.new_allocate(None, len(payload), None)
    buffer.fill(0, payload)
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
    if args.core_url:
        register_source(args.core_url, args.source_id, args.video_flow, args.audio_flow, args.label)

    template = base_frame()
    synth = AudioSynth(args.audio_mode)
    first_grain = current_grain()
    offset = 0
    deadline = time.monotonic()
    print(
        f"av_reference=ready mode={args.audio_mode} first_grain={first_grain} "
        f"rate={ref.VIDEO_RATE}/1 sample_rate={ref.SAMPLE_RATE}",
        flush=True,
    )
    try:
        while RUNNING:
            grain = first_grain + offset
            pts = frame_pts(offset)
            duration = frame_pts(offset + 1) - pts
            push(video, draw_reference(template, grain).tobytes(), pts, duration)
            push(
                audio,
                synth.block(ref.grain_to_sample(grain), ref.SAMPLES_PER_FRAME),
                pts,
                duration,
            )
            offset += 1
            deadline += 1 / ref.VIDEO_RATE
            delay = deadline - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.5:
                # Keep the content timeline continuous; report instead of jumping.
                print(f"late_s={-delay:.3f} frame_offset={offset}", flush=True)
                deadline = time.monotonic()
    finally:
        video.emit("end-of-stream")
        audio.emit("end-of-stream")
        pipeline.set_state(Gst.State.NULL)
    print(f"frames={offset} last_grain={first_grain + offset - 1}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
