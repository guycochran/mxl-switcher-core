#!/usr/bin/env python3
"""Capture a paired MXL video/audio flow straight from the rings (no GStreamer).

Video: each grain's frame code is decoded from ~400 pixels read in place,
then validated after reading (see ``mxl_direct``); a grain the writer may have
lapped is recorded as torn (``-``) instead of under the wrong index. Audio:
480-sample blocks are copied in place and validated the same way. Both are
timestamped in MXL index time relative to the first video grain read, so the
analyzer measures content placement on the timeline a timestamp-aligning
receiver uses.

Writes ``video.codes``, ``audio.f32`` (interleaved Float32), and ``meta.json``
in the format ``mxl_av_analyze.py`` reads.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from array import array

import av_reference as ref
import mxl_direct as mxl

GRAIN_TIMEOUT_NS = 200_000_000
AUDIO_BLOCK = 480


def bump(histogram: dict, key) -> None:
    histogram[str(key)] = histogram.get(str(key), 0) + 1


def capture_video(reader: mxl.GrainReader, rate, epoch_ns, deadline, out, meta, stop) -> None:
    sampler = mxl.v210_code_sampler(ref.decode_v210_frame_code)
    index = reader.head()
    while index is None or index == 0:
        time.sleep(0.01)
        index = reader.head()
    last_code = None
    while time.monotonic() < deadline and not stop.is_set():
        result = reader.read(index, GRAIN_TIMEOUT_NS, sampler)
        if result.status in (mxl.MXL_ERR_OUT_OF_RANGE_TOO_EARLY, mxl.MXL_ERR_TIMEOUT):
            continue
        if result.status == mxl.MXL_ERR_OUT_OF_RANGE_TOO_LATE:
            # Fell behind the ring: resume at the oldest safe grain and record the gap.
            head = reader.head() or index
            resume = max(index + 1, head - reader.grain_count + 2)
            meta["video_reader_skipped"] += resume - index
            index = resume
            continue
        if result.status != mxl.MXL_STATUS_OK:
            meta["video_errors"][str(result.status)] = meta["video_errors"].get(str(result.status), 0) + 1
            stop.set()
            return
        time_s = (ref.index_to_ns(index, rate) - epoch_ns) / 1e9
        if result.torn:
            meta["video_torn"] += 1
            out.write(f"{time_s:.9f} -\n")
        else:
            code = result.value
            meta["video_frames"] += 1
            if code is None:
                meta["video_undecodable"] += 1
            else:
                bump(meta["video_index_minus_code"], ref.wrap_delta(index % ref.CODE_MODULUS, code))
                if last_code is not None:
                    bump(meta["video_content_steps"], ref.wrap_delta(code, last_code))
                last_code = code
            out.write(f"{time_s:.9f} {'-' if code is None else code}\n")
        index += 1


def capture_audio(reader: mxl.SampleReader, epoch_ns_holder, deadline, out, meta, stop) -> None:
    head = reader.head()
    while head is None or head == 0:
        time.sleep(0.005)
        head = reader.head()
    end = head - head % AUDIO_BLOCK
    meta["audio_first_index"] = end - AUDIO_BLOCK
    block = array("f", bytes(8 * AUDIO_BLOCK))
    while time.monotonic() < deadline and not stop.is_set():
        status, channels, torn = reader.read(end, AUDIO_BLOCK, 100_000_000, 2)
        if status == mxl.MXL_ERR_OUT_OF_RANGE_TOO_EARLY:
            continue
        if status == mxl.MXL_ERR_OUT_OF_RANGE_TOO_LATE or torn:
            # Keep the file sample-continuous so the counter reveals the loss.
            meta["audio_torn_or_late_blocks"] += 1
            out.write(bytes(8 * AUDIO_BLOCK))
            end += AUDIO_BLOCK
            continue
        if status != mxl.MXL_STATUS_OK:
            meta["audio_errors"][str(status)] = meta["audio_errors"].get(str(status), 0) + 1
            stop.set()
            return
        left = array("f", channels[0])
        right = array("f", channels[1] if len(channels) > 1 else channels[0])
        block[0::2] = left
        block[1::2] = right
        out.write(block.tobytes())
        meta["audio_samples"] += AUDIO_BLOCK
        end += AUDIO_BLOCK


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--domain", default="/mxl-domain")
    parser.add_argument("--video-flow", required=True)
    parser.add_argument("--audio-flow", required=True)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--output", required=True, help="new directory for the capture")
    parser.add_argument("--library", default="libmxl.so")
    args = parser.parse_args()

    os.makedirs(args.output)
    api = mxl.load_library(args.library)
    instance = api.mxlCreateInstance(args.domain.encode(), b"")
    if not instance:
        raise RuntimeError("mxlCreateInstance failed")
    probe = mxl.Reader(api, instance, args.video_flow)
    video_config = probe.config()
    probe.close()
    audio_probe = mxl.Reader(api, instance, args.audio_flow)
    audio_config = audio_probe.config()
    audio_probe.close()
    rate = video_config.common.grainRate.numerator
    if video_config.common.grainRate.denominator != 1 or rate != ref.VIDEO_RATE:
        raise RuntimeError("the reference capture expects 30/1 video")
    video = mxl.GrainReader(api, instance, args.video_flow, video_config.discrete.grainCount)
    audio = mxl.SampleReader(api, instance, args.audio_flow, audio_config.continuous.bufferLength)

    meta = {
        "reader": "direct",
        "video_grain_count": video_config.discrete.grainCount,
        "audio_buffer_length": audio_config.continuous.bufferLength,
        "audio_channels": audio_config.continuous.channelCount,
        "video_frames": 0, "video_torn": 0, "video_undecodable": 0, "video_reader_skipped": 0,
        "video_index_minus_code": {}, "video_content_steps": {}, "video_errors": {},
        "audio_samples": 0, "audio_torn_or_late_blocks": 0, "audio_errors": {}, "audio_first_index": None,
    }
    head = video.head() or 0
    epoch_ns = ref.index_to_ns(head, rate)
    deadline = time.monotonic() + args.seconds
    stop = threading.Event()
    with open(os.path.join(args.output, "video.codes"), "w") as codes, \
            open(os.path.join(args.output, "audio.f32"), "wb") as samples:
        threads = [
            threading.Thread(target=capture_video, args=(video, rate, epoch_ns, deadline, codes, meta, stop)),
            threading.Thread(target=capture_audio, args=(audio, None, deadline, samples, meta, stop)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    video.close()
    audio.close()
    api.mxlDestroyInstance(instance)

    audio_start_s = (ref.index_to_ns(meta["audio_first_index"], ref.SAMPLE_RATE) - epoch_ns) / 1e9
    meta["epoch_index"] = head
    meta["error"] = "reader error" if stop.is_set() else None
    meta["analyze_args"] = [
        "--video-codes", os.path.join(args.output, "video.codes"),
        "--audio-raw", os.path.join(args.output, "audio.f32"),
        "--audio-start-s", f"{audio_start_s:.9f}",
    ]
    with open(os.path.join(args.output, "meta.json"), "w") as handle:
        json.dump(meta, handle, indent=2)
    print(json.dumps(meta), flush=True)
    return 1 if stop.is_set() else 0


if __name__ == "__main__":
    raise SystemExit(main())
