#!/usr/bin/env python3
"""Read-only EasyMXL discovery planner for MXL Switcher Core source bindings.

EasyMXL owns MXL flow discovery and writer lifecycle; Core owns the
authoritative Program/Preview state. This tool joins the two without changing
either: it reads an EasyMXL flow inventory (``GET /api/domains/{domain}/flows``)
and a Core source snapshot (``GET /api/mxl/v1/sources``), applies a declarative
stable-source mapping, and prints a deterministic JSON reconciliation plan.

The plan may *propose* ``PUT /api/mxl/v1/sources/{id}`` requests. Nothing is
ever sent: there is no apply mode. Applying a plan is a separate, explicitly
enabled phase.

Matching never uses labels. A flow belongs to a stable source only when its
writer container name, NMOS group, role, media format, operating point, and
rate all match the mapping, and only active flows are bound by default.
Anything the rules cannot decide uniquely is reported, never guessed.

Every status a source can receive:

- ``noop``: Core already holds the desired binding;
- ``rebind``: one stable source whose flow UUIDs changed; a request is proposed;
- ``create_required``: the stable source is not registered in Core;
- ``missing``: no active flow matches (or required audio is absent);
- ``ambiguous``: more than one candidate, or the binding would conflict with
  another source; an operator must decide;
- ``incompatible``: matching flows exist but their format does not match.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from fractions import Fraction

PLAN_VERSION = 1
STATUSES = ("noop", "rebind", "create_required", "missing", "ambiguous", "incompatible")
ACTIONABLE = ("noop", "rebind")
SOURCES_PATH = "/api/mxl/v1/sources"


class PlanInputError(ValueError):
    """The mapping or an input document is malformed; no plan is produced."""


# --- input normalisation ----------------------------------------------------


def _rate(value) -> Fraction | None:
    """Parse ``"30/1"``, ``30``, or ``{"numerator": 30, "denominator": 1}``."""

    if value is None:
        return None
    try:
        if isinstance(value, dict):
            return Fraction(int(value["numerator"]), int(value.get("denominator", 1)))
        if isinstance(value, str) and "/" in value:
            numerator, denominator = value.split("/", 1)
            return Fraction(int(numerator), int(denominator))
        return Fraction(value)
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
        raise PlanInputError(f"invalid rate {value!r}") from error


def _rate_text(rate: Fraction | None) -> str | None:
    return None if rate is None else f"{rate.numerator}/{rate.denominator}"


def _writer_name(flow: dict) -> str | None:
    writer = flow.get("writerContainer")
    if isinstance(writer, dict) and isinstance(writer.get("name"), str):
        return writer["name"].lstrip("/")
    return None


def _video_traits(flow: dict) -> dict:
    definition = flow.get("def") or {}
    return {
        "width": definition.get("frame_width"),
        "height": definition.get("frame_height"),
        "rate": _rate_text(_rate(definition.get("grain_rate"))),
        "media_type": definition.get("media_type") or flow.get("mediaType") or None,
    }


def _audio_traits(flow: dict) -> dict:
    definition = flow.get("def") or {}
    return {
        "sample_rate": _rate_text(_rate(definition.get("sample_rate"))),
        "channels": definition.get("channel_count"),
        "media_type": definition.get("media_type") or flow.get("mediaType") or None,
    }


def _is_active(flow: dict) -> bool:
    return flow.get("active") is True and flow.get("status") == "active"


def _flow_state(flow: dict) -> str:
    if _is_active(flow):
        return "active"
    status = flow.get("status")
    return status if status in ("inactive", "stale") else "unknown"


def normalise_mapping(mapping: dict) -> dict:
    """Validate the declarative mapping and fill defaults. Raises PlanInputError."""

    if not isinstance(mapping, dict) or mapping.get("version") != 1:
        raise PlanInputError("mapping must be an object with version 1")
    defaults = mapping.get("defaults") or {}
    sources = mapping.get("sources")
    if not isinstance(sources, list) or not sources:
        raise PlanInputError("mapping.sources must be a non-empty list")
    seen_ids: set[str] = set()
    seen_rules: dict[tuple, str] = {}
    result = []
    for entry in sources:
        if not isinstance(entry, dict):
            raise PlanInputError("each mapping source must be an object")
        source_id = entry.get("id")
        match = entry.get("match") or {}
        if not isinstance(source_id, str) or not source_id or "/" in source_id:
            raise PlanInputError(f"invalid stable source id {source_id!r}")
        if source_id in seen_ids:
            raise PlanInputError(f"duplicate stable source id {source_id}")
        seen_ids.add(source_id)
        for field in ("writer_container", "group", "video_role"):
            if not isinstance(match.get(field), str) or not match[field]:
                raise PlanInputError(f"{source_id}: match.{field} is required (labels are never used)")
        audio_role = match.get("audio_role")
        if audio_role is not None and (not isinstance(audio_role, str) or not audio_role):
            raise PlanInputError(f"{source_id}: match.audio_role must be a non-empty string when present")
        rule = (match["writer_container"], match["group"], match["video_role"])
        if rule in seen_rules:
            raise PlanInputError(f"{source_id} and {seen_rules[rule]} use the same match rule")
        seen_rules[rule] = source_id

        video = {**(defaults.get("video") or {}), **(entry.get("video") or {})}
        audio = {**(defaults.get("audio") or {}), **(entry.get("audio") or {})}
        for key in ("width", "height", "rate", "media_type"):
            if video.get(key) in (None, ""):
                raise PlanInputError(f"{source_id}: video.{key} is required (set it per source or in defaults)")
        audio_required = bool(audio.get("required", True)) if audio_role else False
        result.append({
            "id": source_id,
            "writer_container": match["writer_container"],
            "group": match["group"],
            "video_role": match["video_role"],
            "audio_role": audio_role,
            "audio_required": audio_required,
            "video": {
                "width": int(video["width"]),
                "height": int(video["height"]),
                "rate": _rate_text(_rate(video["rate"])),
                "media_type": video["media_type"],
            },
            "audio": {
                "sample_rate": _rate_text(_rate(audio.get("sample_rate", 48000))),
                "channels": int(audio.get("channels", 2)),
                "media_type": audio.get("media_type", "audio/float32"),
            },
        })
    return {"domain": mapping.get("domain"), "sources": sorted(result, key=lambda s: s["id"])}


def normalise_inventory(inventory) -> list[dict]:
    """Accept EasyMXL's ``Flow[]`` (or ``{"flows": [...]}`` from the domain route)."""

    flows = inventory.get("flows") if isinstance(inventory, dict) else inventory
    if not isinstance(flows, list):
        raise PlanInputError("inventory must be EasyMXL's flow list")
    for flow in flows:
        if not isinstance(flow, dict) or not isinstance(flow.get("id"), str):
            raise PlanInputError("every inventory flow needs a string id")
    return flows


