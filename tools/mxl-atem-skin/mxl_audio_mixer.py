#!/usr/bin/env python3
"""Small live MXL audio mixer for the ATEM-style operator skin."""

from __future__ import annotations

import argparse
import json
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
MARGIN_NS = 66_000_000


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
        self.pipeline = self._make_pipeline()
        self._apply()

    def _make_pipeline(self):
        desc = (
            "audiomixer name=mix force-live=true ignore-inactive-pads=true "
            "latency=120000000 output-buffer-duration=10000000 ! "
            f"audioconvert ! audioresample ! {CAPS} ! "
            "volume name=master ! queue max-size-buffers=32 ! "
            f"mxlsink name=out domain={self.domain} flow-id={self.output} "
            'label="Program Audio Mix" description="ATEM skin program audio bus" '
            'group-hint="Program:Audio" sync=false '
            f"audiotestsrc wave=silence is-live=true ! audioconvert ! {CAPS} ! "
            "queue ! mix.sink_0 "
        )
        for index, flow in enumerate(self.inputs, 1):
            desc += (
                f"mxlsrc name=src{index} domain={self.domain} audio-flow-id={flow} "
                "automatic-eos=false do-timestamp=true ! audioconvert ! audioresample ! "
                f"{CAPS} ! queue max-size-buffers=32 leaky=downstream ! mix.sink_{index} "
            )
        pipeline = Gst.parse_launch(desc)
        self.mix = pipeline.get_by_name("mix")
        self.master = pipeline.get_by_name("master")
        self.output_sink = pipeline.get_by_name("out")
        self.pads = {
            str(index): self.mix.get_static_pad(f"sink_{index}")
            for index in range(1, len(self.inputs) + 1)
        }
        self._next_pts = None
        self.output_sink.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, self._restamp
        )
        for index in range(1, len(self.inputs) + 1):
            pipeline.get_by_name(f"src{index}").get_static_pad("src").add_probe(
                Gst.PadProbeType.BUFFER, self._normalizer()
            )
        return pipeline

    def _normalizer(self):
        state = {"offset": None}

        def normalize(_pad, info):
            buffer = info.get_buffer()
            clock = self.pipeline.get_clock()
            if not clock or buffer.pts == Gst.CLOCK_TIME_NONE:
                return Gst.PadProbeReturn.OK
            target = clock.get_time() - self.pipeline.get_base_time() + MARGIN_NS
            if state["offset"] is None or abs(buffer.pts + state["offset"] - target) > 500_000_000:
                state["offset"] = target - buffer.pts
            buffer.pts += state["offset"]
            return Gst.PadProbeReturn.OK

        return normalize

    def _restamp(self, _pad, info):
        buffer = info.get_buffer()
        clock = self.pipeline.get_clock()
        if not clock:
            return Gst.PadProbeReturn.OK
        target = clock.get_time() - self.pipeline.get_base_time() + MARGIN_NS
        if self._next_pts is None or abs(target - self._next_pts) > 500_000_000:
            self._next_pts = target
        buffer.pts = self._next_pts
        if buffer.duration != Gst.CLOCK_TIME_NONE:
            self._next_pts += buffer.duration
        return Gst.PadProbeReturn.OK

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
