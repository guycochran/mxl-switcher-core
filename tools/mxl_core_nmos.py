#!/usr/bin/env python3
"""Read-only IS-04 v1.3/BCP-007-03 view of an MXL Core facility."""

from __future__ import annotations

import argparse
import json
import socket
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


NAMESPACE = uuid.UUID("6ba7b812-9dad-11d1-80b4-00c04fd430c8")


def stable(kind: str, name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"mxl-core:{kind}:{name}"))


def version() -> str:
    now = time.time()
    return f"{int(now)}:{int(now % 1 * 1_000_000_000):09d}"


class Model:
    def __init__(self, facility: str, href: str, port: int, domain_id: str):
        self.facility = facility.rstrip("/")
        self.href = href
        self.port = port
        self.domain_id = domain_id
        self.node_id = stable("node", "facility")
        self.device_id = stable("device", "switcher")
        self.sources_data: list[dict] = []
        self.core: dict = {}
        self.revision = version()
        self.lock = threading.Lock()

    def get_json(self, path: str):
        request = urllib.request.Request(
            self.facility + path, headers={"User-Agent": "mxl-core-nmos/1.0"}
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    def refresh(self):
        sources = self.get_json("/api/mxl/v1/sources")
        skin = self.get_json("/api/skin/state")
        core = skin["core"]
        with self.lock:
            signature = json.dumps([sources, core.get("program"), core.get("preview")], sort_keys=True)
            old = json.dumps(
                [self.sources_data, self.core.get("program"), self.core.get("preview")], sort_keys=True
            )
            self.sources_data = sources
            self.core = core
            if signature != old:
                self.revision = version()

    def poll(self):
        while True:
            try:
                self.refresh()
            except Exception as error:
                print(f"facility poll: {error}", flush=True)
            time.sleep(1)

    def base(self, kind: str, name: str, label: str):
        return {
            "id": stable(kind, name),
            "version": self.revision,
            "label": label,
            "description": "MXL Core Azure demo",
            "tags": {},
        }

    def self_resource(self):
        value = self.base("node", "facility", "MXL Core Facility")
        value.update(
            {
                "id": self.node_id,
                "href": self.href,
                "hostname": socket.gethostname(),
                "caps": {},
                "api": {
                    "versions": ["v1.3"],
                    "endpoints": [
                        {
                            "host": self.href.split("//", 1)[-1].split(":", 1)[0],
                            "port": self.port,
                            "protocol": "http",
                        }
                    ],
                },
                "services": [],
                "clocks": [{"name": "clk0", "ref_type": "internal"}],
                "interfaces": [],
            }
        )
        return value

    def device(self):
        value = self.base("device", "switcher", "MXL Core Switcher")
        with self.lock:
            names = [item["id"] for item in self.sources_data]
        value.update(
            {
                "id": self.device_id,
                "type": "urn:x-nmos:device:generic",
                "node_id": self.node_id,
                "controls": [],
                "senders": [stable("sender", name) for name in names],
                "receivers": [stable("receiver", "program"), stable("receiver", "preview")],
            }
        )
        return value

    def sources(self):
        with self.lock:
            rows = json.loads(json.dumps(self.sources_data))
        output = []
        for row in rows:
            value = self.base("source", row["id"], row["label"])
            value.update(
                {
                    "format": "urn:x-nmos:format:video",
                    "caps": {},
                    "device_id": self.device_id,
                    "parents": [],
                    "clock_name": "clk0",
                    "grain_rate": {
                        "numerator": row["format"]["rate_numerator"],
                        "denominator": row["format"]["rate_denominator"],
                    },
                }
            )
            output.append(value)
        return output

    def flows(self):
        with self.lock:
            rows = json.loads(json.dumps(self.sources_data))
        output = []
        for row in rows:
            fmt = row["format"]
            value = self.base("flow", row["id"], row["label"])
            value.update(
                {
                    "format": "urn:x-nmos:format:video",
                    "source_id": stable("source", row["id"]),
                    "device_id": self.device_id,
                    "parents": [],
                    "media_type": fmt["encoding"],
                    "frame_width": fmt["width"],
                    "frame_height": fmt["height"],
                    "colorspace": "BT709",
                    "interlace_mode": "progressive",
                    "grain_rate": {
                        "numerator": fmt["rate_numerator"],
                        "denominator": fmt["rate_denominator"],
                    },
                }
            )
            output.append(value)
        return output

    def senders(self):
        with self.lock:
            rows = json.loads(json.dumps(self.sources_data))
            program, preview = self.core.get("program"), self.core.get("preview")
        output = []
        for row in rows:
            value = self.base("sender", row["id"], row["label"])
            tags = {
                "urn:x-mxl:tag:mxl_domain_id": [self.domain_id],
                "urn:x-mxl:tag:mxl_flow_id": [row["flow_id"]],
            }
            role = "PGM" if row["id"] == program else "PVW" if row["id"] == preview else None
            if role:
                tags["urn:x-nmos:tag:grouphint/v1.0"] = [role]
            value.update(
                {
                    "tags": tags,
                    "flow_id": stable("flow", row["id"]),
                    "device_id": self.device_id,
                    "transport": "urn:x-nmos:transport:mxl",
                    "manifest_href": None,
                    "interface_bindings": [],
                    "subscription": {"receiver_id": None, "active": True},
                }
            )
            output.append(value)
        return output

    def receivers(self):
        with self.lock:
            program, preview = self.core.get("program"), self.core.get("preview")
        output = []
        for name, attached in (("program", program), ("preview", preview)):
            value = self.base("receiver", name, f"MXL Core {name.title()}")
            value.update(
                {
                    "device_id": self.device_id,
                    "format": "urn:x-nmos:format:video",
                    "caps": {"media_types": ["video/v210"]},
                    "transport": "urn:x-nmos:transport:mxl",
                    "interface_bindings": [],
                    "subscription": {
                        "sender_id": stable("sender", attached) if attached else None,
                        "active": bool(attached),
                    },
                }
            )
            output.append(value)
        return output


MODEL: Model


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def send_json(self, value, status=200):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.rstrip("/")
        tables = {
            "/x-nmos": ["node/"],
            "/x-nmos/node": ["v1.3/"],
            "/x-nmos/node/v1.3": ["self/", "devices/", "sources/", "flows/", "senders/", "receivers/"],
        }
        if path in tables:
            return self.send_json(tables[path])
        collections = {
            "/x-nmos/node/v1.3/devices": lambda: [MODEL.device()],
            "/x-nmos/node/v1.3/sources": MODEL.sources,
            "/x-nmos/node/v1.3/flows": MODEL.flows,
            "/x-nmos/node/v1.3/senders": MODEL.senders,
            "/x-nmos/node/v1.3/receivers": MODEL.receivers,
        }
        if path == "/x-nmos/node/v1.3/self":
            return self.send_json(MODEL.self_resource())
        if path in collections:
            return self.send_json(collections[path]())
        for base, factory in collections.items():
            if path.startswith(base + "/"):
                item_id = path[len(base) + 1 :]
                item = next((value for value in factory() if value["id"] == item_id), None)
                return self.send_json(item or {"code": 404, "error": "not found"}, 200 if item else 404)
        return self.send_json({"code": 404, "error": "not found"}, 404)


def register_loop(model: Model, registry: str):
    api = registry.rstrip("/") + "/x-nmos/registration/v1.3"

    def post(kind: str, value: dict):
        request = urllib.request.Request(
            api + "/resource",
            data=json.dumps({"type": kind, "data": value}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "mxl-core-nmos/1.0"},
        )
        urllib.request.urlopen(request, timeout=5).read()

    while True:
        try:
            post("node", model.self_resource())
            post("device", model.device())
            for kind, factory in (
                ("source", model.sources),
                ("flow", model.flows),
                ("sender", model.senders),
                ("receiver", model.receivers),
            ):
                for value in factory():
                    post(kind, value)
            heartbeat = urllib.request.Request(
                api + f"/health/nodes/{model.node_id}",
                data=b"",
                method="POST",
                headers={"User-Agent": "mxl-core-nmos/1.0"},
            )
            urllib.request.urlopen(heartbeat, timeout=5).read()
        except Exception as error:
            print(f"registry: {error}", flush=True)
        time.sleep(5)


def main():
    global MODEL
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8021)
    parser.add_argument("--href")
    parser.add_argument("--facility", default="http://127.0.0.1:9710")
    parser.add_argument("--domain-id", default=stable("domain", "facility"))
    parser.add_argument("--registry", help="Optional NMOS registry base URL")
    args = parser.parse_args()
    href = args.href or f"http://{args.host}:{args.port}/"
    MODEL = Model(args.facility, href, args.port, args.domain_id)
    MODEL.refresh()
    threading.Thread(target=MODEL.poll, daemon=True).start()
    if args.registry:
        threading.Thread(target=register_loop, args=(MODEL, args.registry), daemon=True).start()
    print(json.dumps({"event": "nmos_ready", "href": href, "facility": args.facility}), flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
