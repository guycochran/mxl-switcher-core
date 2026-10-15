#!/usr/bin/env python3
"""Guarded operator executor for an MXL discovery reconciliation plan.

The discovery planner remains read-only. This separate tool applies only an
already-saved, unblocked plan and defaults to a preflight report. A live apply
requires an expected Core revision and the literal confirmation phrase
``APPLY_REBINDS``. Core must be reachable through loopback.

Before writing, the executor verifies that every current binding still equals
the plan's snapshot. For ordinary publisher UUID regeneration (no flow is
being transferred between sources), Preview is applied first, other off-air
sources follow, and Program is last. Every receipt and resulting binding is
verified. A failure rolls back completed requests in reverse order.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

CONFIRMATION = "APPLY_REBINDS"
SOURCES_PATH = "/api/mxl/v1/sources"
STATE_PATH = "/api/mxl/v1/state"


class ApplyError(RuntimeError):
    """A preflight, request, verification, or rollback failed."""


def _read_json(path: str):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _binding(source: dict) -> dict:
    return {
        "flow_id": (source.get("flow_id") or "").lower() or None,
        "audio_flow_id": (source.get("audio_flow_id") or "").lower() or None,
    }


def _valid_uuid(value, optional=False) -> bool:
    if value is None:
        return optional
    try:
        return str(uuid.UUID(value)) == value.lower()
    except (AttributeError, ValueError):
        return False


def _loopback_base(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ApplyError("Core URL must use HTTP loopback; run the executor on the Core host")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ApplyError("Core URL must not contain credentials, a query, or a fragment")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _request_json(base: str, path: str, method="GET", body=None, timeout=10.0):
    data = None if body is None else json.dumps(body, sort_keys=True).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise ApplyError(f"{method} {path} failed with HTTP {error.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise ApplyError(f"{method} {path} failed: {type(error).__name__}") from None


def _normalise_plan(plan: dict) -> list[dict]:
    if not isinstance(plan, dict) or plan.get("planner") != "mxl-discovery-planner":
        raise ApplyError("not an MXL discovery planner document")
    if plan.get("plan_version") != 1 or plan.get("read_only") is not True:
        raise ApplyError("unsupported or malformed plan")
    if plan.get("blocked") is not False:
        raise ApplyError("plan is blocked; operator resolution is required")
    source_entries = plan.get("sources")
    if not isinstance(source_entries, list) or not all(isinstance(entry, dict) for entry in source_entries):
        raise ApplyError("plan sources must be a list of objects")
    source_ids = [entry.get("id") for entry in source_entries]
    if any(not isinstance(source_id, str) or not source_id for source_id in source_ids):
        raise ApplyError("plan contains an invalid source id")
    if len(source_ids) != len(set(source_ids)):
        raise ApplyError("plan contains duplicate source ids")
    if any(entry.get("status") not in ("noop", "rebind") for entry in source_entries):
        raise ApplyError("plan contains a non-actionable source status")
    entries = {entry["id"]: entry for entry in source_entries}
    requests = plan.get("requests")
    if not isinstance(requests, list):
        raise ApplyError("plan requests must be a list")
    result = []
    requested_ids = []
    for request in requests:
        source_id = request.get("source") if isinstance(request, dict) else None
        entry = entries.get(source_id)
        expected_path = SOURCES_PATH + "/" + urllib.parse.quote(source_id or "", safe="")
        if (not source_id or entry is None or entry.get("status") != "rebind"
                or request.get("method") != "PUT" or request.get("path") != expected_path):
            raise ApplyError("plan contains an invalid rebind request")
        body = request.get("body")
        desired = entry.get("desired")
        current = entry.get("current")
        if (body != desired or not isinstance(body, dict) or not isinstance(current, dict)
                or set(body) != {"flow_id", "audio_flow_id"}
                or set(current) != {"flow_id", "audio_flow_id"}
                or not _valid_uuid(body.get("flow_id"))
                or not _valid_uuid(body.get("audio_flow_id"), optional=True)
                or not _valid_uuid(current.get("flow_id"))
                or not _valid_uuid(current.get("audio_flow_id"), optional=True)):
            raise ApplyError(f"{source_id}: malformed or inconsistent bindings")
        result.append({"source": source_id, "path": expected_path, "current": current, "desired": desired})
        requested_ids.append(source_id)
    expected_ids = sorted(entry["id"] for entry in source_entries if entry["status"] == "rebind")
    if sorted(requested_ids) != expected_ids or len(requested_ids) != len(set(requested_ids)):
        raise ApplyError("plan requests do not exactly cover its rebind sources")
    return result


def _sources_by_id(snapshot) -> dict[str, dict]:
    if not isinstance(snapshot, list):
        raise ApplyError("Core sources response is not a list")
    result = {}
    for source in snapshot:
        if not isinstance(source, dict) or not isinstance(source.get("id"), str):
            raise ApplyError("Core sources response is malformed")
        result[source["id"]] = source
    return result


def _dependency_edges(requests: list[dict], live: dict[str, dict]) -> set[tuple[str, str]]:
    owners = {_binding(source)["flow_id"]: source_id for source_id, source in live.items()}
    requested = {request["source"] for request in requests}
    edges = set()
    for request in requests:
        owner = owners.get(request["desired"]["flow_id"])
        if owner and owner != request["source"] and owner in requested:
            edges.add((owner, request["source"]))
    return edges


def preflight(plan: dict, state: dict, sources) -> dict:
    """Validate a plan against live state and return its safe execution order."""

    requests = _normalise_plan(plan)
    if (not isinstance(state, dict) or not isinstance(state.get("revision"), int)
            or not isinstance(state.get("program"), str) or not isinstance(state.get("preview"), str)):
        raise ApplyError("Core state response is malformed")
    live = _sources_by_id(sources)
    for request in requests:
        source = live.get(request["source"])
        if source is None:
            raise ApplyError(f"{request['source']}: stable source disappeared")
        if _binding(source) != request["current"]:
            raise ApplyError(f"{request['source']}: live binding changed after the plan was created")

    edges = _dependency_edges(requests, live)
    program = state.get("program")
    preview = state.get("preview")
    if edges:
        ordered = requests
        positions = {request["source"]: index for index, request in enumerate(ordered)}
        if any(positions[before] >= positions[after] for before, after in edges):
            raise ApplyError("planner request order violates a live flow dependency")
        if program in positions and positions[program] != len(ordered) - 1:
            raise ApplyError("dependent rebind would change Program before all other sources")
        strategy = "planner_dependency_order"
    else:
        indexed = list(enumerate(requests))
        ordered = [item for _, item in sorted(
            indexed,
            key=lambda pair: (0 if pair[1]["source"] == preview else
                              2 if pair[1]["source"] == program else 1, pair[0]))]
        strategy = "preview_off_air_program"
    return {
        "revision": state["revision"],
        "program": program,
        "preview": preview,
        "strategy": strategy,
        "requests": ordered,
    }


def execute(base: str, prepared: dict, expected_revision: int) -> dict:
    """Apply and verify prepared requests. Roll back completed work on failure."""

    state = _request_json(base, STATE_PATH)
    if state.get("revision") != expected_revision or prepared["revision"] != expected_revision:
        raise ApplyError("Core revision changed after preflight")
    completed = []
    receipts = []
    try:
        for request in prepared["requests"]:
            receipt = _request_json(base, request["path"], method="PUT", body=request["desired"])
            if receipt.get("changed") is not True:
                raise ApplyError(f"{request['source']}: Core did not report a changed binding")
            completed.append(request)
            expected_receipt_revision = expected_revision + len(completed)
            if receipt.get("revision") != expected_receipt_revision:
                raise ApplyError(f"{request['source']}: concurrent Core revision change detected")
            live = _sources_by_id(_request_json(base, SOURCES_PATH)).get(request["source"])
            if live is None or _binding(live) != request["desired"]:
                raise ApplyError(f"{request['source']}: post-apply binding verification failed")
            live_state = _request_json(base, STATE_PATH)
            if (live_state.get("revision") != expected_receipt_revision
                    or live_state.get("program") != prepared["program"]
                    or live_state.get("preview") != prepared["preview"]):
                raise ApplyError(f"{request['source']}: Core state changed during apply")
            receipts.append({"source": request["source"], "receipt": receipt})
        final_state = _request_json(base, STATE_PATH)
        expected_final_revision = expected_revision + len(prepared["requests"])
        if final_state.get("revision") != expected_final_revision:
            raise ApplyError("Core revision changed before final receipt")
    except ApplyError as apply_error:
        rollback_errors = []
        for request in reversed(completed):
            try:
                _request_json(base, request["path"], method="PUT", body=request["current"])
                live = _sources_by_id(_request_json(base, SOURCES_PATH)).get(request["source"])
                if live is None or _binding(live) != request["current"]:
                    raise ApplyError("rollback verification failed")
            except ApplyError as rollback_error:
                rollback_errors.append(f"{request['source']}: {rollback_error}")
        suffix = "" if not rollback_errors else "; rollback errors: " + "; ".join(rollback_errors)
        raise ApplyError(str(apply_error) + suffix) from None
    return {
        "applied": True,
        "starting_revision": expected_revision,
        "final_revision": final_state.get("revision"),
        "program": final_state.get("program"),
        "preview": final_state.get("preview"),
        "strategy": prepared["strategy"],
        "receipts": receipts,
    }


def render(value: dict) -> str:
    return json.dumps(value, sort_keys=True, indent=2) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Preflight or explicitly apply a saved MXL rebind plan.")
    parser.add_argument("--plan", required=True, help="saved JSON output from mxl_discovery_planner.py")
    parser.add_argument("--core-url", required=True, help="loopback Core base URL")
    parser.add_argument("--apply", action="store_true", help="apply after a successful preflight")
    parser.add_argument("--expect-revision", type=int, help="required for --apply; must equal live revision")
    parser.add_argument("--confirm", help=f"required for --apply; literal {CONFIRMATION}")
    parser.add_argument("--output", help="write the receipt here instead of stdout")
    args = parser.parse_args(argv)
    try:
        base = _loopback_base(args.core_url)
        plan = _read_json(args.plan)
        state = _request_json(base, STATE_PATH)
        sources = _request_json(base, SOURCES_PATH)
        prepared = preflight(plan, state, sources)
        if args.apply:
            if args.expect_revision is None or args.confirm != CONFIRMATION:
                raise ApplyError(f"--apply requires --expect-revision and --confirm {CONFIRMATION}")
            result = execute(base, prepared, args.expect_revision)
        else:
            result = {
                "applied": False,
                "dry_run": True,
                "revision": prepared["revision"],
                "program": prepared["program"],
                "preview": prepared["preview"],
                "strategy": prepared["strategy"],
                "order": [request["source"] for request in prepared["requests"]],
                "apply_requirements": {
                    "expect_revision": prepared["revision"],
                    "confirm": CONFIRMATION,
                },
            }
        text = render(result)
        if args.output:
            pathlib.Path(args.output).write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
        return 0
    except (ApplyError, OSError, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
