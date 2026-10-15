#!/usr/bin/env python3
"""Capture a paired MXL video/audio flow into ``mxl_av_analyze.py`` inputs.

Both ``mxlsrc`` branches run in one pipeline, so their buffer timestamps share
one timeline. Video is read as v210 with no conversion; each grain's frame
code is decoded from ~400 sampled pixels. Writes ``video.codes`` (capture
time and code per grain), ``audio.f32`` (interleaved Float32 48 kHz stereo),
and ``meta.json``. The metadata separates index steps (from timestamps, so
reader-side loss) from content steps (from codes, so publisher-side loss)
and lists the exact analyzer arguments.

It also reconstructs each buffer's MXL index from its timestamp, the pipeline
base time, and the sampled MXL clock offset (as gst-mxl-rs maps it). For a
reference published with MXL-anchored timestamps, the video code must equal
the grain index and a lossless counter must equal the sample index. Any
other difference is reported.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import threading
import time

import av_reference as ref
import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

AUDIO_CAPS = (
    "audio/x-raw,format=F32LE,layout=interleaved,rate=48000,"
    "channels=2,channel-mask=(bitmask)0x3"
)
BYTES_PER_FRAME = 8


def pipeline_description(domain: str, video_flow: str, audio_flow: str) -> str:
    return (
        f"mxlsrc domain={domain} video-flow-id={video_flow} automatic-eos=false "
        "! video/x-raw,format=v210 "
        "! queue max-size-time=2000000000 max-size-buffers=0 max-size-bytes=0 "
        "! appsink name=video sync=false emit-signals=true max-buffers=0 "
        f"mxlsrc domain={domain} audio-flow-id={audio_flow} automatic-eos=false "
        f"! {AUDIO_CAPS} "
        "! queue max-size-time=2000000000 max-size-buffers=0 max-size-bytes=0 "
        "! appsink name=audio sync=false emit-signals=true max-buffers=0"
    )


def bump(histogram: dict, key: int) -> None:
    histogram[str(key)] = histogram.get(str(key), 0) + 1


class Recorder:
    def __init__(self, directory: str) -> None:
        self.lock = threading.Lock()
        self.video = open(os.path.join(directory, "video.codes"), "w")
        self.audio = open(os.path.join(directory, "audio.f32"), "wb")
        self.started = time.monotonic()
        self.meta = {
            "video_frames": 0,
            "video_first_pts_s": None,
            "video_first_arrival_s": None,
            "video_index_steps": {},
            "video_content_steps": {},
            "video_undecodable": 0,
            "video_content_index_mismatches": 0,
            "video_max_decode_ms": 0.0,
            "video_index_minus_code": {},
            "audio_index_minus_counter": {},
            "audio_buffers": 0,
            "audio_samples": 0,
            "audio_first_pts_s": None,
            "audio_first_arrival_s": None,
            "audio_pts_gaps": 0,
            "audio_pts_gap_max_ns": 0,
        }
        self.last_video_pts = None
        self.last_code = None
        self.anchor = None  # base_time + MXL clock offset, set once PLAYING
        self.next_audio_pts = None

    def on_video(self, sink):
        sample = sink.emit("pull-sample")
        buffer = sample.get_buffer()
        ok, info = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.ERROR
        try:
            decode_started = time.monotonic()
            code = ref.decode_v210_frame_code(info.data)
            decode_ms = (time.monotonic() - decode_started) * 1000
            with self.lock:
                meta = self.meta
                pts = buffer.pts
                meta["video_max_decode_ms"] = max(meta["video_max_decode_ms"], round(decode_ms, 3))
                if meta["video_first_pts_s"] is None:
                    meta["video_first_pts_s"] = pts / 1e9
                    meta["video_first_arrival_s"] = time.monotonic() - self.started
                index_step = None
                if self.last_video_pts is not None:
                    index_step = round((pts - self.last_video_pts) * ref.VIDEO_RATE / 1e9)
                    bump(meta["video_index_steps"], index_step)
                if code is None:
                    meta["video_undecodable"] += 1
                else:
                    if self.anchor is not None:
                        index = ref.ns_to_index(pts + self.anchor, ref.VIDEO_RATE)
                        bump(meta["video_index_minus_code"], ref.wrap_delta(index % ref.CODE_MODULUS, code))
                    if self.last_code is not None:
                        content_step = ref.wrap_delta(code, self.last_code)
                        bump(meta["video_content_steps"], content_step)
                        if index_step is not None and content_step != index_step:
                            meta["video_content_index_mismatches"] += 1
                    self.last_code = code
                self.last_video_pts = pts
                self.video.write(f"{pts / 1e9:.9f} {'-' if code is None else code}\n")
                meta["video_frames"] += 1
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
                if self.anchor is not None and frames:
                    counter = struct.unpack_from("<f", info.data, 4)[0]
                    value = round(counter / ref.COUNTER_SCALE) + ref.COUNTER_MODULUS // 2
                    index = ref.ns_to_index(pts + self.anchor, ref.SAMPLE_RATE)
                    delta = (index - value) % ref.COUNTER_MODULUS
                    if delta >= ref.COUNTER_MODULUS // 2:
                        delta -= ref.COUNTER_MODULUS
                    bump(self.meta["audio_index_minus_counter"], delta)
                self.audio.write(info.data[: frames * BYTES_PER_FRAME])
                self.meta["audio_buffers"] += 1
                self.meta["audio_samples"] += frames
        finally:
            buffer.unmap(info)
        return Gst.FlowReturn.OK

    def close(self) -> dict:
        with self.lock:
            for handle in (self.video, self.audio):
                handle.close()
            return dict(self.meta)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--domain", default="/mxl-domain")
    parser.add_argument("--video-flow", required=True)
    parser.add_argument("--audio-flow", required=True)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--output", required=True, help="new directory for the capture")
    args = parser.parse_args()

    os.makedirs(args.output)
    Gst.init(None)
    pipeline = Gst.parse_launch(
        pipeline_description(args.domain, args.video_flow, args.audio_flow)
    )
    recorder = Recorder(args.output)
    pipeline.get_by_name("video").connect("new-sample", recorder.on_video)
    pipeline.get_by_name("audio").connect("new-sample", recorder.on_audio)
    if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        raise RuntimeError("capture pipeline failed to start")
    pipeline.get_state(5 * Gst.SECOND)
    tai_clock = getattr(time, "CLOCK_TAI", time.CLOCK_REALTIME)
    offset = ref.tightest_clock_offset(pipeline.get_clock().get_time, lambda: time.clock_gettime_ns(tai_clock))
    with recorder.lock:
        recorder.anchor = pipeline.get_base_time() + offset
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
    meta.update({"seconds": args.seconds, "error": error})
    audio_start = meta["audio_first_pts_s"] or 0.0
    meta["analyze_args"] = [
        "--video-codes", os.path.join(args.output, "video.codes"),
        "--audio-raw", os.path.join(args.output, "audio.f32"),
        "--audio-start-s", f"{audio_start:.9f}",
    ]
    with open(os.path.join(args.output, "meta.json"), "w") as handle:
        json.dump(meta, handle, indent=2)
    print(json.dumps(meta), flush=True)
    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
