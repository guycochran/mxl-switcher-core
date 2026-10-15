#!/usr/bin/env python3
"""Operator-gated observer for the real-ZoomISO flow-rotation rebind gate.

Changing a ZoomISO output from MXL to another protocol and back keeps the
output's identity but creates a **new** video/audio flow pair
(``docs/REAL_ZOOMISO_FLOW_ROTATION_2026-10-15.md``). This harness proves that
an isolated candidate Core's stable source ID follows that rotation, by
joining the existing pieces rather than adding new decision logic:

- ``mxl_recovery_observer``: the isolated candidate, probes, read-only on-air
  refusals, invariants, evidence, and exact cleanup;
- ``mxl_discovery_planner``: matching by writer attribution plus NMOS group
  and role, never labels, with ``missing``/``ambiguous``/``incompatible``
  refusal statuses;
- ``mxl_rebind_apply``: the guarded executor, with its revision gate,
  ownership checks, verification, and reverse-order rollback.

**The operator** changes the declared output's protocol and changes it back.
The harness never calls ZoomISO, never changes a meeting or mapping, and
never touches a permanent service or the operator Core. It runs no
background daemon. One invocation observes one rotation and then exits.

Without ``--apply`` it only plans (read-only). With ``--apply`` it runs the
gate. Without ``--confirm APPLY_REBINDS`` it stops at the saved plan and the
executor's dry-run receipt, and never writes a binding. With it, the
executor applies the planned rebind against the revision recorded at
baseline.

Phases (each bounded; a missing transition fails):

``start → old_pair → content_motion → baseline → awaiting_old_pair_inactive
→ awaiting_replacement → rebind (rotated) | continuation (same UUIDs) →
post_rebind_recovery → content_resumed → stable_hold → verdict``
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import mxl_discovery_planner as planner  # noqa: E402
import mxl_rebind_apply as executor  # noqa: E402
import mxl_recovery_observer as obs  # noqa: E402

CONFIRMATION = executor.CONFIRMATION
VIDEO = {"width": 1920, "height": 1080, "rate": "30/1", "media_type": "video/v210"}


def mapping_for(a) -> dict:
    """One stable source: the selected ZoomISO output, matched by writer, group, and roles."""

    return {"version": 1, "sources": [{
        "id": a.selected_source,
        "match": {"writer_container": a.writer_container, "group": a.group,
                  "video_role": a.video_role, "audio_role": a.audio_role},
        "video": VIDEO,
        "audio": {"sample_rate": 48000, "channels": a.audio_channels, "media_type": "audio/float32",
                  "required": True},
    }]}


def rule_flows(flows: list, a) -> list:
    return [flow for flow in flows if flow.get("group") == a.group and flow.get("role") in (a.video_role, a.audio_role)]


def check_inventory(flows: list, a) -> None:
    """Refuse what the planner would only report as ``missing``: undeterminable or unattributed writers."""

    for flow in rule_flows(flows, a):
        if flow.get("active") is None:
            raise obs.GateFailure("EasyMXL cannot determine a writer state for the selected group; refusing")
        if flow.get("active") is True and not planner._writer_name(flow):
            raise obs.GateFailure("an active flow in the selected group has no writer attribution; refusing")


def writer_of(flows: list, flow_id: str | None) -> dict | None:
    for flow in flows:
        if flow.get("id", "").lower() == (flow_id or "").lower():
            writer = flow.get("writerContainer")
            return {"id": writer.get("id"), "name": planner._writer_name(flow)} if isinstance(writer, dict) else None
    return None


def is_active(flows: list, flow_id: str | None) -> bool:
    return any(flow.get("id", "").lower() == (flow_id or "").lower() and planner._is_active(flow) for flow in flows)


def selected_entry(plan_doc: dict, source_id: str) -> dict:
    return next(entry for entry in plan_doc["sources"] if entry["id"] == source_id)


def candidate_snapshot(a) -> list:
    """The candidate's sources as its generated config will register them (for the dry run)."""

    fmt = {"width": 1920, "height": 1080, "rate_numerator": 30, "rate_denominator": 1, "encoding": "video/v210"}
    return [{"id": a.selected_source, "flow_id": a.selected_flow, "audio_flow_id": None, "format": fmt},
            {"id": a.control_source, "flow_id": a.control_flow, "audio_flow_id": None, "format": fmt}]