def normalise_core(snapshot) -> dict[str, dict]:
    """Accept Core's ``GET /api/mxl/v1/sources`` list (or ``{"sources": [...]}``)."""

    sources = snapshot.get("sources") if isinstance(snapshot, dict) else snapshot
    if not isinstance(sources, list):
        raise PlanInputError("core snapshot must be Core's source list")
    by_id = {}
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("id"), str):
            raise PlanInputError("every Core source needs a string id")
        by_id[source["id"]] = source
    return by_id


# --- planning ---------------------------------------------------------------


def _candidates(flows, rule: dict, kind: str, role: str) -> list[dict]:
    return [
        flow for flow in flows
        if flow.get("format") == kind
        and _writer_name(flow) == rule["writer_container"]
        and flow.get("group") == rule["group"]
        and flow.get("role") == role
    ]


def _video_mismatch(traits: dict, expected: dict) -> list[str]:
    return sorted(f"{key}={traits[key]!r} expected {expected[key]!r}"
                  for key in ("width", "height", "rate", "media_type") if traits[key] != expected[key])


def _audio_mismatch(traits: dict, expected: dict) -> list[str]:
    return sorted(f"{key}={traits[key]!r} expected {expected[key]!r}"
                  for key in ("sample_rate", "channels", "media_type") if traits[key] != expected[key])


