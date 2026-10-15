#!/usr/bin/env python3
"""IS-04/IS-05 view and router bridge for an MXL Core facility.

The Node API advertises Core sources as MXL Senders and the Program/Preview
buses as Receivers. The Connection API maps immediate Receiver activations to
Core's grain-boundary ``cut`` and ``preview`` commands. Scheduled activation
and disconnect are deliberately rejected because Core does not implement those
semantics yet.
"""

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
    def __init__(
        self,
        facility: str,
        href: str,
        port: int,
        domain_id: str,
        instance_id: str = "facility",
    ):
        self.facility = facility.rstrip("/")
        self.href = href
        self.port = port
        self.domain_id = domain_id
        self.instance_id = instance_id
        self.node_id = self.stable("node", "facility")
        self.device_id = self.stable("device", "switcher")
        self.sources_data: list[dict] = []
        self.core: dict = {}
        self.revision = version()
        self.lock = threading.Lock()

    def stable(self, kind: str, name: str) -> str:
        """Return a stable ID scoped to this bridge instance.

        The historical ``facility`` instance deliberately keeps the original
        IDs so existing NMOS installations do not see an identity migration.
        Additional bridge instances must provide a unique ``instance_id`` so
        their Node and all dependent resources remain disjoint.
        """
        if self.instance_id == "facility":
            return stable(kind, name)
        return str(
            uuid.uuid5(
                NAMESPACE,
                f"mxl-core:{self.instance_id}:{kind}:{name}",
            )
        )

    def get_json(self, path: str):
        request = urllib.request.Request(
            self.facility + path, headers={"User-Agent": "mxl-core-nmos/1.0"}
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    def post_json(self, path: str, value: dict):
        request = urllib.request.Request(
            self.facility + path,
            data=json.dumps(value).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "mxl-core-nmos/1.1",
            },
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)

    def refresh(self):
        sources = self.get_json("/api/mxl/v1/sources")
        try:
            core = self.get_json("/api/skin/state")["core"]
        except Exception:
            core = self.get_json("/api/mxl/v1/state")
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
            "id": self.stable(kind, name),
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
                "controls": [
                    {
                        "type": "urn:x-nmos:control:sr-ctrl/v1.1",
                        "href": self.href.rstrip("/") + "/x-nmos/connection/v1.1/",
                    }
                ],
                "senders": [self.stable("sender", name) for name in names],
                "receivers": [
                    self.stable("receiver", "program"),
                    self.stable("receiver", "preview"),
                ],
            }
        )
        return value

    def sources(self):
        with self.lock:
            rows = json.loads(json.dumps(self.sources_data))
        output = []
        for row in rows:
            value = self.base("source", row["id"], row["label"])
            value["tags"] = {
                "urn:x-mxl:tag:source_kind": [row.get("kind", "other")],
                "urn:x-mxl:tag:capabilities": row.get("capabilities", ["video"]),
            }
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
            value["tags"] = {
                "urn:x-mxl:tag:source_kind": [row.get("kind", "other")],
                "urn:x-mxl:tag:capabilities": row.get("capabilities", ["video"]),
            }
            value.update(
                {
                    "format": "urn:x-nmos:format:video",
                    "source_id": self.stable("source", row["id"]),
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
                "urn:x-mxl:tag:source_kind": [row.get("kind", "other")],
                "urn:x-mxl:tag:capabilities": row.get("capabilities", ["video"]),
            }
            role = "PGM" if row["id"] == program else "PVW" if row["id"] == preview else None
            if role:
                tags["urn:x-nmos:tag:grouphint/v1.0"] = [role]
            value.update(
                {
                    "tags": tags,
                    "flow_id": self.stable("flow", row["id"]),
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
                    "tags": {
                        "urn:x-nmos:tag:grouphint/v1.0": [
                            "PGM" if name == "program" else "PVW"
                        ]
                    },
                    "device_id": self.device_id,
                    "format": "urn:x-nmos:format:video",
                    "caps": {
                        "media_types": ["video/v210"],
                        "constraint_sets": [
                            {
                                "urn:x-nmos:cap:format:media_type": {
                                    "enum": ["video/v210"]
                                }
                            }
                        ],
                    },
                    "transport": "urn:x-nmos:transport:mxl",
                    "interface_bindings": [],
                    "subscription": {
                        "sender_id": self.stable("sender", attached) if attached else None,
                        "active": bool(attached),
                    },
                }
            )
            output.append(value)
        return output

    def source_name_for_sender(self, sender_id: str) -> str | None:
        with self.lock:
            rows = json.loads(json.dumps(self.sources_data))
        return next(
            (
                row["id"]
                for row in rows
                if self.stable("sender", row["id"]) == sender_id
            ),
            None,
        )

    def source_name_for_flow(self, flow_id: str) -> str | None:
        with self.lock:
            rows = json.loads(json.dumps(self.sources_data))
        return next((row["id"] for row in rows if row["flow_id"] == flow_id), None)

    def flow_id_for_source(self, source_name: str | None) -> str | None:
        if source_name is None:
            return None
        with self.lock:
            rows = json.loads(json.dumps(self.sources_data))
        return next(
            (row["flow_id"] for row in rows if row["id"] == source_name),
            None,
        )

    def attached_sender(self, receiver_name: str) -> str | None:
        with self.lock:
            attached = self.core.get(receiver_name)
        return self.stable("sender", attached) if attached else None

    def receiver_name(self, receiver_id: str) -> str | None:
        return next(
            (
                name
                for name in ("program", "preview")
                if self.stable("receiver", name) == receiver_id
            ),
            None,
        )

    def connection_state(self, receiver_name: str):
        with self.lock:
            attached = self.core.get(receiver_name)
        return {
            "sender_id": self.attached_sender(receiver_name),
            "master_enable": True,
            "activation": {
                "mode": None,
                "requested_time": None,
                "activation_time": None,
            },
            "transport_file": {"data": None, "type": None},
            "transport_params": [
                {
                    "mxl_domain_id": self.domain_id if attached else None,
                    "mxl_flow_id": self.flow_id_for_source(attached),
                }
            ],
        }

    def activate_receiver(self, receiver_name: str, patch: dict):
        activation = patch.get("activation") or {}
        mode = activation.get("mode")
        if mode not in (None, "activate_immediate"):
            raise ValueError("only activate_immediate is supported")
        if patch.get("master_enable", True) is not True:
            raise ValueError("MXL Core Program and Preview cannot be disconnected")
        sender_id = patch.get("sender_id")
        source_name = self.source_name_for_sender(sender_id) if sender_id else None
        transport_params = patch.get("transport_params") or []
        if transport_params:
            params = transport_params[0]
            domain_id = params.get("mxl_domain_id")
            if domain_id not in (None, "auto", self.domain_id):
                raise ValueError("mxl_domain_id is not available on this Node")
            flow_source = self.source_name_for_flow(params.get("mxl_flow_id"))
            if flow_source is None:
                raise ValueError("mxl_flow_id is not an advertised MXL Core source")
            if source_name is not None and source_name != flow_source:
                raise ValueError("sender_id and mxl_flow_id identify different sources")
            source_name = flow_source
            sender_id = self.stable("sender", source_name)
        if sender_id is None:
            raise ValueError(
                "sender_id or transport_params.mxl_flow_id is required; "
                "Core buses cannot be disconnected"
            )
        if source_name is None:
            raise ValueError("sender_id is not an advertised MXL Core source")
        path = "/api/mxl/v1/cut" if receiver_name == "program" else "/api/mxl/v1/preview"
        self.post_json(path, {"source": source_name})

        # Core accepts commands immediately and applies them at the next grain
        # boundary. Wait briefly so the IS-05 response reflects that frame.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            self.refresh()
            if self.attached_sender(receiver_name) == sender_id:
                break
            time.sleep(0.01)
        if self.attached_sender(receiver_name) != sender_id:
            raise RuntimeError("Core accepted the route but did not apply it in time")

        value = self.connection_state(receiver_name)
        if mode == "activate_immediate":
            value["activation"] = {
                "mode": "activate_immediate",
                "requested_time": None,
                "activation_time": version(),
            }
        return value


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

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def connection_get(self, path: str):
        base = "/x-nmos/connection/v1.1"
        sender_ids = [value["id"] for value in MODEL.senders()]
        receiver_ids = [value["id"] for value in MODEL.receivers()]
        tables = {
            "/x-nmos/connection": ["v1.1/"],
            base: ["bulk/", "single/"],
            base + "/bulk": ["senders/", "receivers/"],
            base + "/single": ["senders/", "receivers/"],
            base + "/single/senders": [value + "/" for value in sender_ids],
            base + "/single/receivers": [value + "/" for value in receiver_ids],
        }
        if path in tables:
            return tables[path]

        for kind, ids in (("senders", sender_ids), ("receivers", receiver_ids)):
            prefix = base + "/single/" + kind + "/"
            if not path.startswith(prefix):
                continue
            parts = path[len(prefix) :].split("/")
            resource_id = parts[0]
            if resource_id not in ids:
                raise KeyError(resource_id)
            if len(parts) == 1:
                names = ["constraints/", "staged/", "active/", "transporttype/"]
                if kind == "senders":
                    names.append("transportfile/")
                return names
            endpoint = parts[1]
            if endpoint == "transporttype":
                return "urn:x-nmos:transport:mxl"
            if endpoint == "constraints":
                with MODEL.lock:
                    rows = json.loads(json.dumps(MODEL.sources_data))
                if kind == "receivers":
                    return [
                        {
                            "mxl_domain_id": {"enum": [MODEL.domain_id, "auto"]},
                            "mxl_flow_id": {
                                "enum": [row["flow_id"] for row in rows]
                            },
                        }
                    ]
                source = MODEL.source_name_for_sender(resource_id)
                flow_id = next(
                    row["flow_id"] for row in rows if row["id"] == source
                )
                return [
                    {
                        "mxl_domain_id": {"enum": [MODEL.domain_id]},
                        "mxl_flow_id": {"enum": [flow_id]},
                    }
                ]
            if endpoint == "transportfile" and kind == "senders":
                raise KeyError(endpoint)
            if endpoint in ("staged", "active"):
                if kind == "receivers":
                    return MODEL.connection_state(MODEL.receiver_name(resource_id))
                source = MODEL.source_name_for_sender(resource_id)
                with MODEL.lock:
                    flow_id = next(
                        row["flow_id"] for row in MODEL.sources_data if row["id"] == source
                    )
                return {
                    "receiver_id": None,
                    "master_enable": True,
                    "activation": {
                        "mode": None,
                        "requested_time": None,
                        "activation_time": None,
                    },
                    "transport_params": [
                        {
                            "mxl_domain_id": MODEL.domain_id,
                            "mxl_flow_id": flow_id,
                        }
                    ],
                }
            raise KeyError(endpoint)
        raise KeyError(path)

    def do_GET(self):
        path = self.path.rstrip("/")
        tables = {
            "/x-nmos": ["node/", "connection/"],
            "/x-nmos/node": ["v1.3/"],
            "/x-nmos/node/v1.3": ["self/", "devices/", "sources/", "flows/", "senders/", "receivers/"],
        }
        if path in tables:
            return self.send_json(tables[path])
        if path.startswith("/x-nmos/connection"):
            try:
                return self.send_json(self.connection_get(path))
            except KeyError:
                return self.send_json({"code": 404, "error": "not found"}, 404)
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

    def do_PATCH(self):
        path = self.path.rstrip("/")
        prefix = "/x-nmos/connection/v1.1/single/receivers/"
        if path.startswith(prefix) and path.endswith("/staged"):
            receiver_id = path[len(prefix) : -len("/staged")]
            receiver_name = MODEL.receiver_name(receiver_id)
            if receiver_name is None:
                return self.send_json({"code": 404, "error": "receiver not found"}, 404)
            try:
                return self.send_json(
                    MODEL.activate_receiver(receiver_name, self.read_json())
                )
            except (ValueError, json.JSONDecodeError) as error:
                return self.send_json({"code": 400, "error": str(error)}, 400)
            except Exception as error:
                return self.send_json({"code": 503, "error": str(error)}, 503)
        return self.send_json({"code": 405, "error": "method not supported"}, 405)

    def do_POST(self):
        path = self.path.rstrip("/")
        if path == "/x-nmos/connection/v1.1/bulk/receivers":
            try:
                requests = self.read_json()
                if not isinstance(requests, list):
                    raise ValueError("bulk request must be an array")
                results = []
                for request in requests:
                    receiver_id = request.get("id") if isinstance(request, dict) else None
                    receiver_name = MODEL.receiver_name(receiver_id) if receiver_id else None
                    if receiver_name is None:
                        results.append(
                            {"id": receiver_id, "code": 404, "error": "receiver not found"}
                        )
                        continue
                    try:
                        MODEL.activate_receiver(receiver_name, request.get("params") or {})
                        results.append({"id": receiver_id, "code": 200})
                    except ValueError as error:
                        results.append({"id": receiver_id, "code": 400, "error": str(error)})
                    except Exception as error:
                        results.append({"id": receiver_id, "code": 503, "error": str(error)})
                return self.send_json(results)
            except (ValueError, json.JSONDecodeError) as error:
                return self.send_json({"code": 400, "error": str(error)}, 400)
        return self.send_json({"code": 405, "error": "method not supported"}, 405)


def registry_resources(model: Model) -> dict[str, list[dict]]:
    resources = {
        "node": [model.self_resource()],
        "device": [model.device()],
    }
    resources.update(
        {
            kind: factory()
            for kind, factory in (
                ("source", model.sources),
                ("flow", model.flows),
                ("sender", model.senders),
                ("receiver", model.receivers),
            )
        }
    )
    return resources


def withdrawal_plan(
    registered: dict[str, set[str]], current: dict[str, set[str]]
) -> list[tuple[str, str]]:
    plan = []
    for kind in ("receiver", "sender", "flow", "source", "device", "node"):
        plan.extend(
            (kind, resource_id)
            for resource_id in sorted(registered[kind] - current[kind])
        )
    return plan


def register_loop(model: Model, registry: str):
    api = registry.rstrip("/") + "/x-nmos/registration/v1.3"
    registered: dict[str, set[str]] = {
        "node": set(),
        "device": set(),
        "source": set(),
        "flow": set(),
        "sender": set(),
        "receiver": set(),
    }

    def post(kind: str, value: dict):
        request = urllib.request.Request(
            api + "/resource",
            data=json.dumps({"type": kind, "data": value}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "mxl-core-nmos/1.0"},
        )
        urllib.request.urlopen(request, timeout=5).read()

    def delete(kind: str, resource_id: str):
        plural = {"source": "sources", "flow": "flows"}.get(kind, kind + "s")
        request = urllib.request.Request(
            api + f"/resource/{plural}/{resource_id}",
            method="DELETE",
            headers={"User-Agent": "mxl-core-nmos/1.1"},
        )
        urllib.request.urlopen(request, timeout=5).read()

    while True:
        try:
            resources = registry_resources(model)
            current = {
                kind: {value["id"] for value in values}
                for kind, values in resources.items()
            }
            for kind, values in resources.items():
                for value in values:
                    post(kind, value)
            # Withdraw dependants first so a conforming Registry never sees a
            # Flow without its Source or a Sender without its Flow.
            for kind, resource_id in withdrawal_plan(registered, current):
                delete(kind, resource_id)
            registered = current
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
    parser.add_argument(
        "--instance-id",
        default="facility",
        help=(
            "Stable identity namespace for this NMOS bridge. Keep the default "
            "for an existing installation; use a unique value for every "
            "additional bridge instance."
        ),
    )
    parser.add_argument("--registry", help="Optional NMOS registry base URL")
    args = parser.parse_args()
    href = args.href or f"http://{args.host}:{args.port}/"
    MODEL = Model(
        args.facility,
        href,
        args.port,
        args.domain_id,
        instance_id=args.instance_id,
    )
    MODEL.refresh()
    threading.Thread(target=MODEL.poll, daemon=True).start()
    if args.registry:
        threading.Thread(target=register_loop, args=(MODEL, args.registry), daemon=True).start()
    print(
        json.dumps(
            {
                "event": "nmos_ready",
                "href": href,
                "facility": args.facility,
                "instance_id": args.instance_id,
                "node_id": MODEL.node_id,
            }
        ),
        flush=True,
    )
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