def default_inventory(a):
    url = (f"{a.easy_mxl_url.rstrip('/')}/api/domains/"
           f"{urllib.parse.quote(a.easy_mxl_domain, safe='')}/flows")
    try:
        return planner._http_get_json(url, os.environ.get(a.easy_mxl_token_env) or None)
    except planner.PlanInputError as error:
        raise obs.GateFailure(str(error)) from None


def discover_old_pair(inventory, a, core_sources) -> dict:
    """Pre-interruption identity: the one active pair the mapping matches, and its writer."""

    flows = planner.normalise_inventory(inventory)
    check_inventory(flows, a)
    plan_doc = planner.plan(flows, mapping_for(a), core_sources)
    entry = selected_entry(plan_doc, a.selected_source)
    if entry["status"] not in ("noop", "rebind"):
        raise obs.Refused(f"the selected output is not uniquely discoverable before interruption: "
                          f"{entry['status']} {entry['reasons']}")
    pair = entry["desired"]
    if pair["flow_id"] != a.selected_flow.lower():
        raise obs.Refused("the mapping's active video is not --selected-flow; check the writer/group/role")
    video_writer, audio_writer = writer_of(flows, pair["flow_id"]), writer_of(flows, pair["audio_flow_id"])
    if not video_writer or not audio_writer or video_writer["id"] != audio_writer["id"]:
        raise obs.Refused("the selected video and audio are not owned by one writer container")
    return {"pair": pair, "writer": video_writer, "plan": plan_doc, "status": entry["status"]}