def _core_video_traits(core_source: dict) -> dict | None:
    fmt = core_source.get("format")
    if not isinstance(fmt, dict):
        return None
    return {
        "width": fmt.get("width"),
        "height": fmt.get("height"),
        "rate": _rate_text(_rate({"numerator": fmt.get("rate_numerator"), "denominator": fmt.get("rate_denominator", 1)}))
        if fmt.get("rate_numerator") else None,
        "media_type": fmt.get("encoding"),
    }


def _summarise(flow: dict, traits: dict) -> dict:
    return {"flow_id": flow["id"].lower(), "state": _flow_state(flow), **traits}


def _select(flows, rule, kind, role, traits_of, mismatch_of, expected):
    """Return (chosen_flow | None, outcome, details) for one role of one source."""

    found = _candidates(flows, rule, kind, role)
    active = [flow for flow in found if _is_active(flow)]
    details = {
        "not_active": sorted((_summarise(flow, traits_of(flow)) for flow in found if not _is_active(flow)),
                             key=lambda item: item["flow_id"]),
        "incompatible": [],
        "compatible": [],
    }
    for flow in active:
        traits = traits_of(flow)
        problems = mismatch_of(traits, expected)
        if problems:
            details["incompatible"].append({**_summarise(flow, traits), "reasons": problems})
        else:
            details["compatible"].append(_summarise(flow, traits))
    for key in ("incompatible", "compatible"):
        details[key].sort(key=lambda item: item["flow_id"])
    if len(details["compatible"]) == 1:
        chosen = next(flow for flow in active if flow["id"].lower() == details["compatible"][0]["flow_id"])
        return chosen, "matched", details
    if len(details["compatible"]) > 1:
        return None, "ambiguous", details
    if details["incompatible"]:
        return None, "incompatible", details
    return None, "missing", details


def plan(inventory, mapping: dict, core_snapshot, domain: str | None = None) -> dict:
    """Build the reconciliation plan. Pure: no I/O, no clock, deterministic."""

    rules = normalise_mapping(mapping)
    flows = normalise_inventory(inventory)
    core = normalise_core(core_snapshot)
    entries = []

    for rule in rules["sources"]:
        reasons: list[str] = []
        video, video_outcome, video_details = _select(
            flows, rule, "video", rule["video_role"], _video_traits, _video_mismatch, rule["video"])
        audio, audio_outcome, audio_details = (None, "not_configured", None)
        if rule["audio_role"]:
            audio, audio_outcome, audio_details = _select(
                flows, rule, "audio", rule["audio_role"], _audio_traits, _audio_mismatch, rule["audio"])

        status = None
        if video_outcome != "matched":
            status = video_outcome
            reasons.append(f"video_{video_outcome}")
        if rule["audio_role"] and audio_outcome != "matched":
            if audio_outcome == "missing" and not rule["audio_required"]:
                reasons.append("audio_missing_optional")
            else:
                reasons.append(f"audio_{audio_outcome}")
                status = status or audio_outcome

        current = core.get(rule["id"])
        current_binding = None if current is None else {
            "flow_id": (current.get("flow_id") or "").lower() or None,
            "audio_flow_id": (current.get("audio_flow_id") or "").lower() or None,
        }
        desired = None
        if status is None:
            desired = {"flow_id": video["id"].lower(), "audio_flow_id": audio["id"].lower() if audio else None}
            if current is None:
                status = "create_required"
                reasons.append("stable_source_not_registered_in_core")
            else:
                core_traits = _core_video_traits(current)
                if core_traits is not None and _video_mismatch(_video_traits(video), core_traits):
                    status = "incompatible"
                    reasons.append("video_differs_from_core_operating_point")
                elif desired == current_binding:
                    status = "noop"
                else:
                    status = "rebind"
        entries.append({
            "id": rule["id"],
            "status": status,
            "reasons": sorted(reasons),
            "match": {key: rule[key] for key in ("writer_container", "group", "video_role", "audio_role")},
            "current": current_binding,
            "desired": desired,
            "video": video_details,
            "audio": audio_details,
            "request": None,
        })

    _reject_conflicts(entries, core)
    requests = _order_requests(entries, core)

    diagnostics = _unclaimed(flows, rules, entries)
    summary = {status: sum(1 for entry in entries if entry["status"] == status) for status in STATUSES}
    return {
        "planner": "mxl-discovery-planner",
        "plan_version": PLAN_VERSION,
        "read_only": True,
        "domain": domain if domain is not None else rules["domain"],
        "summary": summary,
        "blocked": any(entry["status"] not in ACTIONABLE for entry in entries),
        "sources": entries,
        "requests": requests,
        "diagnostics": diagnostics,
    }


