#!/usr/bin/env python3
"""Publish cuttable Playout and Keyed diagnostic video flows into an MXL domain."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import signal
import time
import urllib.request

import cv2
import gi
import numpy as np

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402


CANVAS_WIDTH = 960
CANVAS_HEIGHT = 540
OUTPUT_WIDTH = 1920
OUTPUT_HEIGHT = 1080
DRAW_SCALE = CANVAS_WIDTH / OUTPUT_WIDTH
RATE = 30
FRAME_NS = 1_000_000_000 // RATE
RUNNING = True


def stop(_signum: int, _frame: object) -> None:
    global RUNNING
    RUNNING = False


def timecode(frame_number: int) -> str:
    frames = frame_number % RATE
    total_seconds = frame_number // RATE
    seconds = total_seconds % 60
    minutes = (total_seconds // 60) % 60
    hours = (total_seconds // 3600) % 24
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"


def put_text(
    frame: np.ndarray,
    text: str,
    origin: tuple[int, int],
    scale: float,
    color: tuple[int, int, int],
    thickness: int = 3,
) -> None:
    cv2.putText(
        frame,
        text,
        point(*origin),
        cv2.FONT_HERSHEY_DUPLEX,
        scale * DRAW_SCALE,
        (0, 0, 0),
        max(1, round((thickness + 6) * DRAW_SCALE)),
        cv2.LINE_AA,
    )
    cv2.putText(
        frame,
        text,
        point(*origin),
        cv2.FONT_HERSHEY_DUPLEX,
        scale * DRAW_SCALE,
        color,
        max(1, round(thickness * DRAW_SCALE)),
        cv2.LINE_AA,
    )


def point(x: int, y: int) -> tuple[int, int]:
    return round(x * DRAW_SCALE), round(y * DRAW_SCALE)


def rectangle(frame: np.ndarray, first: tuple[int, int], second: tuple[int, int], color: tuple[int, int, int], thickness: int) -> None:
    cv2.rectangle(frame, point(*first), point(*second), color, -1 if thickness < 0 else max(1, round(thickness * DRAW_SCALE)))


def line(frame: np.ndarray, first: tuple[int, int], second: tuple[int, int], color: tuple[int, int, int], thickness: int) -> None:
    cv2.line(frame, point(*first), point(*second), color, max(1, round(thickness * DRAW_SCALE)))


def circle(frame: np.ndarray, center: tuple[int, int], radius: int, color: tuple[int, int, int], thickness: int) -> None:
    cv2.circle(frame, point(*center), round(radius * DRAW_SCALE), color, -1 if thickness < 0 else max(1, round(thickness * DRAW_SCALE)))


def base_frame() -> np.ndarray:
    x = np.linspace(0, 1, CANVAS_WIDTH, dtype=np.float32)
    y = np.linspace(0, 1, CANVAS_HEIGHT, dtype=np.float32)[:, None]
    frame = np.empty((CANVAS_HEIGHT, CANVAS_WIDTH, 3), dtype=np.uint8)
    frame[:, :, 0] = np.clip(30 + 80 * x + 25 * y, 0, 255)
    frame[:, :, 1] = np.clip(18 + 35 * x + 70 * y, 0, 255)
    frame[:, :, 2] = np.clip(65 + 110 * x - 25 * y, 0, 255)
    return frame


def draw_playout(template: np.ndarray, frame_number: int) -> np.ndarray:
    frame = template.copy()
    phase = (frame_number * 6) % (OUTPUT_WIDTH + 420) - 210
    rectangle(frame, (phase - 170, 0), (phase + 170, OUTPUT_HEIGHT), (235, 170, 30), -1)
    rectangle(frame, (phase - 75, 0), (phase + 75, OUTPUT_HEIGHT), (40, 45, 220), -1)
    for row in range(7):
        left = (frame_number * (4 + row) + row * 235) % (OUTPUT_WIDTH + 240) - 120
        top = 110 + row * 115
        circle(frame, (left, top), 38 + row * 3, (30 + row * 24, 230 - row * 18, 245), -1)
    rectangle(frame, (90, 72), (1830, 305), (12, 14, 24), -1)
    put_text(frame, "MXL PLAYOUT", (145, 175), 2.1, (255, 255, 255), 5)
    put_text(frame, "MOVING DIAGNOSTIC SOURCE", (150, 255), 0.9, (80, 220, 255), 2)
    rectangle(frame, (320, 745), (1600, 980), (8, 10, 17), -1)
    put_text(frame, timecode(frame_number), (385, 920), 3.4, (90, 255, 125), 6)
    return frame


def draw_keyed(template: np.ndarray, frame_number: int) -> np.ndarray:
    frame = template.copy()
    grid_shift = (frame_number * 5) % 160
    for x in range(-160 + grid_shift, OUTPUT_WIDTH, 160):
        line(frame, (x, 0), (x, OUTPUT_HEIGHT), (105, 80, 135), 2)
    for y in range(0, OUTPUT_HEIGHT, 120):
        line(frame, (0, y), (OUTPUT_WIDTH, y), (105, 80, 135), 2)
    bug = frame.copy()
    rectangle(bug, (1450, 55), (1835, 175), (20, 20, 24), -1)
    rectangle(bug, (1450, 55), (1470, 175), (30, 90, 255), -1)
    frame = cv2.addWeighted(bug, 0.92, frame, 0.08, 0)
    put_text(frame, "MXL  KEYED", (1510, 135), 0.85, (255, 255, 255), 2)

    overlay = frame.copy()
    rectangle(overlay, (90, 720), (1510, 970), (15, 17, 24), -1)
    rectangle(overlay, (90, 720), (118, 970), (255, 75, 38), -1)
    frame = cv2.addWeighted(overlay, 0.90, frame, 0.10, 0)
    put_text(frame, "LIVE GRAPHICS KEY", (155, 820), 1.45, (255, 255, 255), 4)
    put_text(frame, "MXL-native compositing proof", (160, 895), 0.8, (115, 205, 255), 2)
    put_text(frame, timecode(frame_number), (925, 915), 1.25, (100, 255, 145), 3)
    return frame


class Publisher:
    def __init__(self, domain: str, flow_id: str, label: str) -> None:
        description = f"{label} running-timecode diagnostic source"
        pipeline = Gst.parse_launch(
            "appsrc name=source is-live=true block=true format=time "
            f"caps=video/x-raw,format=BGR,width={CANVAS_WIDTH},height={CANVAS_HEIGHT},framerate={RATE}/1 "
            "! queue max-size-buffers=2 leaky=downstream "
            "! videoscale n-threads=4 ! videoconvert n-threads=4 "
            f"! video/x-raw,format=v210,width={OUTPUT_WIDTH},height={OUTPUT_HEIGHT},framerate={RATE}/1 "
            f"! mxlsink domain={domain} flow-id={flow_id} label=\"{label}\" "
            f"description=\"{description}\" group-hint=\"Demo:{label}\" "
            "sync=true async=false max-lateness=-1"
        )
        self.pipeline = pipeline
        self.source = pipeline.get_by_name("source")

    def start(self) -> None:
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("MXL demo-source pipeline failed to start")

    def push(self, frame: np.ndarray, frame_number: int) -> None:
        payload = frame.tobytes()
        buffer = Gst.Buffer.new_allocate(None, len(payload), None)
        buffer.fill(0, payload)
        buffer.pts = frame_number * FRAME_NS
        buffer.dts = buffer.pts
        buffer.duration = FRAME_NS
        result = self.source.emit("push-buffer", buffer)
        if result != Gst.FlowReturn.OK:
            raise RuntimeError(f"MXL demo-source push failed: {result.value_nick}")

    def stop(self) -> None:
        self.source.emit("end-of-stream")
        self.pipeline.set_state(Gst.State.NULL)


def register_source(
    core_url: str,
    source_id: str,
    flow_id: str,
    label: str,
    kind: str,
    capabilities: list[str],
) -> None:
    payload = json.dumps(
        {
            "id": source_id,
            "flow_id": flow_id,
            "label": label,
            "kind": kind,
            "capabilities": capabilities,
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", default="/mxl-domain")
    parser.add_argument("--playout-flow", required=True)
    parser.add_argument("--keyed-flow", required=True)
    parser.add_argument("--core-url")
    parser.add_argument("--playout-id", default="playout-tc")
    parser.add_argument("--keyed-id", default="keyed-tc")
    args = parser.parse_args()

    Gst.init(None)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    template = base_frame()
    playout = Publisher(args.domain, args.playout_flow, "MXL Playout TC")
    keyed = Publisher(args.domain, args.keyed_flow, "MXL Keyed TC")
    playout.start()
    keyed.start()
    if args.core_url:
        register_source(
            args.core_url,
            args.playout_id,
            args.playout_flow,
            "MXL Playout TC",
            "playout",
            ["video", "playback", "timecode", "health"],
        )
        register_source(
            args.core_url,
            args.keyed_id,
            args.keyed_flow,
            "MXL Keyed TC",
            "graphics",
            ["video", "graphics", "timecode", "health"],
        )
    frame_number = 0
    deadline = time.monotonic()
    print("demo_sources=ready rate=30/1", flush=True)
    try:
        while RUNNING:
            playout.push(draw_playout(template, frame_number), frame_number)
            keyed.push(draw_keyed(template, frame_number), frame_number)
            frame_number += 1
            deadline += 1 / RATE
            delay = deadline - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.5:
                deadline = time.monotonic()
    finally:
        playout.stop()
        keyed.stop()
    print(f"frames={frame_number}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
