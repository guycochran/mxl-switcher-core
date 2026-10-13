#!/usr/bin/env python3
"""Local-only ATEM-style operator skin for MXL Core and SuperSource banks."""

from __future__ import annotations

import argparse
import json
import mimetypes
import pathlib
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class App:
    def __init__(
        self,
        root: pathlib.Path,
        core: str,
        helper: str | None,
        state_file: pathlib.Path,
        program_url: str | None = None,
        multiview_url: str | None = None,
    ):
        self.root = root
        self.core = core.rstrip("/")
        self.helper = helper
        self.program_url = program_url
        self.multiview_url = multiview_url
        self.state_file = state_file
        self.lock = threading.Lock()
        self.banks = {
            "a": {"count": 2, "sources": [1, 2], "ready": False},
            "b": {"count": 2, "sources": [3, 4], "ready": False},
        }
        if state_file.is_file():
            saved = json.loads(state_file.read_text())
            if set(saved) == {"a", "b"}:
                self.banks = saved

    def persist(self):
        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.banks, indent=2) + "\n")
        temporary.replace(self.state_file)

    def core_json(self, path: str, method: str = "GET", body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.core + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status, response.headers.get_content_type(), response.read()

    def state(self):
        _status, _kind, raw = self.core_json("/api/mxl/v1/state")
        core = json.loads(raw)
        with self.lock:
            banks = json.loads(json.dumps(self.banks))
        return {
            "core": core,
            "banks": banks,
            "live": bool(self.helper),
            "program_url": self.program_url,
            "multiview_url": self.multiview_url,
        }

    def prepare(self, payload):
        if not self.helper:
            raise RuntimeError("persistent SuperSource scene engine is not enabled")
        count = int(payload.get("count", 0))
        sources = payload.get("sources")
        if count not in range(2, 9):
            raise ValueError("layout must be between 2-up and 8-up")
        if not isinstance(sources, list) or len(sources) != count:
            raise ValueError("source count must match the selected layout")
        sources = [int(item) for item in sources]
        if any(item not in range(1, 9) for item in sources):
            raise ValueError("sources must be numbered 1 through 8")
        if len(set(sources)) != len(sources):
            raise ValueError("each SuperSource box must use a different source")

        core = self.state()["core"]
        active = {core.get("program"), core.get("preview")}
        bank = "b" if "supersource-a" in active else "a"
        if self.helper:
            result = subprocess.run(
                [self.helper, bank, str(count), *[str(item) for item in sources]],
                text=True,
                capture_output=True,
                timeout=75,
                check=False,
            )
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout or "SuperSource launch failed").strip())
        with self.lock:
            self.banks[bank] = {"count": count, "sources": sources, "ready": True}
            self.persist()
        status, _kind, receipt = self.core_json(
            "/api/mxl/v1/preview", "POST", {"source": f"supersource-{bank}"}
        )
        return {"bank": bank, "scene": self.banks[bank], "receipt": json.loads(receipt), "status": status}


class Handler(BaseHTTPRequestHandler):
    app: App

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}")

    def send_bytes(self, status: int, kind: str, data: bytes):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status: int, value):
        self.send_bytes(status, "application/json", json.dumps(value).encode())

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        try:
            if self.path == "/api/skin/state":
                return self.send_json(200, self.app.state())
            if self.path.startswith("/api/mxl/v1/"):
                status, kind, data = self.app.core_json(self.path)
                return self.send_bytes(status, kind, data)
            path = "/index.html" if self.path in ("/", "/skin") else self.path.split("?", 1)[0]
            candidate = (self.app.root / path.lstrip("/")).resolve()
            if self.app.root not in candidate.parents or not candidate.is_file():
                return self.send_json(404, {"error": "not found"})
            kind = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            return self.send_bytes(200, kind, candidate.read_bytes())
        except (urllib.error.URLError, TimeoutError) as error:
            self.send_json(502, {"error": f"Core unavailable: {error}"})
        except Exception as error:
            self.send_json(500, {"error": str(error)})

    def do_POST(self):
        try:
            if self.path == "/api/skin/supersource/prepare":
                return self.send_json(202, self.app.prepare(self.read_json()))
            if self.path == "/api/skin/supersource/take":
                status, _kind, data = self.app.core_json("/api/mxl/v1/take", "POST")
                return self.send_bytes(status, "application/json", data)
            if self.path.startswith("/api/mxl/v1/"):
                body = self.read_json()
                status, kind, data = self.app.core_json(self.path, "POST", body or None)
                return self.send_bytes(status, kind, data)
            self.send_json(404, {"error": "not found"})
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error)})
        except subprocess.TimeoutExpired:
            self.send_json(504, {"error": "SuperSource did not become ready in time"})
        except urllib.error.HTTPError as error:
            self.send_bytes(error.code, "application/json", error.read())
        except RuntimeError as error:
            self.send_json(503, {"error": str(error)})
        except Exception as error:
            self.send_json(500, {"error": str(error)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9700)
    parser.add_argument("--core", default="http://127.0.0.1:18085")
    parser.add_argument("--supersource-helper")
    parser.add_argument("--program-url", help="WebRTC confidence-player URL to embed in Program")
    parser.add_argument("--multiview-url", help="WebRTC operator-multiview URL to embed in Preview")
    parser.add_argument("--state-file")
    args = parser.parse_args()
    root = pathlib.Path(__file__).with_name("static").resolve()
    state_file = pathlib.Path(args.state_file).resolve() if args.state_file else root.parent / "state.json"
    Handler.app = App(
        root,
        args.core,
        args.supersource_helper,
        state_file,
        args.program_url,
        args.multiview_url,
    )
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(json.dumps({"event": "skin_ready", "url": f"http://{args.host}:{args.port}", "live_supersource": bool(args.supersource_helper)}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
