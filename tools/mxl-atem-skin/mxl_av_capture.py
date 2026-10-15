#!/usr/bin/env python3
"""Capture a paired MXL video/audio flow into ``mxl_av_analyze.py`` raw inputs.

Both ``mxlsrc`` branches run in one pipeline, so their buffer timestamps share
one timeline. Writes ``video.y8`` (8-bit luma), ``video.pts`` (seconds per
frame), ``audio.f32`` (interleaved Float32 48 kHz stereo), and ``meta.json``
with the first audio timestamp, buffer counts, timestamp gaps, and arrival
times. The analyzer arguments are printed at the end.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

AUDIO_CAPS = (
    "audio/x-raw,format=F32LE,layout=interleaved,rate=48000,"
    "channels=2,channel-mask=(bitmask)0x3"
)
BYTES_PER_FRAME = 8


def pipeline_description(domain: str, video_flow: str, audio_flow: str, width: int, height: int) -> str:
    return (
        f"mxlsrc domain={domain} video-flow-id={video_flow} automatic-eos=false "
        "! queue max-size-time=2000000000 max-size-buffers=0 max-size-bytes=0 "
        "! videoconvert n-threads=2 ! videoscale n-threads=2 "
        f"! video/x-raw,format=GRAY8,width={width},height={height} "
        "! appsink name=video sync=false emit-signals=true max-buffers=0 "
        f"mxlsrc domain={domain} audio-flow-id={audio_flow} automatic-eos=false "
        f"! {AUDIO_CAPS} "
        "! queue max-size-time=2000000000 max-size-buffers=0 max-size-bytes=0 "
        "! appsink name=audio sync=false emit-signals=true max-buffers=0"
    )


class Recorder:
    def __init__(self, directory: str, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.lock = threading.Lock()
        self.video = open(os.path.join(directory, "video.y8"), "wb")
        self.video_pts = open(os.path.join(directory, "video.pts"), "w")
        self.audio = open(os.path.join(directory, "audio.f32"), "wb")
        self.started = time.monotonic()
        self.meta = {
            "video_frames": 0,
            "video_first_pts_s": None,
            "video_first_arrival_s": None,
            "video_pts_steps_ns": {},
            "audio_buffers": 0,
            "audio_samples": 0,
            "audio_first_pts_s": None,
            "audio_first_arrival_s": None,
            "audio_pts_gaps": 0,
            "audio_pts_gap_max_ns": 0,
        }
        self.last_video_pts = None
        self.next_audio_pts = None

    def on_video(self, sink):
        sample = sink.emit("pull-sample")
        buffer = sample.get_buffer()
        ok, info = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.ERROR
        try:
            data = info.data[: self.width * self.height]
            with self.lock:
                pts = buffer.pts
                if self.meta["video_first_pts_s"] is None:
                    self.meta["video_first_pts_s"] = pts / 1e9
                    self.meta["video_first_arrival_s"] = time.monotonic() - self.started
                if self.last_video_pts is not None:
                    step = str(pts - self.last_video_pts)
                    steps = self.meta["video_pts_steps_ns"]
                    steps[step] = steps.get(step, 0) + 1
                self.last_video_pts = pts
                self.video.write(data)
                self.video_pts.write(f"{pts / 1e9:.9f}\n")
                self.meta["video_frames"] += 1
        finally:
            buffer.unmap(info)
        return Gst.FlowReturn.OK

    def on_audio(self, sink):
        sample = sink.emit("pull-sample")
        buffer = sample.get_buffer()
        ok, info = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.ERROR
        try:
            frames = len(info.data) // BYTES_PER_FRAME
            with self.lock:
                pts = buffer.pts
                if self.meta["audio_first_pts_s"] is None:
                    self.meta["audio_first_pts_s"] = pts / 1e9
                    self.meta["audio_first_arrival_s"] = time.monotonic() - self.started
                elif self.next_audio_pts is not None and pts != self.next_audio_pts:
                    gap = abs(pts - self.next_audio_pts)
                    if gap > 1000:  # ignore sub-microsecond rounding
                        self.meta["audio_pts_gaps"] += 1
                        self.meta["audio_pts_gap_max_ns"] = max(self.meta["audio_pts_gap_max_ns"], gap)
                self.next_audio_pts = pts + frames * 1_000_000_000 // 48_000
                self.audio.write(info.data[: frames * BYTES_PER_FRAME])
                self.meta["audio_buffers"] += 1
                self.meta["audio_samples"] += frames
        finally:
            buffer.unmap(info)
        return Gst.FlowReturn.OK

    def close(self) -> dict:
        with self.lock:
            for handle in (self.video, self.video_pts, self.audio):
                handle.close()
            return dict(self.meta)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--domain", default="/mxl-domain")
    parser.add_argument("--video-flow", required=True)
    parser.add_argument("--audio-flow", required=True)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--width", type=int, default=480)
    parser.add_argument("--height", type=int, default=270)
    parser.add_argument("--output", required=True, help="new directory for the capture")
    args = parser.parse_args()

    os.makedirs(args.output)
    Gst.init(None)
    pipeline = Gst.parse_launch(
        pipeline_description(args.domain, args.video_flow, args.audio_flow, args.width, args.height)
    )
    recorder = Recorder(args.output, args.width, args.height)
    pipeline.get_by_name("video").connect("new-sample", recorder.on_video)
    pipeline.get_by_name("audio").connect("new-sample", recorder.on_audio)
    if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        raise RuntimeError("capture pipeline failed to start")
    bus = pipeline.get_bus()
    deadline = time.monotonic() + args.seconds
    error = None
    while time.monotonic() < deadline:
        message = bus.timed_pop_filtered(200 * Gst.MSECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
        if message:
            error = message.parse_error()[1] if message.type == Gst.MessageType.ERROR else "unexpected EOS"
            break
    pipeline.set_state(Gst.State.NULL)
    meta = recorder.close()
    meta.update({"seconds": args.seconds, "width": args.width, "height": args.height, "error": error})
    audio_start = meta["audio_first_pts_s"] or 0.0
    meta["analyze_args"] = [
        "--video-raw", os.path.join(args.output, "video.y8"),
        "--video-pts", os.path.join(args.output, "video.pts"),
        "--width", str(args.width), "--height", str(args.height),
        "--audio-raw", os.path.join(args.output, "audio.f32"),
        "--audio-start-s", f"{audio_start:.9f}",
    ]
    with open(os.path.join(args.output, "meta.json"), "w") as handle:
        json.dump(meta, handle, indent=2)
    print(json.dumps(meta), flush=True)
    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
