#!/usr/bin/env python3
"""Small live MXL audio mixer for the ATEM-style operator skin."""

from __future__ import annotations

import argparse
import json
import pathlib
import struct
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402


CAPS = (
    "audio/x-raw,format=F32LE,layout=interleaved,rate=48000,"
    "channels=2,channel-mask=(bitmask)0x3"
)
HEAD_INDEX_OFFSET = 0xC8
PIPELINE_LATENCY_NS = 250_000_000
OUTPUT_BUFFER_DURATION_NS = 10_000_000
OUTPUT_STALL_SECONDS = 3.0
STARTUP_GRACE_SECONDS = 8.0


class Mixer:
    def __init__(self, domain: str, output: str, inputs: list[str], core: str):
        self.domain = domain
        self.output = output
        self.inputs = inputs
        self.core = core.rstrip("/")
        self.lock = threading.Lock()
        self.mode = "afv"
        self.program = None
        self.master_gain = 1.0
        self.channels = {
            str(index): {"gain": 1.0, "on": False, "mute": False}
            for index in range(1, len(inputs) + 1)
        }
        self.pipeline_generation = 0
        self.pipeline_restarts = 0
        self.last_restart_reason = None
        self.output_head_index = None
        self.output_progress_at = time.monotonic()
        self.pipeline_started_at = time.monotonic()
        self.pipeline = self._make_pipeline()
        self._apply()

    def _make_pipeline(self):
        desc = (
            "audiomixer name=mix force-live=true ignore-inactive-pads=true "
            f"latency={PIPELINE_LATENCY_NS} "
            f"output-buffer-duration={OUTPUT_BUFFER_DURATION_NS} ! "
            f"{CAPS} ! volume name=master ! "
            "queue max-size-time=1000000000 max-size-buffers=0 max-size-bytes=0 ! "
            f"mxlsink name=out domain={self.domain} flow-id={self.output} "
            'label="Program Audio Mix" description="ATEM skin program audio bus" '
            'group-hint="Program:Audio" sync=false '
            f"audiotestsrc wave=silence is-live=true ! audioconvert ! {CAPS} ! "
            "queue ! mix.sink_0 "
        )
        for index, flow in enumerate(self.inputs, 1):
            desc += (
                f"mxlsrc name=src{index} domain={self.domain} audio-flow-id={flow} "
                "automatic-eos=false ! "
                f"{CAPS} ! queue max-size-time=1000000000 "
                "max-size-buffers=0 max-size-bytes=0 ! "
                f"mix.sink_{index} "
            )
        pipeline = Gst.parse_launch(desc)
        self.mix = pipeline.get_by_name("mix")
        self.master = pipeline.get_by_name("master")
        self.output_sink = pipeline.get_by_name("out")
        self.pads = {
            str(index): self.mix.get_static_pad(f"sink_{index}")
            for index in range(1, len(self.inputs) + 1)
        }
        self.bus = pipeline.get_bus()
        self.pipeline_generation += 1
        self.pipeline_started_at = time.monotonic()
        return pipeline

    @property
    def output_data_path(self) -> pathlib.Path:
        return pathlib.Path(self.domain) / f"{self.output}.mxl-flow" / "data"

    def _read_output_head(self):
        payload = self.output_data_path.read_bytes()
        return struct.unpack_from("<Q", payload, HEAD_INDEX_OFFSET)[0]

    def _restart_pipeline(self, reason: str):
        old_pipeline = self.pipeline
        old_pipeline.set_state(Gst.State.NULL)
        self.pipeline = self._make_pipeline()
        self._apply()
        result = self.pipeline.set_state(Gst.State.PLAYING)
        if result == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError(f"audio mixer pipeline restart failed: {reason}")
        self.pipeline_restarts += 1
        self.last_restart_reason = reason
        self.output_head_index = None
        self.output_progress_at = time.monotonic()

    def monitor_pipeline(self):
        while True:
            time.sleep(0.5)
            with self.lock:
                message = self.bus.pop_filtered(
                    Gst.MessageType.ERROR | Gst.MessageType.EOS
                )
                if message is not None:
                    if message.type == Gst.MessageType.ERROR:
                        error, debug = message.parse_error()
                        reason = f"gstreamer error: {error}; {debug or ''}".strip()
                    else:
                        reason = "unexpected end of stream"
                    self._restart_pipeline(reason)
                    continue

                now = time.monotonic()
                try:
                    head = self._read_output_head()
                except (OSError, struct.error):
                    head = None
                if head is not None and head != self.output_head_index:
                    self.output_head_index = head
                    self.output_progress_at = now
                if (
                    now - self.pipeline_started_at > STARTUP_GRACE_SECONDS
                    and now - self.output_progress_at > OUTPUT_STALL_SECONDS
                ):
                    self._restart_pipeline(
                        f"output ring stalled for {now - self.output_progress_at:.1f}s"
                    )

    def _enabled(self, number: str) -> bool:
        channel = self.channels[number]
        if channel["mute"]:
            return False
        if self.mode == "afv" and self.program and self.program.startswith("zoomiso-"):
            return number == self.program.rsplit("-", 1)[-1]
        return bool(channel["on"])

    def _apply(self):
        self.master.set_property("volume", self.master_gain)
        for number, pad in self.pads.items():
            channel = self.channels[number]
            pad.set_property("volume", channel["gain"] if self._enabled(number) else 0.0)

    def poll_program(self):
        while True:
            try:
                with urllib.request.urlopen(
                    self.core + "/api/mxl/v1/state", timeout=2
                ) as response:
                    program = json.load(response).get("program")
                with self.lock:
                    if program != self.program:
                        self.program = program
                        self._apply()
            except (OSError, TimeoutError, json.JSONDecodeError):
                pass
            time.sleep(0.25)

    def state_unlocked(self):
        return {
            "state": "ok",
            "mode": self.mode,
            "program": self.program,
            "master_gain": self.master_gain,
            "output_flow": self.output,
            "pipeline_generation": self.pipeline_generation,
            "pipeline_restarts": self.pipeline_restarts,
            "last_restart_reason": self.last_restart_reason,
            "output_head_index": self.output_head_index,
            "output_progress_age_seconds": round(
                time.monotonic() - self.output_progress_at, 3
            ),
            "channels": {
                number: {**channel, "active": self._enabled(number)}
                for number, channel in self.channels.items()
            },
        }

    def state(self):
        with self.lock:
            return self.state_unlocked()

    def control(self, command):
        with self.lock:
            if "mode" in command:
                if command["mode"] not in ("afv", "manual"):
                    raise ValueError("mode must be afv or manual")
                self.mode = command["mode"]
            if "master_gain" in command:
                self.master_gain = max(0.0, min(1.5, float(command["master_gain"])))
            if "channel" in command:
                number = str(int(command["channel"]))
                if number not in self.channels:
                    raise ValueError("channel must be 1 through 8")
                channel = self.channels[number]
                if "gain" in command:
                    channel["gain"] = max(0.0, min(1.5, float(command["gain"])))
                if "on" in command:
                    channel["on"] = bool(command["on"])
                if "mute" in command:
                    channel["mute"] = bool(command["mute"])
            self._apply()
            return self.state_unlocked()

    def start(self):
        result = self.pipeline.set_state(Gst.State.PLAYING)
        if result == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("audio mixer pipeline failed to start")
        threading.Thread(target=self.poll_program, daemon=True).start()
        threading.Thread(target=self.monitor_pipeline, daemon=True).start()


MIXER: Mixer


class Handler(BaseHTTPRequestHandler):
    def _json(self, status, value):
        payload = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        self._json(200, MIXER.state()) if self.path == "/state" else self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/control":
            return self._json(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            command = json.loads(self.rfile.read(length) or b"{}")
            self._json(200, MIXER.control(command))
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self._json(400, {"error": str(error)})

    def log_message(self, _format, *_args):
        return


def main():
    global MIXER
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", default="/mxl-domain")
    parser.add_argument("--output", required=True)
    parser.add_argument("--input", action="append", required=True)
    parser.add_argument("--core", default="http://host.docker.internal:18085")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9740)
    args = parser.parse_args()
    Gst.init(None)
    MIXER = Mixer(args.domain, args.output, args.input, args.core)
    MIXER.start()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