class RotationObserver(obs.Observer):
    def __init__(self, plan, docker, fetch, inventory, confirm: bool, **kwargs) -> None:
        super().__init__(plan, docker, fetch, **kwargs)
        self.inventory = inventory
        self.confirm = confirm
        self.core_base = f"http://127.0.0.1:{plan.a.host_port}"
        self.facts: dict = {"path": None, "applied": False}

    # helpers
    def core_sources(self) -> list:
        return obs.fetch_json(self.fetch, self.plan.core_url + "/sources")

    def core_state(self) -> dict:
        return obs.fetch_json(self.fetch, self.plan.core_url + "/state")

    def motion(self, flow: str, label: str) -> dict:
        a = self.plan.a
        command = self.plan._python(f"{self.plan.run_id}-motion", "motion", "--domain", obs.DOMAIN_MOUNT,
                                    "--flow", flow, "--seconds", repr(a.motion_seconds), detach=False)
        status, out, error = self.docker(command)
        result = obs.last_json_line(out) or {"error": (error or "no report").strip()[-200:]}
        self.event(f"motion_{label}", change_fps=result.get("change_fps"))
        self.facts.setdefault("motion", {})[label] = result
        if status != 0 or result.get("error") or result.get("change_fps", 0) < a.min_change_fps:
            raise obs.GateFailure(f"content_motion: {label} is not changing content "
                                  f"({result.get('change_fps')} changes/s < {a.min_change_fps}); "
                                  "a meeting reset or unassigned output can keep cadence with a static picture")
        return result

    def apply(self, plan_doc: dict, revision: int, step: str) -> dict:
        state, sources = self.core_state(), self.core_sources()
        prepared = executor.preflight(plan_doc, state, sources)
        receipt_path = Path(self.plan.evidence_dir) / f"{step}-preflight.json"
        receipt_path.write_text(executor.render({k: v for k, v in prepared.items()}))
        if not self.confirm:
            raise obs.GateFailure(f"{step}: planned only; --confirm {CONFIRMATION} was not given, nothing written")
        try:
            result = executor.execute(self.core_base, prepared, revision)
        except executor.ApplyError as error:
            restored = {s["id"]: executor._binding(s) for s in self.core_sources()}
            self.facts[f"{step}_rollback"] = {"error": str(error), "bindings_after": restored}
            raise obs.GateFailure(f"{step}: {error}") from None
        self.write(f"{step}-receipt.json", result)
        return result

    # phases
    def record_old_pair(self) -> None:
        a = self.plan.a
        found = discover_old_pair(self.inventory(), a, self.core_sources())
        self.facts.update(old_pair=found["pair"], expected_writer=found["writer"])
        self.write("old-pair-plan.json", found["plan"])
        expected = found["pair"]
        if found["status"] == "rebind":  # the generated config carries video only; bind the audio half
            if self.confirm:
                self.apply(found["plan"], self.core_state()["revision"], "bind-old-pair")
            else:  # rehearsal: leave audio unbound and keep observing; the rotation step will stop at its plan
                expected = {"flow_id": found["pair"]["flow_id"], "audio_flow_id": None}
                self.facts["audio_bound"] = False
        live = {s["id"]: executor._binding(s) for s in self.core_sources()}
        if live.get(a.selected_source) != expected:
            raise obs.GateFailure("the candidate's selected source is not bound to the recorded old pair")
        self.mark("old_pair_recorded")

    def record_baseline_identity(self) -> None:
        state = self.core_state()
        self.facts.update(revision=state["revision"], program=state["program"], preview=state["preview"],
                          outputs={"program": state.get("program_flow"), "preview": state.get("preview_flow")})

    def await_old_pair_inactive(self) -> None:
        a = self.plan.a
        old = self.facts["old_pair"]
        self.say(f"[{self.plan.run_id}] BASELINE OK. Operator: switch the declared ZoomISO output away from "
                 f"MXL now. Waiting up to {a.interruption_timeout:g}s for its writer to stop.")
        self.mark("awaiting_old_pair_inactive")

        def inactive(_sample):
            flows = planner.normalise_inventory(self.inventory())
            check_inventory(flows, a)
            return not is_active(flows, old["flow_id"]) and not is_active(flows, old["audio_flow_id"])

        self.wait("awaiting_old_pair_inactive", a.interruption_timeout, inactive, "the old pair becoming inactive")
        self.mark("old_pair_inactive")

    def await_replacement(self) -> dict:
        a = self.plan.a
        self.say(f"[{self.plan.run_id}] OLD PAIR INACTIVE. Operator: switch the output back to MXL. Waiting up to "
                 f"{a.restoration_timeout:g}s for exactly one compatible replacement pair.")
        found: dict = {}

        def replacement(_sample):
            flows = planner.normalise_inventory(self.inventory())
            check_inventory(flows, a)
            plan_doc = planner.plan(flows, mapping_for(a), self.core_sources())
            entry = selected_entry(plan_doc, a.selected_source)
            if entry["status"] == "missing":
                return False
            if entry["status"] not in ("noop", "rebind"):
                raise obs.GateFailure(f"replacement refused: {entry['status']} {entry['reasons']}")
            pair = entry["desired"]
            writers = writer_of(flows, pair["flow_id"]), writer_of(flows, pair["audio_flow_id"])
            if not all(writers) or writers[0]["id"] != writers[1]["id"]:
                raise obs.GateFailure("replacement refused: video and audio are owned by different writers")
            if writers[0]["id"] != self.facts["expected_writer"]["id"]:
                raise obs.GateFailure("replacement refused: the writer container is not the recorded writer")
            found.update(plan=plan_doc, entry=entry, pair=pair)
            return True

        self.wait("awaiting_replacement", a.restoration_timeout, replacement, "one compatible replacement pair")
        self.facts["path"] = "same_uuid_continued" if found["entry"]["status"] == "noop" else "rotated"
        self.facts["new_pair"] = found["pair"]
        self.write("replacement-plan.json", found["plan"])
        self.mark("replacement_discovered")
        return found

    def rebind(self, found: dict) -> None:
        state = self.core_state()
        if state["revision"] != self.facts["revision"]:
            raise obs.GateFailure("rebind refused: the candidate Core revision changed since baseline")
        if (state["program"], state["preview"]) != (self.facts["program"], self.facts["preview"]):
            raise obs.GateFailure("rebind refused: Program/Preview changed since baseline")
        self.apply(found["plan"], self.facts["revision"], "rotation-rebind")
        self.facts["applied"] = True
        self.plan.a.selected_flow = found["pair"]["flow_id"]  # follow the stable source's new video for lag
        self.mark("rebind_applied")

    def verify_identity(self) -> None:
        a = self.plan.a
        live = {s["id"]: executor._binding(s) for s in self.core_sources()}
        if live.get(a.selected_source) != self.facts["new_pair"]:
            raise obs.GateFailure("post-rebind: the stable source is not bound to the discovered pair")
        state = self.core_state()
        if (state["program"], state["preview"]) != (self.facts["program"], self.facts["preview"]):
            raise obs.GateFailure("post-rebind: Program/Preview selection changed")
        if {"program": state.get("program_flow"), "preview": state.get("preview_flow")} != self.facts["outputs"]:
            raise obs.GateFailure("post-rebind: a candidate output flow identity changed")
        self.mark("identity_verified")

    def run(self) -> dict:
        self.deadline = self.clock() + self.plan.max_total_seconds
        failure = None
        handlers = {}
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                handlers[signum] = signal.signal(signum, self._on_signal)
            except ValueError:  # not the main thread (tests)
                pass
        try:
            self.mark("start")
            self.start()
            self.record_old_pair()
            self.motion(self.facts["old_pair"]["flow_id"], "selected_before")
            self.motion(self.plan.a.control_flow, "control_before")
            self.establish_baseline()
            self.record_baseline_identity()
            self.await_old_pair_inactive()
            found = self.await_replacement()
            if self.facts["path"] == "rotated":
                self.rebind(found)
            self.verify_identity()
            self.await_recovery()
            self.motion(self.plan.outputs["program"], "program_after")
            self.stable_hold()
        except (obs.GateFailure, obs.Interrupted, obs.Refused, planner.PlanInputError,
                executor.ApplyError) as error:
            failure = f"{type(error).__name__}: {error}"
            self.event("failure", reason=failure)
        finally:
            for signum, handler in handlers.items():
                signal.signal(signum, handler)
        verdict = evaluate_rotation(self.samples, self.baseline, self.plan.a, self.facts, failure)
        verdict["permanent_services"] = self.permanent_check()
        if not verdict["permanent_services"]["unchanged"]:
            verdict["verdict"] = "FAIL"
        verdict.update(run_id=self.plan.run_id, marks=self.marks, facts=self.facts)
        verdict["evidence"] = self.preserve()
        verdict["cleanup"] = self.cleanup()
        self.write("verdict.json", verdict)
        self.write("public-summary.json", public_summary(verdict, self.plan, self.facts))
        return verdict