def _demote(entry: dict, reason: str) -> None:
    entry["status"] = "ambiguous"
    entry["reasons"] = sorted(set(entry["reasons"]) | {reason})
    entry["request"] = None


def _reject_conflicts(entries: list[dict], core: dict[str, dict]) -> None:
    """Never assign one video flow to two stable sources; never steal a bound flow."""

    by_flow: dict[str, list[dict]] = {}
    for entry in entries:
        if entry["desired"]:
            by_flow.setdefault(entry["desired"]["flow_id"], []).append(entry)
    for flow_id, claimants in by_flow.items():
        if len(claimants) > 1:
            for entry in claimants:
                _demote(entry, "duplicate_video_assignment")
    planned = {entry["id"]: entry for entry in entries}
    for entry in entries:
        if entry["status"] not in ("rebind", "create_required"):
            continue
        flow_id = entry["desired"]["flow_id"]
        for other_id, other in core.items():
            if other_id == entry["id"] or (other.get("flow_id") or "").lower() != flow_id:
                continue
            other_plan = planned.get(other_id)
            moving_away = (other_plan is not None and other_plan["status"] == "rebind"
                           and other_plan["desired"]["flow_id"] != flow_id)
            if not moving_away:
                _demote(entry, f"video_flow_bound_to_other_source:{other_id}")


def _order_requests(entries: list[dict], core: dict[str, dict]) -> list[dict]:
    """Order proposed rebinds so a flow is released before it is claimed; refuse cycles."""

    rebinds = {entry["id"]: entry for entry in entries if entry["status"] == "rebind"}
    current_owner = {(source.get("flow_id") or "").lower(): source_id for source_id, source in core.items()}
    depends = {
        source_id: {current_owner[entry["desired"]["flow_id"]]}
        & (set(rebinds) - {source_id}) if entry["desired"]["flow_id"] in current_owner else set()
        for source_id, entry in rebinds.items()
    }
    ordered, done = [], set()
    while len(done) < len(rebinds):
        ready = sorted(source_id for source_id in rebinds if source_id not in done and depends[source_id] <= done)
        if not ready:
            for source_id in sorted(set(rebinds) - done):
                _demote(rebinds[source_id], "rebinding_cycle_requires_operator")
            break
        for source_id in ready:
            done.add(source_id)
            ordered.append(source_id)
    requests = []
    for source_id in ordered:
        entry = rebinds[source_id]
        if entry["status"] != "rebind":
            continue
        request = {
            "method": "PUT",
            "path": f"{SOURCES_PATH}/{urllib.parse.quote(source_id, safe='')}",
            "body": {"flow_id": entry["desired"]["flow_id"], "audio_flow_id": entry["desired"]["audio_flow_id"]},
        }
        entry["request"] = request
        requests.append({"source": source_id, **request})
    return requests


