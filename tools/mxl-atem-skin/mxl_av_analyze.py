#!/usr/bin/env python3
"""Measure an A/V reference capture: cadence, sample continuity, and lip sync.

Inputs are a media file (decoded with ffmpeg/ffprobe), pre-decoded frame
codes from ``mxl_av_capture.py``, or raw 8-bit luma frames, each with
interleaved Float32 48 kHz audio. Output is a JSON
report; the exit status is 0 when every threshold passes, 1 when any fails,
and 2 for input errors.

Offsets are ``audio_time - video_time``: positive means audio is late.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import subprocess
import sys
from array import array
from typing import BinaryIO, Iterator

import av_reference as ref

DEFAULT_ANALYSIS_WIDTH = 480
DEFAULT_ANALYSIS_HEIGHT = 270


def read_frames(stream: BinaryIO, width: int, height: int) -> Iterator[bytes]:
    size = width * height
    while True:
        frame = stream.read(size)
        if len(frame) < size:
            return
        yield frame


def read_codes(path: str) -> list[tuple[float, int | None]]:
    frames = []
    with open(path) as handle:
        for line in handle:
            fields = line.split()
            if len(fields) >= 2:
                frames.append((float(fields[0]), None if fields[1] == "-" else int(fields[1])))
    return frames


def read_audio(path_or_stream, channels: int) -> list[array]:
    data = array("f")
    if hasattr(path_or_stream, "read"):
        raw = path_or_stream.read()
    else:
        with open(path_or_stream, "rb") as handle:
            raw = handle.read()
    usable = len(raw) - len(raw) % (4 * channels)
    data.frombytes(raw[:usable])
    if sys.byteorder != "little":
        data.byteswap()
    return [data[channel::channels] for channel in range(channels)]


def select_channel(channels: list[array], name: str) -> array:
    if name == "left" or len(channels) == 1:
        return channels[0]
    if name == "right":
        return channels[1]
    mixed = array("f", bytes(4 * len(channels[0])))
    for index in range(len(mixed)):
        mixed[index] = sum(channel[index] for channel in channels) / len(channels)
    return mixed


def analyze(
    frames: Iterator[tuple[float, bytes]],
    width: int,
    height: int,
    audio_channels: list[array] | None,
    audio_start_s: float,
    event_channel: str,
    counter_channel: str | None,
    thresholds: ref.Thresholds,
    pre_decoded: bool = False,
) -> dict:
    decoded = (
        list(frames) if pre_decoded
        else [(t, ref.decode_frame_code(frame, width, height)) for t, frame in frames]
    )
    video, video_events = ref.analyze_video(decoded)
    report: dict = {"video": video.as_dict(), "video_events": len(video_events)}
    if audio_channels:
        events = ref.detect_audio_events(select_channel(audio_channels, event_channel))
        pairs, unmatched = ref.pair_events(video_events, events, audio_start_s)
        report["audio"] = {
            "samples": len(audio_channels[0]),
            "channels": len(audio_channels),
            "events": len(events),
            "unmatched_events": unmatched,
        }
        report["sync"] = ref.sync_summary(pairs)
        report["sync_series"] = [
            {"grain_code": p.grain, "video_s": round(p.video_s, 6), "offset_ms": round(p.offset_ms, 3)}
            for p in pairs
        ]
        if counter_channel:
            channel = audio_channels[1 if counter_channel == "right" else 0]
            report["counter"] = ref.analyze_counter(channel).as_dict()
    else:
        report["sync"] = {"pairs": 0}
    failures = ref.verdict(report, thresholds)
    report["passed"] = not failures
    report["failures"] = failures
    return report


def probe_start(path: str, stream: str) -> float:
    output = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries",
         "stream=start_time", "-of", "csv=p=0", path],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(output) if output and output != "N/A" else 0.0


def media_inputs(path: str, width: int, height: int, with_audio: bool):
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} is required for --media; use the raw inputs instead")
    times = [
        float(line)
        for line in subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "frame=best_effort_timestamp_time", "-of", "csv=p=0", path],
            check=True, capture_output=True, text=True,
        ).stdout.split()
        if line and line != "N/A"
    ]
    video = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", path, "-map", "0:v:0", "-fps_mode", "passthrough",
         "-vf", f"scale={width}:{height},format=gray", "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE,
    )

    def frames():
        yield from zip(times, read_frames(video.stdout, width, height))
        video.wait()

    audio = None
    audio_start = 0.0
    if with_audio:
        audio_start = probe_start(path, "a:0")
        decoded = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", path, "-map", "0:a:0", "-ac", "2",
             "-ar", str(ref.SAMPLE_RATE), "-f", "f32le", "-"],
            check=True, capture_output=True,
        ).stdout
        audio = array("f")
        audio.frombytes(decoded[: len(decoded) - len(decoded) % 8])
        audio = [audio[0::2], audio[1::2]]
    return frames(), audio, audio_start


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_argument_group("input")
    source.add_argument("--media", help="any capture ffmpeg can decode")
    source.add_argument("--no-audio", action="store_true", help="ignore audio in --media")
    source.add_argument("--video-raw", help="concatenated 8-bit luma frames")
    source.add_argument("--video-codes",
                        help="pre-decoded frames: one 'capture_time_s code' line per frame, '-' if undecodable")
    source.add_argument("--video-pts", help="one capture time in seconds per frame")
    source.add_argument("--fps", type=float, default=30.0, help="frame rate when --video-pts is absent")
    source.add_argument("--width", type=int, default=DEFAULT_ANALYSIS_WIDTH)
    source.add_argument("--height", type=int, default=DEFAULT_ANALYSIS_HEIGHT)
    source.add_argument("--audio-raw", help="interleaved Float32 little-endian 48 kHz")
    source.add_argument("--audio-channels", type=int, default=2)
    source.add_argument("--audio-start-s", type=float, default=0.0,
                        help="capture time of the first audio sample (raw inputs)")
    parser.add_argument("--event-channel", choices=("left", "right", "mix"), default="left")
    parser.add_argument("--counter-channel", choices=("left", "right"),
                        help="analyze a lossless-mode sample counter on this channel")
    limits = parser.add_argument_group("thresholds")
    defaults = ref.Thresholds()
    limits.add_argument("--audio-lead-ms", type=float, default=defaults.audio_lead_ms)
    limits.add_argument("--audio-lag-ms", type=float, default=defaults.audio_lag_ms)
    limits.add_argument("--max-offset-range-ms", type=float, default=defaults.max_offset_range_ms)
    limits.add_argument("--max-drift-ppm", type=float, default=defaults.max_drift_ppm)
    limits.add_argument("--max-undecodable-fraction", type=float, default=defaults.max_undecodable_fraction)
    limits.add_argument("--max-freeze-ms", type=float, default=defaults.max_freeze_ms,
                        help="longest repeated-frame freeze allowed; negative disables")
    limits.add_argument("--min-pairs", type=int, default=defaults.min_pairs)
    limits.add_argument("--max-skipped-grains", type=int, default=defaults.max_skipped_grains,
                        help="video grains allowed to be missing; -1 disables (reduced-rate paths)")
    limits.add_argument("--max-repeats", type=int, default=-1,
                        help="repeated video grains allowed; -1 (default) disables")
    limits.add_argument("--max-unmatched-events", type=int, default=defaults.max_unmatched_events)
    parser.add_argument("--series", action="store_true", help="include per-event offsets")
    args = parser.parse_args(argv)

    thresholds = ref.Thresholds(
        audio_lead_ms=args.audio_lead_ms,
        audio_lag_ms=args.audio_lag_ms,
        max_offset_range_ms=args.max_offset_range_ms,
        max_drift_ppm=args.max_drift_ppm,
        max_undecodable_fraction=args.max_undecodable_fraction,
        max_freeze_ms=None if args.max_freeze_ms < 0 else args.max_freeze_ms,
        min_pairs=args.min_pairs,
        max_skipped_grains=None if args.max_skipped_grains < 0 else args.max_skipped_grains,
        max_repeats=None if args.max_repeats < 0 else args.max_repeats,
        max_unmatched_events=args.max_unmatched_events,
    )
    resources = contextlib.ExitStack()
    if args.media:
        frames, audio, audio_start = media_inputs(args.media, args.width, args.height, not args.no_audio)
    elif args.video_codes:
        frames = read_codes(args.video_codes)
        audio = read_audio(args.audio_raw, args.audio_channels) if args.audio_raw else None
        audio_start = args.audio_start_s
    elif args.video_raw:
        if args.video_pts:
            with open(args.video_pts) as handle:
                times = [float(line) for line in handle if line.strip()]
        else:
            times = None
        stream = resources.enter_context(open(args.video_raw, "rb"))
        raw = read_frames(stream, args.width, args.height)
        frames = (
            zip(times, raw) if times is not None
            else ((index / args.fps, frame) for index, frame in enumerate(raw))
        )
        audio = read_audio(args.audio_raw, args.audio_channels) if args.audio_raw else None
        audio_start = args.audio_start_s
    else:
        parser.error("provide --media, --video-codes, or --video-raw")
        return 2

    with resources:
        report = analyze(
            frames, args.width, args.height, audio, audio_start,
            args.event_channel, args.counter_channel, thresholds,
            pre_decoded=bool(args.video_codes),
        )
    if not args.series:
        report.pop("sync_series", None)
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