def evaluate_rotation(samples, baseline, a, facts, failure) -> dict:
    checks: dict[str, dict] = {}

    def check(name, ok, detail):
        checks[name] = {"pass": bool(ok), "detail": detail}

    check("sequence_completed", failure is None, failure or "all phases reached")
    check("path_recognised", facts.get("path") in ("rotated", "same_uuid_continued"), facts.get("path"))
    if facts.get("path") == "rotated":
        check("rebind_applied", facts.get("applied") is True, facts.get("applied"))
        check("pair_changed", facts.get("new_pair") != facts.get("old_pair"),
              {"video_changed": (facts.get("new_pair") or {}).get("flow_id") != facts["old_pair"]["flow_id"]})
    motion = facts.get("motion", {})
    for label in ("selected_before", "control_before", "program_after"):
        result = motion.get(label) or {}
        check(f"content_{label}", result.get("change_fps", 0) >= a.min_change_fps, result.get("change_fps"))
    if baseline and samples:
        h0, h1 = baseline["health"], samples[-1]["health"]
        deltas = {key: obs.delta(h0, h1, key) for key in (*obs.MUST_NOT_MOVE, *obs.OUTAGE_MEASUREMENTS,
                                                           *obs.SIGNATURE_COUNTERS)}
        check("must_not_move", all(deltas[key] == 0 for key in obs.MUST_NOT_MOVE),
              {key: deltas[key] for key in obs.MUST_NOT_MOVE})
        check("no_synthetic_skip", deltas["selected_input_skipped_grains"] < obs.SYNTHETIC_SKIP_LIMIT,
              deltas["selected_input_skipped_grains"])
        check("alarm_clear_at_end", h1.get("sustained_unavailable_active", 0) == 0,
              h1.get("sustained_unavailable_active"))
        lag = samples[-1]["selected"].get("latest_input_lag_frames")
        check("final_lag_bounded", lag is not None and lag <= a.input_delay_frames + 1, lag)
        probes = {}
        for role in ("program", "preview"):
            p0, p1 = baseline["probes"][role], samples[-1]["probes"][role]
            probes[role] = {"frames": p1["frames"] - p0["frames"], "drops": p1["drops"] - p0["drops"],
                            "stalls": p1["stalls"] - p0["stalls"]}
        check("probes_clean", all(p["drops"] == 0 and p["stalls"] == 0 for p in probes.values()), probes)
    else:
        deltas, probes = {}, {}
        check("baseline_established", False, "no baseline")
    return {"verdict": "PASS" if all(c["pass"] for c in checks.values()) else "FAIL",
            "path": facts.get("path"), "checks": checks, "deltas": deltas, "probes": probes}