def _unclaimed(flows, rules, entries) -> list[dict]:
    """Report active flows from mapped writers that no source claimed."""

    writers = {rule["writer_container"] for rule in rules["sources"]}
    claimed = set()
    for entry in entries:
        if entry["desired"]:
            claimed.add(entry["desired"]["flow_id"])
            if entry["desired"]["audio_flow_id"]:
                claimed.add(entry["desired"]["audio_flow_id"])
    diagnostics = []
    for flow in flows:
        flow_id = flow["id"].lower()
        if _is_active(flow) and _writer_name(flow) in writers and flow_id not in claimed:
            diagnostics.append({
                "kind": "unclaimed_active_flow",
                "flow_id": flow_id,
                "writer_container": _writer_name(flow),
                "group": flow.get("group") or "",
                "role": flow.get("role") or "",
                "format": flow.get("format") or "unknown",
            })
        elif flow.get("active") is True and _writer_name(flow) is None:
            diagnostics.append({
                "kind": "active_flow_without_writer_attribution",
                "flow_id": flow_id,
                "writer_container": None,
                "group": flow.get("group") or "",
                "role": flow.get("role") or "",
                "format": flow.get("format") or "unknown",
            })
    return sorted(diagnostics, key=lambda item: (item["kind"], item["flow_id"]))


def render(result: dict) -> str:
    """Byte-stable JSON for identical inputs."""

    return json.dumps(result, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


# --- read-only I/O ------------------------------------------------------------


def _read_json_file(path: str):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _http_get_json(url: str, token: str | None, timeout: float = 10.0):
    """GET only. The token travels in a header and is never logged or returned."""

    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise PlanInputError(f"GET {_redact(url)} failed with HTTP {error.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise PlanInputError(f"GET {_redact(url)} failed: {type(error).__name__}") from None


def _redact(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.urlencode([(key, "REDACTED" if key.lower() == "token" else value)
                                    for key, value in urllib.parse.parse_qsl(parts.query)])
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc.split("@")[-1], parts.path, query, ""))


def _token(args) -> str | None:
    if args.token is not None:
        return args.token
    return os.environ.get(args.token_env) or None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Print a read-only reconciliation plan from EasyMXL discovery to Core source bindings.")
    parser.add_argument("--mapping", required=True, help="declarative stable-source mapping (JSON)")
    inventory = parser.add_mutually_exclusive_group(required=True)
    inventory.add_argument("--inventory", help="saved EasyMXL GET /api/domains/{domain}/flows response")
    inventory.add_argument("--easy-mxl-url", help="EasyMXL base URL; requires --domain (read-only GET)")
    core = parser.add_mutually_exclusive_group(required=True)
    core.add_argument("--core-snapshot", help="saved Core GET /api/mxl/v1/sources response")
    core.add_argument("--core-url", help="Core control base URL (read-only GET)")
    parser.add_argument("--domain", help="EasyMXL domain name")
    parser.add_argument("--token-env", default="EASY_MXL_TOKEN",
                        help="environment variable holding the EasyMXL token (default EASY_MXL_TOKEN)")
    parser.add_argument("--token", help="EasyMXL token at runtime (prefer --token-env; never stored)")
    parser.add_argument("--output", help="write the plan here instead of stdout")
    args = parser.parse_args(argv)

    try:
        mapping = _read_json_file(args.mapping)
        if args.inventory:
            flows = _read_json_file(args.inventory)
        else:
            if not args.domain:
                raise PlanInputError("--easy-mxl-url requires --domain")
            url = (f"{args.easy_mxl_url.rstrip('/')}/api/domains/"
                   f"{urllib.parse.quote(args.domain, safe='')}/flows")
            flows = _http_get_json(url, _token(args))
        snapshot = (_read_json_file(args.core_snapshot) if args.core_snapshot
                    else _http_get_json(args.core_url.rstrip("/") + SOURCES_PATH, None))
        result = plan(flows, mapping, snapshot, domain=args.domain)
    except (PlanInputError, OSError, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2
    text = render(result)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text)
    else:
        sys.stdout.write(text)
    return 1 if result["blocked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
