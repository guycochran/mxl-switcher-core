#!/usr/bin/env python3
"""Bounded MXL flow-lifecycle supervisor for stable Core source bindings.

When a writer recreates its flows, for example a ZoomISO output switched
away from MXL and back, the stable Core source must follow the new video/audio
pair. The real-ZoomISO rotation gate proved the primitives one shot at a time.
This tool runs the same primitives continuously. It adds no new matching and
no new write behaviour:

- matching is ``mxl_discovery_planner.plan()``: writer attribution, NMOS
  group, and video/audio roles, never labels;
- writing is ``mxl_rebind_apply.preflight()`` and ``execute()``: the live
  revision gate, single-owner checks, verification, and reverse rollback.

Lifecycle policy stays out of Core. Per stable source:

``steady`` (the bound pair is the one active match) → ``outage`` (that pair
is inactive and nothing replaces it) → ``dwelling`` (exactly one replacement
pair, from the same writer container, has stayed uniquely active; a change
resets the timer and counts a flap) → ``planned`` (observe mode) or
``applied`` (apply mode) → ``steady``.

``refused`` records the reason and never writes. ``uncertain`` means the
journal shows a write whose outcome cannot be proven; that source stays
read-only until ``clear`` is run.

Default mode observes and plans only. Writing requires ``--apply`` **and**
``--confirm APPLY_REBINDS``; there is no other write path. The journal holds
only the logical source, bindings, revision, state, timestamps, and result.
On restart it is reconciled with live Core before anything else, and never
replayed.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mxl_discovery_planner as planner  # noqa: E402
import mxl_rebind_apply as executor  # noqa: E402

CONFIRMATION = executor.CONFIRMATION
ON_AIR = "on_air_program_rebind_not_permitted"
LATCHED = ("apply_failed_rolled_back",)
JOURNAL_VERSION = 1
DEFAULTS = {"interval_s": 2.0, "dwell_s": 5.0, "timeout_s": 5.0, "max_backoff_s": 60.0, "journal_limit": 200,
            "policy": {"allow_program_rebind": False}}


class ConfigError(ValueError):
    """The configuration is unusable; nothing starts."""


def load_config(path: str) -> dict:
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError) as error:
        raise ConfigError(f"config: {error}") from None
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ConfigError("config must be an object with version 1")
    config = {**DEFAULTS, **{k: v for k, v in raw.items() if k != "policy"},
              "policy": {**DEFAULTS["policy"], **(raw.get("policy") or {})}}
    for key in ("interval_s", "dwell_s", "timeout_s", "max_backoff_s"):
        if not isinstance(config[key], (int, float)) or isinstance(config[key], bool) or config[key] <= 0:
            raise ConfigError(f"{key} must be a positive number")
    if config["max_backoff_s"] < config["interval_s"]:
        raise ConfigError("max_backoff_s must be at least interval_s")
    easy = config.get("easy_mxl") or {}
    for key in ("url", "domain"):
        if not isinstance(easy.get(key), str) or not easy[key]:
            raise ConfigError(f"easy_mxl.{key} is required")
    try:
        config["core_base"] = executor._loopback_base(str(config.get("core_url", "")))
    except executor.ApplyError as error:
        raise ConfigError(str(error)) from None
    try:
        config["rules"] = planner.normalise_mapping(config.get("mapping"))["sources"]
    except planner.PlanInputError as error:
        raise ConfigError(f"mapping: {error}") from None
    for key in ("journal", "status"):
        if not isinstance(config.get(key), str) or not config[key]:
            raise ConfigError(f"{key} path is required")
    return config


def atomic_write(path: str, value) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.")
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)


class Journal:
    """Append-only (bounded) record of write intents and outcomes. Never holds credentials."""

    FIELDS = ("t", "source", "state", "previous", "desired", "revision", "result")

    def __init__(self, path: str, limit: int) -> None:
        self.path, self.limit = path, limit
        try:
            data = json.loads(Path(path).read_text())
            self.entries = list(data.get("entries", [])) if data.get("version") == JOURNAL_VERSION else []
        except (OSError, ValueError):
            self.entries = []

    def record(self, **entry) -> None:
        self.entries.append({key: entry.get(key) for key in self.FIELDS})
        if len(self.entries) > self.limit:
            # Drop the oldest entries first, but never a source's newest one: restart
            # reconciliation depends on it.
            newest = {id(e) for e in {e["source"]: e for e in self.entries}.values()}
            excess = len(self.entries) - self.limit
            kept = []
            for entry in self.entries:
                if excess > 0 and id(entry) not in newest:
                    excess -= 1
                    continue
                kept.append(entry)
            self.entries = kept
        atomic_write(self.path, {"version": JOURNAL_VERSION, "entries": self.entries})

    def last(self, source: str) -> dict | None:
        return next((e for e in reversed(self.entries) if e["source"] == source), None)


def writer_of(flows, flow_id):
    for flow in flows:
        if flow.get("id", "").lower() == (flow_id or "").lower():
            writer = flow.get("writerContainer")
            return writer.get("id") if isinstance(writer, dict) else None
    return None


def is_active(flows, flow_id) -> bool:
    return any(flow.get("id", "").lower() == (flow_id or "").lower() and planner._is_active(flow) for flow in flows)


class SourceState:
    def __init__(self, source_id: str) -> None:
        self.id = source_id
        self.state = "starting"
        self.writer_id: str | None = None
        self.candidate: dict | None = None
        self.since: float | None = None
        self.program_at_dwell: str | None = None
        self.refusal: str | None = None
        self.last_rebind: dict | None = None
        self.counters = {"flaps": 0, "refusals": 0, "applies": 0, "apply_failures": 0, "plans": 0}

    def view(self, now: float, dwell: float) -> dict:
        remaining = None
        if self.state == "dwelling" and self.since is not None:
            remaining = round(max(0.0, dwell - (now - self.since)), 3)
        return {"state": self.state, "pending_dwell_s": remaining, "candidate": self.candidate,
                "refusal": self.refusal, "last_accepted_rebind": self.last_rebind,
                "writer_known": self.writer_id is not None, "counters": dict(self.counters)}


class Supervisor:
    def __init__(self, config: dict, apply: bool, inventory_get=None, clock=time.time, sleep=time.sleep,
                 log=lambda line: print(line, file=sys.stderr, flush=True)) -> None:
        self.c = config
        self.apply = apply
        self.inventory_get = inventory_get or self._default_inventory
        self.clock, self.sleep, self.log = clock, sleep, log
        self.journal = Journal(config["journal"], int(config["journal_limit"]))
        self.sources = {rule["id"]: SourceState(rule["id"]) for rule in config["rules"]}
        self.rules = {rule["id"]: rule for rule in config["rules"]}
        self.failures = 0
        self.reconciled = False
        self.stop = False
        self.health = {"last_inventory_ok": None, "last_core_ok": None, "consecutive_failures": 0,
                       "last_error": None, "ticks": 0}

    # I/O ------------------------------------------------------------------------
    def _default_inventory(self):
        easy = self.c["easy_mxl"]
        url = f"{easy['url'].rstrip('/')}/api/domains/{planner.urllib.parse.quote(easy['domain'], safe='')}/flows"
        return planner._http_get_json(url, os.environ.get(easy.get("token_env", "EASY_MXL_TOKEN")) or None,
                                      timeout=self.c["timeout_s"])

    def core(self, path: str):
        return executor._request_json(self.c["core_base"], path, timeout=self.c["timeout_s"])

    # transitions ------------------------------------------------------------------
    def set_state(self, source: SourceState, state: str, refusal: str | None = None,
                  keep_candidate: bool = False) -> None:
        if source.state != state or source.refusal != refusal:
            if state == "refused" and (source.state != "refused" or source.refusal != refusal):
                source.counters["refusals"] += 1
            self.log(json.dumps({"t": round(self.clock(), 3), "source": source.id, "from": source.state,
                                 "to": state, **({"reason": refusal} if refusal else {})}, sort_keys=True))
        source.state, source.refusal = state, refusal
        if state not in ("dwelling", "planned") and not keep_candidate:
            source.candidate, source.since = None, None

    def reconcile(self, core_sources: dict) -> None:
        """Before any action: settle journal entries whose outcome a crash left open."""

        for source_id, source in self.sources.items():
            entry = self.journal.last(source_id)
            if entry is None:
                continue
            live = executor._binding(core_sources[source_id]) if source_id in core_sources else None
            if entry["state"] == "uncertain":
                self.set_state(source, "uncertain", entry.get("result") or "unresolved journal entry")
            elif entry["state"] == "applying":
                if live == entry["desired"]:
                    outcome = "applied"
                elif live == entry["previous"]:
                    outcome = "not_applied"
                else:
                    outcome = "uncertain"
                self.journal.record(t=self.clock(), source=source_id, state=outcome if outcome != "not_applied"
                                    else "failed", previous=entry["previous"], desired=entry["desired"],
                                    revision=entry["revision"], result=f"reconciled after restart: {outcome}")
                if outcome == "uncertain":
                    self.set_state(source, "uncertain", "restart found a binding that is neither previous nor desired")
                elif outcome == "applied":
                    source.last_rebind = {"t": entry["t"], "revision": entry["revision"]}
            elif entry["state"] == "applied":
                source.last_rebind = {"t": entry["t"], "revision": entry["revision"]}
        self.reconciled = True

    # one poll ---------------------------------------------------------------------
    def tick(self) -> None:
        self.health["ticks"] += 1
        try:
            inventory = planner.normalise_inventory(self.inventory_get())
            self.health["last_inventory_ok"] = round(self.clock(), 3)
            state = self.core(executor.STATE_PATH)
            sources_list = self.core(executor.SOURCES_PATH)
            core_sources = executor._sources_by_id(sources_list)
            self.health["last_core_ok"] = round(self.clock(), 3)
        except (planner.PlanInputError, executor.ApplyError) as error:
            self.failures += 1
            self.health.update(consecutive_failures=self.failures, last_error=type(error).__name__)
            return
        self.failures = 0
        self.health.update(consecutive_failures=0, last_error=None)
        if not self.reconciled:
            self.reconcile(core_sources)
        for source_id, source in self.sources.items():
            try:
                self.evaluate(source, inventory, state, sources_list, core_sources)
            except (planner.PlanInputError, executor.ApplyError, KeyError, TypeError, ValueError) as error:
                self.set_state(source, "refused", f"evaluation error: {type(error).__name__}")

    def evaluate(self, source: SourceState, flows: list, state: dict, sources_list: list, core_sources: dict) -> None:
        if source.state == "uncertain":
            return
        rule = self.rules[source.id]
        group_flows = [f for f in flows if f.get("group") == rule["group"]
                       and f.get("role") in (rule["video_role"], rule["audio_role"])]
        if any(f.get("active") is None for f in group_flows):
            return self.set_state(source, "refused", "unknown_writer_state")
        if any(f.get("active") is True and not planner._writer_name(f) for f in group_flows):
            return self.set_state(source, "refused", "missing_writer_attribution")
        mapping = {"version": 1, "sources": [self._mapping_entry(rule)]}
        plan_doc = planner.plan(flows, mapping, sources_list)
        entry = plan_doc["sources"][0]
        status, current, desired = entry["status"], entry["current"], entry["desired"]

        if status == "noop":
            ids = writer_of(flows, desired["flow_id"]), writer_of(flows, desired["audio_flow_id"])
            if desired["audio_flow_id"] and ids[0] != ids[1]:
                return self.set_state(source, "refused", "split_writer_ownership")
            source.writer_id = ids[0]
            return self.set_state(source, "steady")
        if status == "missing":
            if source.state == "dwelling":
                source.counters["flaps"] += 1
            return self.set_state(source, "outage")
        if status != "rebind":
            return self.set_state(source, "refused", f"{status}:{','.join(entry['reasons'])}")

        # Only the halves being replaced must be inactive. An unchanged active
        # video gaining its audio (completing a pair) is not a replacement.
        replaced = [current[field] for field in ("flow_id", "audio_flow_id")
                    if current and current.get(field) and current[field] != desired[field]]
        if any(is_active(flows, flow_id) for flow_id in replaced):
            return self.set_state(source, "refused", "old_pair_still_active")
        # Learn identity only from the pair Core is already bound to while it
        # is visibly active. Starting during an outage must not let a matching
        # container name stand in for a previously observed container ID.
        if source.writer_id is None and current and is_active(flows, current.get("flow_id")):
            current_ids = (writer_of(flows, current.get("flow_id")),
                           writer_of(flows, current.get("audio_flow_id")))
            if current.get("audio_flow_id") and current_ids[0] != current_ids[1]:
                return self.set_state(source, "refused", "split_writer_ownership")
            source.writer_id = current_ids[0]
        ids = writer_of(flows, desired["flow_id"]), writer_of(flows, desired["audio_flow_id"])
        if desired["audio_flow_id"] and ids[0] != ids[1]:
            return self.set_state(source, "refused", "split_writer_ownership")
        if source.writer_id is None:
            return self.set_state(source, "refused", "writer_not_observed_steady")
        if source.writer_id is not None and ids[0] != source.writer_id:
            return self.set_state(source, "refused", "wrong_writer")

        now = self.clock()
        same_candidate = source.candidate == desired
        if source.state == "refused" and same_candidate and source.refusal in LATCHED:
            return None  # a failed, rolled-back write is never retried for the same candidate
        policy_hold = source.state == "refused" and same_candidate and source.refusal == ON_AIR
        if not policy_hold:
            if source.state not in ("dwelling", "planned") or not same_candidate:
                if source.state in ("dwelling", "planned"):
                    source.counters["flaps"] += 1
                source.candidate, source.since, source.program_at_dwell = desired, now, state.get("program")
                return self.set_state(source, "dwelling")
            if now - source.since < self.c["dwell_s"] or source.state == "planned":
                return None
            if state.get("program") != source.program_at_dwell:
                return self.set_state(source, "refused", "program_changed_during_dwell")
        if state.get("program") == source.id and not self.c["policy"]["allow_program_rebind"]:
            return self.set_state(source, "refused", ON_AIR, keep_candidate=True)  # released when cut away

        prepared = executor.preflight(plan_doc, state, sources_list)
        if not self.apply:
            if source.state != "planned":
                source.counters["plans"] += 1
            source.state = "planned"  # keep the candidate visible for the dashboard
            self.log(json.dumps({"t": round(now, 3), "source": source.id, "to": "planned",
                                 "revision": prepared["revision"]}, sort_keys=True))
            return None
        self.journal.record(t=now, source=source.id, state="applying", previous=current, desired=desired,
                            revision=prepared["revision"], result=None)
        try:
            result = executor.execute(self.c["core_base"], prepared, prepared["revision"])
        except executor.ApplyError as error:
            source.counters["apply_failures"] += 1
            live = executor._binding(executor._sources_by_id(self.core(executor.SOURCES_PATH)).get(source.id) or {})
            outcome = "failed" if live == current else "uncertain"
            self.journal.record(t=self.clock(), source=source.id, state=outcome, previous=current, desired=desired,
                                revision=prepared["revision"], result=str(error)[:300])
            return self.set_state(source, "uncertain" if outcome == "uncertain" else "refused",
                                  "apply_failed_rolled_back" if outcome == "failed" else "apply_outcome_unproven",
                                  keep_candidate=True)
        source.counters["applies"] += 1
        source.writer_id = ids[0]
        source.last_rebind = {"t": round(self.clock(), 3), "revision": result["final_revision"]}
        self.journal.record(t=self.clock(), source=source.id, state="applied", previous=current, desired=desired,
                            revision=result["final_revision"], result="applied")
        return self.set_state(source, "applied")

    @staticmethod
    def _mapping_entry(rule: dict) -> dict:
        return {"id": rule["id"],
                "match": {"writer_container": rule["writer_container"], "group": rule["group"],
                          "video_role": rule["video_role"], "audio_role": rule["audio_role"]},
                "video": rule["video"], "audio": {**rule["audio"], "required": rule["audio_required"]}}

    # status and loop ------------------------------------------------------------------
    def status(self) -> dict:
        now = self.clock()
        return {"supervisor": "mxl-flow-supervisor", "mode": "apply" if self.apply else "observe",
                "state": "stopped" if self.stop else ("backoff" if self.failures else "running"),
                "next_poll_s": self.delay(), **self.health,
                "sources": {sid: s.view(now, self.c["dwell_s"]) for sid, s in sorted(self.sources.items())}}

    def delay(self) -> float:
        if not self.failures:
            return float(self.c["interval_s"])
        return float(min(self.c["max_backoff_s"], self.c["interval_s"] * 2 ** self.failures))

    def run(self, max_ticks: int | None = None) -> int:
        handlers = {}
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                handlers[signum] = signal.signal(signum, lambda *_: setattr(self, "stop", True))
            except ValueError:
                pass
        try:
            ticks = 0
            while not self.stop and (max_ticks is None or ticks < max_ticks):
                self.tick()
                ticks += 1
                atomic_write(self.c["status"], self.status())
                if self.stop or (max_ticks is not None and ticks >= max_ticks):
                    break
                remaining = self.delay()
                while remaining > 0 and not self.stop:  # short slices: a signal is honoured promptly
                    step = min(remaining, 0.5)
                    self.sleep(step)
                    remaining -= step
        finally:
            for signum, handler in handlers.items():
                signal.signal(signum, handler)
            self.stop = True
            atomic_write(self.c["status"], self.status())
        return 0


def main(argv=None, inventory_get=None, clock=time.time, sleep=time.sleep, log=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="observe (default) or, with --apply --confirm, maintain bindings")
    run.add_argument("--config", required=True)
    run.add_argument("--apply", action="store_true")
    run.add_argument("--confirm", help=f"literal {CONFIRMATION}; required with --apply")
    run.add_argument("--max-ticks", type=int, help="stop after N polls (one-shot checks)")
    clear = sub.add_parser("clear", help="mark one source's uncertain journal entry as operator-resolved")
    clear.add_argument("--config", required=True)
    clear.add_argument("--source", required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2
    if args.command == "clear":
        journal = Journal(config["journal"], int(config["journal_limit"]))
        entry = journal.last(args.source)
        if not entry or entry["state"] != "uncertain":
            print(json.dumps({"error": "no uncertain entry for that source"}), file=sys.stderr)
            return 2
        journal.record(t=clock(), source=args.source, state="cleared", previous=entry["previous"],
                       desired=entry["desired"], revision=entry["revision"], result="operator resolved")
        return 0
    if args.command != "run":
        parser.print_help()
        return 2
    if args.apply and args.confirm != CONFIRMATION:
        print(json.dumps({"error": f"--apply requires --confirm {CONFIRMATION}"}), file=sys.stderr)
        return 2
    if args.confirm and not args.apply:
        print(json.dumps({"error": "--confirm is only meaningful with --apply"}), file=sys.stderr)
        return 2
    supervisor = Supervisor(config, apply=args.apply, inventory_get=inventory_get, clock=clock, sleep=sleep,
                            **({"log": log} if log else {}))
    return supervisor.run(args.max_ticks)


if __name__ == "__main__":
    raise SystemExit(main())
