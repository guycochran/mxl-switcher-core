#!/usr/bin/env python3
"""Local-only ATEM-style operator skin for MXL Core and SuperSource banks."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import time
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
        preview_url: str | None = None,
        multiview_url: str | None = None,
        audio_meter_url: str | None = None,
        audio_mixer_url: str | None = None,
        nmos_node_url: str | None = None,
        nmos_registry_url: str | None = None,
        easy_mxl_url: str | None = None,
        easy_mxl_domain: str | None = None,
        recovery_mapping: pathlib.Path | None = None,
        recovery_planner: pathlib.Path | None = None,
        recovery_executor: pathlib.Path | None = None,
        recovery_token_env: str = "EASY_MXL_TOKEN",
        recovery_apply_enabled: bool = False,
        extension_health_urls: dict[str, str] | None = None,
    ):
        self.root = root
        self.core = core.rstrip("/")
        self.helper = helper
        self.program_url = program_url
        self.preview_url = preview_url
        self.multiview_url = multiview_url
        self.audio_meter_url = audio_meter_url.rstrip("/") if audio_meter_url else None
        self.audio_mixer_url = audio_mixer_url.rstrip("/") if audio_mixer_url else None
        self.nmos_node_url = nmos_node_url.rstrip("/") if nmos_node_url else None
        self.nmos_registry_url = nmos_registry_url.rstrip("/") if nmos_registry_url else None
        self.easy_mxl_url = easy_mxl_url.rstrip("/") if easy_mxl_url else None
        self.easy_mxl_domain = easy_mxl_domain
        self.recovery_mapping = recovery_mapping
        self.recovery_planner = recovery_planner
        self.recovery_executor = recovery_executor
        self.recovery_token_env = recovery_token_env
        self.recovery_apply_enabled = recovery_apply_enabled
        self.extension_health_urls = extension_health_urls or {}
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

    @staticmethod
    def url_json(url: str, timeout: float = 2):
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read())

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
            "preview_url": self.preview_url,
            "multiview_url": self.multiview_url,
            "audio": self.audio_state(),
            "audio_mixer": self.audio_mixer_state(),
        }

    def audio_state(self):
        if not self.audio_meter_url:
            return {"state": "disabled", "meters": {}}
        try:
            with urllib.request.urlopen(self.audio_meter_url + "/meters", timeout=1) as response:
                value = json.loads(response.read())
            value["state"] = "ok"
            return value
        except (OSError, TimeoutError, json.JSONDecodeError) as error:
            return {"state": "unavailable", "meters": {}, "error": str(error)}

    def nmos_state(self):
        if not self.nmos_node_url:
            return {"state": "disabled", "registry": "disabled"}
        try:
            base = self.nmos_node_url + "/x-nmos/node/v1.3"
            node = self.url_json(base + "/self")
            counts = {
                resource: len(self.url_json(base + f"/{resource}"))
                for resource in ("devices", "sources", "flows", "senders", "receivers")
            }
            clocks = node.get("clocks") or []
            clock_type = "unknown"
            if clocks:
                clock_type = clocks[0].get("ref_type", "unknown")
            registration = "disabled"
            if self.nmos_registry_url:
                registration = "offline"
                try:
                    nodes = self.url_json(
                        self.nmos_registry_url + "/x-nmos/query/v1.3/nodes"
                    )
                    registration = (
                        "registered"
                        if any(item.get("id") == node.get("id") for item in nodes)
                        else "missing"
                    )
                except (OSError, TimeoutError, json.JSONDecodeError):
                    pass
            return {
                "state": "online",
                "registry": registration,
                "node_id": node.get("id"),
                "label": node.get("label") or node.get("description") or "MXL Core",
                "clock": clock_type,
                **counts,
            }
        except (OSError, TimeoutError, json.JSONDecodeError) as error:
            return {"state": "unavailable", "registry": "unknown", "error": str(error)}

    def health_dashboard_state(self):
        _status, _kind, raw_state = self.core_json("/api/mxl/v1/state")
        _status, _kind, raw_health = self.core_json("/api/mxl/v1/health")
        _status, _kind, raw_sources = self.core_json("/api/mxl/v1/sources")
        return {
            "sample_time": time.time(),
            "core": json.loads(raw_state),
            "health": json.loads(raw_health),
            "sources": json.loads(raw_sources),
            "audio": self.audio_state(),
            "audio_mixer": self.audio_mixer_state(),
            "nmos": self.nmos_state(),
            "extensions": self.extension_states(),
        }

    def extension_states(self):
        states = []
        for configured_name, base_url in sorted(self.extension_health_urls.items()):
            try:
                snapshot = self.url_json(base_url + "/api/v1/state", timeout=1)
                states.append({
                    "configured_name": configured_name,
                    "name": snapshot.get("name") or configured_name,
                    "kind": snapshot.get("kind") or "extension",
                    "state": snapshot.get("state") or "unknown",
                    "ready": snapshot.get("ready") is True,
                    "revision": snapshot.get("revision"),
                    "restarts": snapshot.get("restarts"),
                    "last_error": snapshot.get("last_error"),
                })
            except (OSError, TimeoutError, json.JSONDecodeError, ValueError) as error:
                states.append({
                    "configured_name": configured_name,
                    "name": configured_name,
                    "kind": "extension",
                    "state": "unavailable",
                    "ready": False,
                    "revision": None,
                    "restarts": None,
                    "last_error": str(error),
                })
        return states

    def recovery_configured(self):
        return all((
            self.easy_mxl_url,
            self.easy_mxl_domain,
            self.recovery_mapping,
            self.recovery_planner,
            self.recovery_executor,
        ))

    @staticmethod
    def plan_id(plan):
        canonical = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def command_error(result, fallback):
        detail = (result.stderr or result.stdout or fallback).strip()
        try:
            parsed = json.loads(detail)
            return parsed.get("error") or detail
        except json.JSONDecodeError:
            return detail

    def recovery_plan(self):
        if not self.recovery_configured():
            return {"state": "disabled", "apply_enabled": False}
        for path, label in (
            (self.recovery_mapping, "mapping"),
            (self.recovery_planner, "planner"),
            (self.recovery_executor, "executor"),
        ):
            if not pathlib.Path(path).is_file():
                raise RuntimeError(f"source recovery {label} is not installed")
        with tempfile.TemporaryDirectory(prefix="mxl-recovery-plan-") as directory:
            output = pathlib.Path(directory) / "plan.json"
            command = [
                sys.executable,
                str(self.recovery_planner),
                "--mapping", str(self.recovery_mapping),
                "--easy-mxl-url", self.easy_mxl_url,
                "--domain", self.easy_mxl_domain,
                "--core-url", self.core,
                "--token-env", self.recovery_token_env,
                "--output", str(output),
            ]
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
                env=os.environ.copy(),
            )
            if result.returncode not in (0, 1) or not output.is_file():
                raise RuntimeError(self.command_error(result, "source discovery failed"))
            plan = json.loads(output.read_text())
        _status, _kind, raw = self.core_json("/api/mxl/v1/state")
        core = json.loads(raw)
        return {
            "state": "blocked" if plan.get("blocked") else "ready",
            "apply_enabled": self.recovery_apply_enabled,
            "plan_id": self.plan_id(plan),
            "revision": core.get("revision"),
            "program": core.get("program"),
            "preview": core.get("preview"),
            "summary": plan.get("summary") or {},
            "sources": [
                {
                    "id": item.get("id"),
                    "status": item.get("status"),
                    "reasons": item.get("reasons") or [],
                    "current": item.get("current"),
                    "desired": item.get("desired"),
                }
                for item in plan.get("sources", [])
            ],
            "plan": plan,
        }

    def run_recovery_executor(self, recovery, apply=False, expected_revision=None, confirm=None):
        with tempfile.TemporaryDirectory(prefix="mxl-recovery-exec-") as directory:
            plan_path = pathlib.Path(directory) / "plan.json"
            plan_path.write_text(json.dumps(recovery["plan"], sort_keys=True) + "\n")
            command = [
                sys.executable,
                str(self.recovery_executor),
                "--plan", str(plan_path),
                "--core-url", self.core,
            ]
            if apply:
                command.extend([
                    "--apply",
                    "--expect-revision", str(expected_revision),
                    "--confirm", str(confirm),
                ])
            result = subprocess.run(command, text=True, capture_output=True, timeout=30, check=False)
            if result.returncode:
                raise RuntimeError(self.command_error(result, "source recovery failed"))
            return json.loads(result.stdout)

    def recovery_preflight(self, payload):
        recovery = self.recovery_plan()
        if recovery["state"] == "disabled":
            raise RuntimeError("source recovery is not configured")
        if payload.get("plan_id") != recovery["plan_id"]:
            raise ValueError("source inventory changed; scan and review the new plan")
        result = self.run_recovery_executor(recovery)
        return {**result, "plan_id": recovery["plan_id"], "apply_enabled": recovery["apply_enabled"]}

    def recovery_apply(self, payload):
        if not self.recovery_apply_enabled:
            raise RuntimeError("source reconnect is not armed on this Skin")
        if payload.get("confirm") != "APPLY_REBINDS":
            raise ValueError("type APPLY_REBINDS to confirm")
        expected_revision = payload.get("expected_revision")
        if not isinstance(expected_revision, int):
            raise ValueError("expected_revision must be an integer")
        recovery = self.recovery_plan()
        if payload.get("plan_id") != recovery["plan_id"]:
            raise ValueError("source inventory changed; scan and review the new plan")
        if expected_revision != recovery["revision"]:
            raise ValueError("Core revision changed; scan and review again")
        return self.run_recovery_executor(
            recovery,
            apply=True,
            expected_revision=expected_revision,
            confirm=payload["confirm"],
        )

    def audio_mixer_state(self):
        if not self.audio_mixer_url:
            return {"state": "disabled"}
        try:
            with urllib.request.urlopen(self.audio_mixer_url + "/state", timeout=1) as response:
                return json.loads(response.read())
        except (OSError, TimeoutError, json.JSONDecodeError) as error:
            return {"state": "unavailable", "error": str(error)}

    def audio_mixer_control(self, payload):
        if not self.audio_mixer_url:
            raise RuntimeError("audio mixer is not enabled")
        request = urllib.request.Request(
            self.audio_mixer_url + "/control",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.loads(response.read())

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
        program = core.get("program")
        preview = core.get("preview")
        if program == "supersource-a":
            bank = "b"
        elif program == "supersource-b":
            bank = "a"
        else:
            # When neither ME is on air, preserve the ME already staged in Preview.
            bank = "b" if preview == "supersource-a" else "a"
        engine = {}
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
            try:
                engine = json.loads(result.stdout.strip().splitlines()[-1])
            except (IndexError, json.JSONDecodeError):
                engine = {}
        with self.lock:
            self.banks[bank] = {"count": count, "sources": sources, "ready": True}
            self.persist()
        status, _kind, receipt = self.core_json(
            "/api/mxl/v1/preview", "POST", {"source": f"supersource-{bank}"}
        )
        return {
            "bank": bank,
            "scene": self.banks[bank],
            "receipt": json.loads(receipt),
            "status": status,
            "engine": engine,
        }


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
            if self.path == "/api/skin/health":
                return self.send_json(200, self.app.health_dashboard_state())
            if self.path == "/api/skin/recovery":
                return self.send_json(200, self.app.recovery_plan())
            if self.path.startswith("/api/mxl/v1/"):
                status, kind, data = self.app.core_json(self.path)
                return self.send_bytes(status, kind, data)
            path = "/index.html" if self.path in ("/", "/skin") else self.path.split("?", 1)[0]
            if path in ("/health", "/health/"):
                path = "/health.html"
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
            if self.path == "/api/skin/audio":
                return self.send_json(200, self.app.audio_mixer_control(self.read_json()))
            if self.path == "/api/skin/recovery/preflight":
                return self.send_json(200, self.app.recovery_preflight(self.read_json()))
            if self.path == "/api/skin/recovery/apply":
                return self.send_json(200, self.app.recovery_apply(self.read_json()))
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


def parse_extension_health(values):
    result = {}
    for value in values:
        name, separator, url = value.partition("=")
        if not separator or not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name):
            raise ValueError("extension health must be NAME=http://127.0.0.1:PORT")
        url = url.rstrip("/")
        if not re.fullmatch(r"https?://(?:127\.0\.0\.1|localhost|\[::1\])(?::[0-9]+)?", url):
            raise ValueError(f"extension health URL for {name} must be loopback HTTP(S)")
        if name in result:
            raise ValueError(f"duplicate extension health name: {name}")
        result[name] = url
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9700)
    parser.add_argument("--core", default="http://127.0.0.1:18085")
    parser.add_argument("--supersource-helper")
    parser.add_argument("--program-url", help="WebRTC confidence-player URL to embed in Program")
    parser.add_argument("--preview-url", help="WebRTC confidence-player URL to embed in Preview")
    parser.add_argument("--multiview-url", help="WebRTC operator-multiview URL to embed in the source wall")
    parser.add_argument("--audio-meter-url", help="Loopback MXL audio meter service URL")
    parser.add_argument("--audio-mixer-url", help="Loopback MXL audio mixer service URL")
    parser.add_argument("--nmos-node-url", help="Loopback NMOS Node API base URL")
    parser.add_argument("--nmos-registry-url", help="Loopback NMOS Registry Query API base URL")
    parser.add_argument("--easy-mxl-url", help="Loopback EasyMXL API base URL for source discovery")
    parser.add_argument("--easy-mxl-domain", help="EasyMXL domain used by the Core")
    parser.add_argument("--recovery-mapping", help="Stable-source discovery mapping JSON")
    parser.add_argument("--recovery-planner", help="Path to mxl_discovery_planner.py")
    parser.add_argument("--recovery-executor", help="Path to mxl_rebind_apply.py")
    parser.add_argument("--recovery-token-env", default="EASY_MXL_TOKEN")
    parser.add_argument("--enable-source-reconnect", action="store_true",
                        help="allow confirmed reconnects; scan/preflight remain read-only without this")
    parser.add_argument(
        "--extension-health",
        action="append",
        default=[],
        metavar="NAME=LOOPBACK_URL",
        help="repeatable extension state endpoint shown on the EIC dashboard",
    )
    parser.add_argument("--state-file")
    args = parser.parse_args()
    root = pathlib.Path(__file__).with_name("static").resolve()
    state_file = pathlib.Path(args.state_file).resolve() if args.state_file else root.parent / "state.json"
    try:
        extension_health_urls = parse_extension_health(args.extension_health)
    except ValueError as error:
        parser.error(str(error))
    Handler.app = App(
        root,
        args.core,
        args.supersource_helper,
        state_file,
        args.program_url,
        args.preview_url,
        args.multiview_url,
        args.audio_meter_url,
        args.audio_mixer_url,
        args.nmos_node_url,
        args.nmos_registry_url,
        args.easy_mxl_url,
        args.easy_mxl_domain,
        pathlib.Path(args.recovery_mapping).resolve() if args.recovery_mapping else None,
        pathlib.Path(args.recovery_planner).resolve() if args.recovery_planner else None,
        pathlib.Path(args.recovery_executor).resolve() if args.recovery_executor else None,
        args.recovery_token_env,
        args.enable_source_reconnect,
        extension_health_urls,
    )
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(json.dumps({"event": "skin_ready", "url": f"http://{args.host}:{args.port}", "live_supersource": bool(args.supersource_helper)}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
