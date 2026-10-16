#!/usr/bin/env python3
"""Reduced-cost browser confidence encoder for 1080p v210 MXL flows.

Reads the newest grain of each flow in place through the MXL SDK, keeps only
the pixels the monitor needs (exact 1/2, 1/3 or 1/6 decimation of v210), and
publishes constrained-baseline H.264 over RTMP to a loopback MediaMTX, which
serves it to browsers over WebRTC. No full-frame copy, colour-conversion
pipeline, or scaler touches the 1080p signal, and the production MXL flows
are only read.

Layouts:

- ``single``: one flow at 1920/f x 1080/f (Program, Preview).
- ``wall``: up to eight flows as the operator multiview, 1280x720 with a 4x2
  grid of 320x180 tiles and 40-pixel label bars (the geometry the skin's
  click overlay expects). Missing or stalled sources show a slate.

Every read is validated after decimation; a grain the writer may have lapped
is discarded and the previous picture is kept.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import signal
import time

import numpy as np

import av_reference as ref
import mxl_direct as mxl

SOURCE_WIDTH = 1920
SOURCE_HEIGHT = 1080
WORDS_PER_ROW = ref.v210_stride(SOURCE_WIDTH) // 4
GRAIN_SIZE = WORDS_PER_ROW * 4 * SOURCE_HEIGHT

# (word, shift) of each 10-bit sample within a six-pixel v210 group.
Y_POS = ((0, 10), (1, 0), (1, 20), (2, 10), (3, 0), (3, 20))
CB_POS = ((0, 0), (1, 10), (2, 20))   # pixels 0, 2, 4
CR_POS = ((0, 20), (2, 0), (3, 10))   # pixels 0, 2, 4

WALL_WIDTH, WALL_HEIGHT = 1280, 720
TILE_WIDTH, TILE_VIDEO_HEIGHT, TILE_BAR_HEIGHT = 320, 180, 40
WALL_TOP = (WALL_HEIGHT - 2 * (TILE_VIDEO_HEIGHT + TILE_BAR_HEIGHT)) // 2
STALE_SECONDS = 2.0
REOPEN_SECONDS = 5.0
RUNNING = True


def stop(_signum, _frame) -> None:
    global RUNNING
    RUNNING = False


def _unpack(groups: np.ndarray, positions, pixels, out: np.ndarray) -> np.ndarray:
    """Write the 8-bit samples at ``pixels`` (each mapped through ``positions``) into ``out``."""

    view = out.reshape(groups.shape[0], groups.shape[1], len(pixels))
    for column, (word, shift) in enumerate(positions[p] for p in pixels):
        sample = groups[:, :, word] >> (shift + 2)
        np.bitwise_and(sample, 0xFF, out=view[:, :, column], casting="unsafe")
    return out


def decimate(words: np.ndarray, factor: int):
    """Return 8-bit I420 planes (Y, U, V) of a (1080, 1280)-word v210 frame.

    ``factor`` must be 2, 3 or 6, giving 960x540, 640x360 or 320x180. Only the
    rows kept for each plane are touched: luma reads every ``factor``-th row,
    chroma only every second one of those.
    """

    if factor not in (2, 3, 6):
        raise ValueError("factor must be 2, 3 or 6")
    pixels = [p for p in range(6) if p % factor == 0]
    luma_rows = words[::factor].reshape(-1, WORDS_PER_ROW // 4, 4)
    chroma_rows = luma_rows[::2]
    height, width = luma_rows.shape[0], luma_rows.shape[1] * len(pixels)
    y = _unpack(luma_rows, Y_POS, pixels, np.empty((height, width), np.uint8))
    chroma_pixels = [p // 2 for p in pixels]
    cb = _unpack(chroma_rows, CB_POS, chroma_pixels, np.empty((chroma_rows.shape[0], width), np.uint8))
    cr = _unpack(chroma_rows, CR_POS, chroma_pixels, np.empty((chroma_rows.shape[0], width), np.uint8))
    return y, np.ascontiguousarray(cb[:, ::2]), np.ascontiguousarray(cr[:, ::2])


def words_at(address: int) -> np.ndarray:
    """Zero-copy (1080, 1280) uint32 view of a grain payload in the ring."""

    buffer = (ctypes.c_uint32 * (WORDS_PER_ROW * SOURCE_HEIGHT)).from_address(address)
    return np.ctypeslib.as_array(buffer).reshape(SOURCE_HEIGHT, WORDS_PER_ROW)


class Source:
    """One flow, read at its newest committed grain."""

    def __init__(self, api, instance, domain: str, label: str, flow_id: str, factor: int) -> None:
        self.api, self.instance, self.domain = api, instance, domain
        self.label, self.flow_id, self.factor = label, flow_id, factor
        self.reader = None
        self.planes = None
        self.updated = 0.0
        self.next_open = 0.0
        self.grain_count = 0
        self.grain_rate = 0.0
        self.last_head = None
        self.last_head_at = 0.0
        self.stats = {
            "reads": 0, "torn": 0, "torn_retries": 0,
            "torn_recovered": 0, "missing": 0, "reopens": 0,
            "torn_writer_jump": 0, "torn_reader_stall": 0,
            "head_jumps": 0, "max_torn_head_advance": 0,
            "max_torn_sample_ms": 0.0,
        }

    def _open(self, now: float) -> bool:
        if self.reader is not None:
            return True
        if now < self.next_open:
            return False
        self.next_open = now + REOPEN_SECONDS
        if not mxl.flow_exists(self.domain, self.flow_id):
            self.stats["missing"] += 1
            return False
        try:
            probe = mxl.Reader(self.api, self.instance, self.flow_id)
            config = probe.config()
            probe.close()
            self.reader = mxl.GrainReader(self.api, self.instance, self.flow_id, config.discrete.grainCount)
            self.grain_count = int(config.discrete.grainCount)
            rate = config.common.grainRate
            self.grain_rate = rate.numerator / rate.denominator if rate.denominator else 0.0
            self.last_head = None
            self.stats["reopens"] += 1
            return True
        except RuntimeError:
            self.stats["missing"] += 1
            return False

    def _drop(self) -> None:
        if self.reader is not None:
            self.reader.close()
        self.reader = None

    def poll(self, now: float) -> None:
        if not self._open(now):
            return

        def sample(address: int, size: int):
            if size != GRAIN_SIZE:
                raise ValueError("only 1920x1080 v210 grains are supported")
            return decimate(words_at(address), self.factor)

        # A producer burst/head skip or a descheduled reader can lap the
        # zero-copy view during validation. Re-sample one freshly committed
        # head before treating that race as a terminal torn read. The retry is
        # bounded so sustained pressure cannot trap the confidence loop.
        next_head = None
        for attempt in range(2):
            head = next_head if next_head is not None else self.reader.head()
            next_head = None
            if not head:
                return
            if attempt == 0:
                margin = max(1, self.grain_count - 2)
                if self.last_head is not None and self.grain_rate > 0:
                    expected = (now - self.last_head_at) * self.grain_rate
                    if head - self.last_head > expected + margin:
                        self.stats["head_jumps"] += 1
                self.last_head, self.last_head_at = head, now
            sample_started = time.monotonic()
            try:
                result = self.reader.read(head, 0, sample)
            except ValueError:
                self._drop()
                return
            if result.status == mxl.MXL_ERR_FLOW_INVALID:
                self._drop()        # the writer recreated the flow
                self.next_open = 0.0
                return
            if result.status != mxl.MXL_STATUS_OK:
                return
            self.stats["reads"] += 1
            if result.torn or result.value is None:
                after = self.reader.head()
                self._attribute_torn(head, after, time.monotonic() - sample_started)
                if attempt == 0:
                    self.stats["torn_retries"] += 1
                    next_head = after
                    continue
                self.stats["torn"] += 1
                return
            if attempt:
                self.stats["torn_recovered"] += 1
            self.planes = result.value
            self.updated = now
            return

    def _attribute_torn(self, head: int, after, elapsed: float) -> None:
        advance = 0 if after is None else max(0, after - head)
        expected = elapsed * self.grain_rate
        jumped = advance - expected >= max(1, self.grain_count - 2)
        self.stats["torn_writer_jump" if jumped else "torn_reader_stall"] += 1
        self.stats["max_torn_head_advance"] = max(
            self.stats["max_torn_head_advance"], advance)
        self.stats["max_torn_sample_ms"] = max(
            self.stats["max_torn_sample_ms"], round(1000 * elapsed, 3))

    def live(self, now: float) -> bool:
        return self.planes is not None and now - self.updated < STALE_SECONDS


def blank(width: int, height: int):
    return (np.full((height, width), 16, np.uint8),
            np.full((height // 2, width // 2), 128, np.uint8),
            np.full((height // 2, width // 2), 128, np.uint8))


def slate(width: int, height: int, text: str):
    import cv2

    y, u, v = blank(width, height)
    y[:] = 40
    scale = width / 640
    size, _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_DUPLEX, scale, max(1, round(2 * scale)))
    cv2.putText(y, text, ((width - size[0]) // 2, (height + size[1]) // 2),
                cv2.FONT_HERSHEY_DUPLEX, scale, 200, max(1, round(2 * scale)), cv2.LINE_AA)
    return y, u, v


def label_bar(text: str):
    import cv2

    bar = np.full((TILE_BAR_HEIGHT, TILE_WIDTH), 28, np.uint8)
    cv2.putText(bar, text[:28], (10, 27), cv2.FONT_HERSHEY_DUPLEX, 0.6, 225, 1, cv2.LINE_AA)
    return bar


def tile_origin(slot: int) -> tuple[int, int]:
    row, column = divmod(slot, 4)
    return column * TILE_WIDTH, WALL_TOP + row * (TILE_VIDEO_HEIGHT + TILE_BAR_HEIGHT)


class Wall:
    def __init__(self, sources: list[Source]) -> None:
        if not 1 <= len(sources) <= 8:
            raise ValueError("a wall takes one to eight sources")
        self.sources = sources
        self.canvas = blank(WALL_WIDTH, WALL_HEIGHT)
        self.no_signal = slate(TILE_WIDTH, TILE_VIDEO_HEIGHT, "NO SIGNAL")
        for slot, source in enumerate(sources):
            x, y = tile_origin(slot)
            self.canvas[0][y + TILE_VIDEO_HEIGHT:y + TILE_VIDEO_HEIGHT + TILE_BAR_HEIGHT, x:x + TILE_WIDTH] = label_bar(source.label)

    def compose(self, now: float):
        y_plane, u_plane, v_plane = self.canvas
        for slot, source in enumerate(self.sources):
            x, y = tile_origin(slot)
            ty, tu, tv = source.planes if source.live(now) else self.no_signal
            y_plane[y:y + TILE_VIDEO_HEIGHT, x:x + TILE_WIDTH] = ty
            u_plane[y // 2:(y + TILE_VIDEO_HEIGHT) // 2, x // 2:(x + TILE_WIDTH) // 2] = tu
            v_plane[y // 2:(y + TILE_VIDEO_HEIGHT) // 2, x // 2:(x + TILE_WIDTH) // 2] = tv
        return self.canvas


def pipeline_description(width: int, height: int, fps: int, bitrate_kbps: int, rtmp_url: str) -> str:
    return (
        "appsrc name=src is-live=true format=time do-timestamp=true block=false "
        f"caps=video/x-raw,format=I420,width={width},height={height},framerate={fps}/1 "
        "! queue max-size-buffers=2 leaky=downstream "
        f"! x264enc tune=zerolatency speed-preset=ultrafast bitrate={bitrate_kbps} "
        f"key-int-max={2 * fps} threads=2 "
        "! video/x-h264,profile=constrained-baseline "
        "! h264parse config-interval=-1 "
        "! flvmux streamable=true "
        # librtmp's sink accepts a single-component MediaMTX path (rtmp://host:port/path).
        f'! rtmpsink location="{rtmp_url} live=1" sync=false async=false'

    )


class Encoder:
    """appsrc -> x264 -> RTMP, rebuilt if the publish connection fails."""

    def __init__(self, Gst, width, height, fps, bitrate_kbps, rtmp_url) -> None:
        self.Gst = Gst
        self.args = (width, height, fps, bitrate_kbps, rtmp_url)
        self.pipeline = None
        self.restarts = -1
        self.retry_at = 0.0

    def _start(self) -> None:
        Gst = self.Gst
        self.pipeline = Gst.parse_launch(pipeline_description(*self.args))
        self.source = self.pipeline.get_by_name("src")
        self.bus = self.pipeline.get_bus()
        self.pipeline.set_state(Gst.State.PLAYING)
        self.restarts += 1

    def push(self, planes, now: float) -> None:
        Gst = self.Gst
        if self.pipeline is None:
            if now < self.retry_at:
                return
            self._start()
        message = self.bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
        if message is not None:
            reason = message.parse_error()[0].message if message.type == Gst.MessageType.ERROR else "end of stream"
            print(json.dumps({"event": "encoder_error", "reason": reason, "retry_s": 2.0}), flush=True)
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
            self.retry_at = now + 2.0
            return
        payload = b"".join(plane.tobytes() for plane in planes)
        self.source.emit("push-buffer", Gst.Buffer.new_wrapped(payload))

    def stop(self) -> None:
        if self.pipeline is not None:
            self.pipeline.set_state(self.Gst.State.NULL)


def parse_input(text: str) -> tuple[str, str]:
    label, _, flow = text.rpartition("=")
    if not label or not flow:
        raise argparse.ArgumentTypeError("inputs are LABEL=FLOW_ID")
    return label, flow


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--domain", default="/mxl-domain")
    parser.add_argument("--layout", choices=("single", "wall"), required=True)
    parser.add_argument("--input", type=parse_input, action="append", required=True, help="LABEL=FLOW_ID")
    parser.add_argument("--factor", type=int, default=2, help="single layout decimation: 2, 3 or 6")
    parser.add_argument("--fps", type=int, required=True)
    parser.add_argument("--bitrate-kbps", type=int, default=2500)
    parser.add_argument("--rtmp", required=True, help="e.g. rtmp://127.0.0.1:1935/mxl-program")
    parser.add_argument("--library", default="libmxl.so")
    parser.add_argument("--stats-every-s", type=float, default=60.0)
    args = parser.parse_args()

    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    api = mxl.load_library(args.library)
    instance = api.mxlCreateInstance(args.domain.encode(), b"")
    if not instance:
        raise RuntimeError("mxlCreateInstance failed")

    factor = 6 if args.layout == "wall" else args.factor
    sources = [Source(api, instance, args.domain, label, flow, factor) for label, flow in args.input]
    if args.layout == "wall":
        wall = Wall(sources)
        width, height = WALL_WIDTH, WALL_HEIGHT
        compose = wall.compose
    else:
        if len(sources) != 1:
            raise SystemExit("the single layout takes exactly one --input")
        width, height = SOURCE_WIDTH // factor, SOURCE_HEIGHT // factor
        no_signal = slate(width, height, "NO SIGNAL")
        compose = lambda now: sources[0].planes if sources[0].live(now) else no_signal  # noqa: E731

    encoder = Encoder(Gst, width, height, args.fps, args.bitrate_kbps, args.rtmp)
    period = 1.0 / args.fps
    deadline = time.monotonic()
    next_stats = deadline + args.stats_every_s
    frames = late = 0
    work_total = 0.0
    print(json.dumps({"event": "confidence_ready", "layout": args.layout, "inputs": len(sources),
                      "output": f"{width}x{height}@{args.fps}", "rtmp": args.rtmp}), flush=True)
    try:
        while RUNNING:
            started = time.monotonic()
            for source in sources:
                source.poll(started)
            encoder.push(compose(started), started)
            frames += 1
            work_total += time.monotonic() - started
            deadline += period
            delay = deadline - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                late += 1
                if delay < -1.0:
                    deadline = time.monotonic()
            if started >= next_stats:
                print(json.dumps({
                    "event": "stats", "frames": frames, "late": late,
                    "avg_work_ms": round(1000 * work_total / max(1, frames), 2),
                    "encoder_restarts": max(0, encoder.restarts),
                    "sources": {s.label: {**s.stats, "live": s.live(started)} for s in sources},
                }), flush=True)
                next_stats = started + args.stats_every_s
    finally:
        encoder.stop()
        for source in sources:
            source._drop()
        api.mxlDestroyInstance(instance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
