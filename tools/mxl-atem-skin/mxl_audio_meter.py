#!/usr/bin/env python3
"""Read live Float32 MXL audio rings and expose stereo meter levels as JSON."""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


HEAD_INDEX_OFFSET = 0xC8


def dbfs(value: float) -> float:
    return round(20.0 * math.log10(max(value, 1e-12)), 1)


class AudioRing:
    def __init__(self, domain: pathlib.Path, name: str, flow_id: str, window: int):
        self.name = name
        self.flow_id = flow_id
        self.directory = domain / f"{flow_id}.mxl-flow"
        definition = json.loads((self.directory / "flow_def.json").read_text())
        if definition.get("media_type") != "audio/float32":
            raise ValueError(f"{name} is not an audio/float32 flow")
        self.channels = int(definition["channel_count"])
        self.word_size = int(definition["bit_depth"]) // 8
        channel_bytes = (self.directory / "channels").stat().st_size
        self.ring_length = channel_bytes // self.channels // self.word_size
        self.window = min(window, self.ring_length // 2)
        self.last_head = -1
        self.last_progress = 0.0

    def _channel_window(self, payload: bytes, channel: int, head: int) -> list[float]:
        start = head - self.window + 1
        base = channel * self.ring_length * self.word_size
        values: list[float] = []
        for absolute in range(start, head + 1):
            offset = base + (absolute % self.ring_length) * self.word_size
            values.append(struct.unpack_from("<f", payload, offset)[0])
        return values

    def sample(self) -> dict[str, object]:
        now = time.monotonic()
        metadata = (self.directory / "data").read_bytes()
        head = struct.unpack_from("<Q", metadata, HEAD_INDEX_OFFSET)[0]
        if head != self.last_head:
            self.last_head = head
            self.last_progress = now
        payload = (self.directory / "channels").read_bytes()
        result = []
        for channel in range(self.channels):
            values = self._channel_window(payload, channel, head)
            peak = max((abs(value) for value in values), default=0.0)
            rms = math.sqrt(sum(value * value for value in values) / len(values)) if values else 0.0
            result.append({"rms_dbfs": dbfs(rms), "peak_dbfs": dbfs(peak)})
        peak = max((item["peak_dbfs"] for item in result), default=-240.0)
        if now - self.last_progress > 1.0:
            state = "stale"
        elif peak <= -90.0:
            state = "silent"
        else:
            state = "ok"
        return {
            "flow": self.name,
            "head_index": head,
            "channels": result,
            "rms_dbfs": [item["rms_dbfs"] for item in result],
            "peak_dbfs": [item["peak_dbfs"] for item in result],
            "state": state,
        }


class MeterBank:
    def __init__(self, rings: list[AudioRing]):
        self.rings = rings
        self.lock = threading.Lock()

    def state(self) -> dict[str, object]:
        with self.lock:
            meters = {}
            for ring in self.rings:
                try:
                    meters[ring.name] = ring.sample()
                except (OSError, ValueError, struct.error) as error:
                    meters[ring.name] = {"flow": ring.name, "state": "error", "error": str(error)}
            return {"meters": meters, "sampled_at": time.time()}


BANK: MeterBank


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/meters":
            self.send_error(404)
            return
        data = json.dumps(BANK.state(), separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> None:
    global BANK
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", default="/mxl-domain")
    parser.add_argument("--flow", action="append", required=True, metavar="NAME=UUID")
    parser.add_argument("--window", type=int, default=2400, help="Samples per channel")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9730)
    args = parser.parse_args()
    domain = pathlib.Path(args.domain)
    rings = [AudioRing(domain, *item.split("=", 1), args.window) for item in args.flow]
    BANK = MeterBank(rings)
    print(json.dumps({"event": "audio_meter_ready", "flows": len(rings), "port": args.port}), flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
