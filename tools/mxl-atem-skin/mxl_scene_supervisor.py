#!/usr/bin/env python3
"""Keep one SuperSource bank warm and switch its scene over loopback HTTP."""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class SceneEngine:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.lock = threading.RLock()
        self.configure_lock = threading.Lock()
        self.child: subprocess.Popen[str] | None = None
        self.generation = 0
        self.ready = False
        self.scene = {"count": 0, "sources": []}
        self.last_ready_ms = 0
        self.last_error = ""

    def _validate(self, count: int, sources: list[int]) -> None:
        if count < 2 or count > 8 or len(sources) != count:
            raise ValueError("layout must contain 2 through 8 sources")
        if len(set(sources)) != count or any(source < 1 or source > 8 for source in sources):
            raise ValueError("sources must be unique numbers from 1 through 8")

    def _command(self, count: int, sources: list[int]) -> list[str]:
        command = [
            sys.executable,
            self.args.compositor,
            "--domain",
            self.args.domain,
        ]
        for source in sources:
            command.extend(("--input", self.args.input[source - 1]))
        command.extend(
            (
                "--output-flow-id",
                self.args.output_flow_id,
                "--output-label",
                f"MXL SUPERSOURCE {self.args.bank.upper()} {count}-UP",
                "--assets-dir",
                self.args.assets,
                "--rate",
                self.args.rate,
                "--health-interval",
                "0.25",
                "--reader-stale-seconds",
                "8",
            )
        )
        return command

    def _copy_output(self, child: subprocess.Popen[str], generation: int) -> None:
        assert child.stdout is not None
        for line in child.stdout:
            print(line, end="", flush=True)
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            with self.lock:
                if generation != self.generation:
                    continue
                if event.get("event") == "health" and int(event.get("output_ticks", 0)) > 0:
                    self.ready = True
                elif event.get("event") in {"fatal", "output_eos"}:
                    self.last_error = line.strip()

    def configure(self, count: int, sources: list[int]) -> dict[str, object]:
        self._validate(count, sources)
        with self.configure_lock:
            return self._configure(count, sources)

    def _configure(self, count: int, sources: list[int]) -> dict[str, object]:
        started = time.monotonic()
        with self.lock:
            if self.ready and self.scene == {"count": count, "sources": sources}:
                result = self.state()
                result["ready_ms"] = 0
                return result
            old = self.child
            self.generation += 1
            generation = self.generation
            self.ready = False
            self.last_error = ""

        if old is not None and old.poll() is None:
            old.kill()
            try:
                old.wait(timeout=3)
            except subprocess.TimeoutExpired:
                raise RuntimeError("previous compositor did not stop")
            # Let the previous MXL writer release its fixed output flow before
            # the replacement process claims it.
            time.sleep(0.1)

        error = "compositor did not start"
        for _attempt in range(3):
            with self.lock:
                self.ready = False
                self.last_error = ""
            child = subprocess.Popen(
                self._command(count, sources),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            with self.lock:
                self.child = child
            threading.Thread(target=self._copy_output, args=(child, generation), daemon=True).start()

            deadline = time.monotonic() + self.args.ready_timeout
            while time.monotonic() < deadline:
                with self.lock:
                    if generation == self.generation and self.ready:
                        self.scene = {"count": count, "sources": list(sources)}
                        self.last_ready_ms = round((time.monotonic() - started) * 1000)
                        return self.state()
                if child.poll() is not None:
                    break
                time.sleep(0.025)
            with self.lock:
                error = self.last_error or f"compositor exited with {child.poll()}"
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)
            time.sleep(0.2)
        raise RuntimeError(f"scene did not become ready: {error}")

    def state(self) -> dict[str, object]:
        with self.lock:
            child = self.child
            return {
                "bank": self.args.bank,
                "layout": self.scene["count"],
                "sources": self.scene["sources"],
                "state": "ready" if self.ready and child and child.poll() is None else "starting",
                "ready_ms": self.last_ready_ms,
                "child_pid": child.pid if child and child.poll() is None else None,
                "last_error": self.last_error,
            }

    def stop(self) -> None:
        with self.lock:
            child = self.child
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=3)


ENGINE: SceneEngine


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/state":
            return self._send(404, {"error": "not found"})
        self._send(200, ENGINE.state())

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/scene":
            return self._send(404, {"error": "not found"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(size))
            result = ENGINE.configure(int(payload["count"]), [int(item) for item in payload["sources"]])
            self._send(200, result)
        except (KeyError, TypeError, ValueError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send(503, {"error": str(exc)})

    def log_message(self, _format: str, *_args: object) -> None:
        return


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--bank", choices=("a", "b"), required=True)
    result.add_argument("--domain", required=True)
    result.add_argument("--input", action="append", required=True)
    result.add_argument("--output-flow-id", required=True)
    result.add_argument("--assets", required=True)
    result.add_argument("--compositor", required=True)
    result.add_argument("--rate", default="30000/1001")
    result.add_argument("--control-host", default="0.0.0.0")
    result.add_argument("--control-port", type=int, default=9720)
    result.add_argument("--ready-timeout", type=float, default=10)
    result.add_argument("--initial-count", type=int, required=True)
    result.add_argument("--initial-sources", required=True)
    return result


def main() -> None:
    global ENGINE
    args = parser().parse_args()
    if len(args.input) != 8:
        raise SystemExit("exactly eight --input values are required")
    ENGINE = SceneEngine(args)
    initial_sources = [int(item) for item in args.initial_sources.split(",")]
    ENGINE.configure(args.initial_count, initial_sources)
    server = ThreadingHTTPServer((args.control_host, args.control_port), Handler)

    def stop(_signum: int, _frame: object) -> None:
        # shutdown() must run outside serve_forever()'s thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        ENGINE.stop()


if __name__ == "__main__":
    main()