def public_summary(verdict: dict, plan, facts: dict) -> dict:
    a = plan.a
    secrets = {plan.evidence_dir, a.selected_source, a.control_source, a.control_flow, a.writer_container, a.group,
               *plan.outputs.values()}
    for key in ("old_pair", "new_pair"):
        for value in (facts.get(key) or {}).values():
            secrets.add(value)
    writer = facts.get("expected_writer") or {}
    secrets.update({writer.get("id"), writer.get("name")})
    secrets.discard(None)

    def scrub(value):
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items() if k not in obs.PRIVATE_KEYS}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        if isinstance(value, str):
            for secret in secrets:
                if secret:
                    value = value.replace(secret, "<redacted>")
            return value
        return value

    return scrub({k: verdict.get(k) for k in ("verdict", "path", "checks", "deltas", "probes")})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("plan", help="plan (default) or, with --apply, observe one rotation")
    obs.add_plan_arguments(p)
    p.add_argument("--easy-mxl-url", required=True, help="EasyMXL base URL (read-only GET)")
    p.add_argument("--easy-mxl-domain", required=True)
    p.add_argument("--easy-mxl-token-env", default="EASY_MXL_TOKEN", help="env var holding the token")
    p.add_argument("--writer-container", required=True, help="ZoomISO writer container name (attribution)")
    p.add_argument("--group", required=True, help="NMOS group of the declared output")
    p.add_argument("--video-role", required=True)
    p.add_argument("--audio-role", required=True)
    p.add_argument("--audio-channels", type=int, default=2)
    p.add_argument("--min-change-fps", type=float, default=5.0, help="content changes/s that prove live content")
    p.add_argument("--motion-seconds", type=float, default=5.0)
    p.add_argument("--confirm", help=f"literal {CONFIRMATION} to let the executor write bindings")
    return parser


def main(argv=None, docker=None, fetch=obs.http_get, inventory=None, port_check=obs.port_free,
         clock=time.monotonic, sleep=time.sleep, say=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "plan":
        build_parser().print_help()
        return 2
    docker = docker or obs.Docker()
    inventory = inventory or (lambda: default_inventory(args))
    try:
        if args.confirm not in (None, CONFIRMATION):
            raise obs.Refused(f"--confirm must be the literal {CONFIRMATION}")
        plan = obs.Plan(args, args.run_id or obs.new_run_id())
        checked = obs.preflight(plan, docker, fetch, port_check)
        checked["domain"] = obs.domain_check(plan, docker, allow_container=args.apply)
        if docker([*plan.docker, "container", "inspect", f"{plan.run_id}-motion"])[0] == 0:
            raise obs.Refused(f"container {plan.run_id}-motion already exists")
        old = discover_old_pair(inventory(), args, candidate_snapshot(args))
        checked["old_pair"] = {"pair": old["pair"], "writer": old["writer"], "status": old["status"]}
    except (obs.Refused, obs.GateFailure, planner.PlanInputError) as error:
        print(json.dumps({"refused": str(error)}), file=sys.stderr)
        return 2
    if not args.apply:
        print(json.dumps({"mode": "dry_run", "preflight": checked, "plan": plan.as_dict(),
                          "rebind_writes": "only with --apply and --confirm " + CONFIRMATION}, indent=2))
        return 0
    observer = RotationObserver(plan, docker, fetch, inventory, confirm=args.confirm == CONFIRMATION,
                                clock=clock, sleep=sleep, **({"say": say} if say else {}))
    observer.preflight_result = checked
    verdict = observer.run()
    print(json.dumps({k: verdict[k] for k in ("verdict", "path", "checks")}, indent=2))
    return 0 if verdict["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
