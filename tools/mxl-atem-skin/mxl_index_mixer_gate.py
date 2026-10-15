#!/usr/bin/env python3
"""Real-MXL lifecycle gate for the index-domain Program Audio mixer.

Proves, against a real MXL domain, what ``test_index_mixer.py`` proves with
fake rings: an input that is absent at startup, joins later, and is recreated
under the same flow ID never restarts the mixer or stalls the other input.

Everything runs in an **isolated temporary domain**: a new tmpfs Docker volume
that this run creates and removes. Every container has ``--network none``, so
the gate publishes no ports and cannot reach Core, a meeting, or another
service; mixer state is read with ``docker exec``. The gate never accepts a
domain path. Every resource it creates carries the label
``org.mxl.gate.run=<run id>``, and cleanup removes only those exact names,
after checking that label.

Sequence (each wait is bounded; a missing transition is a failure):

1. ``a_absent``: tone writer B is up, the mixer starts with input A absent.
   The mixer must stay up, report A offline (skipped ``offline``), and keep
   writing B's tone.
2. ``a_joined``: writer A starts. The mixer must open A without restarting,
   and the output must carry A + B.
3. ``a_gone``: writer A stops and removes its flow from the domain.
4. ``a_recreated``: a new writer A creates the flow again under the same ID.
   The mixer must close and reopen A, and the output must carry A + B again.
5. ``hold``: both inputs contribute for ``--hold-seconds``.

Throughout, the mixer container must keep running with the same start time
and no restarts, its output head must advance at every sample, and input B
must never stall or be skipped.

Subcommands:

- ``plan``: by default (or with ``--dry-run``), print the exact plan, check
  for collisions read-only, and change nothing. ``--apply`` runs it.
- ``tone-writer``: run inside a gate container; writes a constant stereo
  Float32 tone to one flow at MXL index pace.
- ``probe``: run inside the mixer container; prints the mixer state and the
  last output sample as JSON.

Known limit, deliberately not hidden: the lab SDK can still crash if a flow is
removed between ``flow_exists()`` and reader creation (one syscall wide). The
gate separates removal from re-creation by ``--gap-seconds`` but cannot close
that window. A crash shows up as a mixer exit and fails the gate.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import uuid
from array import array
from datetime import datetime, timezone
from pathlib import Path

LABEL = "org.mxl.gate.run"
DOMAIN_MOUNT = "/mxl-domain"
TOOLS_MOUNT = "/gate-tools"
MIXER_PORT = 9740
BLOCK = 480
VALUE_A = 0.125
VALUE_B = 0.25
REQUIRED_TOOLS = ("mxl_index_mixer.py", "mxl_direct.py", "av_reference.py", "mxl_index_mixer_gate.py")
RUN_ID = re.compile(r"^mxl-gate-[a-z0-9][a-z0-9-]{3,40}$")
FLOW_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
PHASES = ("startup", "a_absent", "a_joined", "a_gone", "a_recreated", "hold")


class GateFailure(RuntimeError):
    """A required transition or invariant did not hold."""


class Refused(RuntimeError):
    """The plan is unsafe to run; nothing was changed."""


# --- plan --------------------------------------------------------------------
def new_run_id(now=None, token=None) -> str:
    now = now or datetime.now(timezone.utc)
    token = token or uuid.uuid4().hex[:8]
    return f"mxl-gate-{now:%Y%m%dt%H%M%S}-{token}"


class Plan:
    """Every name, flow ID, command, and bound for one gate run."""

    def __init__(self, *, run_id: str, image: str, tools_dir: str, library: str, evidence_dir: str,
                 docker: str = "docker", flows: dict | None = None, reopen_interval_ms: float = 1000.0,
                 max_wait_ms: float = 500.0, interval: float = 2.0, hold_interval: float = 10.0,
                 hold_seconds: float = 60.0, absent_seconds: float = 6.0, gap_seconds: float = 3.0,
                 start_timeout: float = 30.0, transition_slack: float = 15.0, volume_size_mb: int = 64) -> None:
        if not RUN_ID.match(run_id):
            raise Refused(f"run id {run_id!r} must match {RUN_ID.pattern}")
        self.run_id = run_id
        self.image = image
        self.tools_dir = str(Path(tools_dir).resolve())
        self.library = library
        self.evidence_dir = str(Path(evidence_dir).resolve())
        self.docker = docker.split()
        self.flows = dict(flows or {role: str(uuid.uuid4()) for role in ("a", "b", "output")})
        for role, flow in self.flows.items():
            if not FLOW_ID.match(flow):
                raise Refused(f"flow {role} {flow!r} is not a lowercase UUIDv4")
        if len(set(self.flows.values())) != 3:
            raise Refused("flow IDs must be distinct")
        self.reopen_interval_ms = reopen_interval_ms
        self.max_wait_ms = max_wait_ms
        self.interval = interval
        self.hold_interval = hold_interval
        self.hold_seconds = hold_seconds
        self.absent_seconds = absent_seconds
        self.gap_seconds = gap_seconds
        self.start_timeout = start_timeout
        self.transition_timeout = 2 * reopen_interval_ms / 1000 + transition_slack
        self.volume_size_mb = volume_size_mb
        for name, value in (("interval", interval), ("hold-interval", hold_interval),
                            ("hold-seconds", hold_seconds), ("absent-seconds", absent_seconds),
                            ("gap-seconds", gap_seconds), ("start-timeout", start_timeout),
                            ("reopen-interval-ms", reopen_interval_ms)):
            if not value > 0:
                raise Refused(f"--{name} must be positive")
        if absent_seconds < 2 * interval:
            raise Refused("--absent-seconds must cover at least two samples")
        self.volume = f"{run_id}-domain"
        self.names = {
            "mixer": f"{run_id}-mixer",
            "writer_b": f"{run_id}-writer-b",
            "writer_a1": f"{run_id}-writer-a1",
            "writer_a2": f"{run_id}-writer-a2",
        }

    # Upper bound on wall-clock time: every wait plus the cost of sampling.
    @property
    def max_total_seconds(self) -> float:
        waits = (3 * self.start_timeout + self.absent_seconds + 2 * self.transition_timeout
                 + self.gap_seconds + self.hold_seconds + 60)
        return round(waits * 1.5)

    def _run(self, name: str, entry: list[str]) -> list[str]:
        return [*self.docker, "run", "-d", "--name", name, "--label", f"{LABEL}={self.run_id}",
                "--network", "none", "--restart", "no",
                "-v", f"{self.volume}:{DOMAIN_MOUNT}", "-v", f"{self.tools_dir}:{TOOLS_MOUNT}:ro",
                "-w", TOOLS_MOUNT, "--entrypoint", "python3", self.image, *entry]

    def volume_create(self) -> list[str]:
        return [*self.docker, "volume", "create", "--driver", "local", "--label", f"{LABEL}={self.run_id}",
                "--opt", "type=tmpfs", "--opt", "device=tmpfs", "--opt", f"o=size={self.volume_size_mb}m,mode=1777",
                self.volume]

    def writer(self, role: str) -> list[str]:
        flow = self.flows["b" if role == "writer_b" else "a"]
        value = VALUE_B if role == "writer_b" else VALUE_A
        entry = [f"{TOOLS_MOUNT}/mxl_index_mixer_gate.py", "tone-writer", "--domain", DOMAIN_MOUNT,
                 "--flow", flow, "--value", repr(value), "--library", self.library]
        if role == "writer_a1":
            entry.append("--remove-flow-on-exit")
        return self._run(self.names[role], entry)

    def mixer(self) -> list[str]:
        return self._run(self.names["mixer"], [
            f"{TOOLS_MOUNT}/mxl_index_mixer.py", "--domain", DOMAIN_MOUNT, "--output", self.flows["output"],
            "--input", self.flows["a"], "--input", self.flows["b"], "--library", self.library,
            "--core", "http://127.0.0.1:9", "--host", "127.0.0.1", "--port", str(MIXER_PORT),
            "--max-wait-ms", repr(self.max_wait_ms), "--reopen-interval-ms", repr(self.reopen_interval_ms),
            "--label", "MXL Gate Mix", "--group-hint", "MXLGate"])

    def probe(self, control: list[dict] | None = None) -> list[str]:
        command = [*self.docker, "exec", self.names["mixer"], "python3", f"{TOOLS_MOUNT}/mxl_index_mixer_gate.py",
                   "probe", "--port", str(MIXER_PORT), "--domain", DOMAIN_MOUNT,
                   "--output", self.flows["output"], "--library", self.library]
        for item in control or []:
            command += ["--control", json.dumps(item, separators=(",", ":"))]
        return command

    def stop(self, name: str) -> list[str]:
        return [*self.docker, "stop", "-t", "10", name]

    def remove_container(self, name: str) -> list[str]:
        return [*self.docker, "rm", "-f", name]

    def remove_volume(self) -> list[str]:
        return [*self.docker, "volume", "rm", self.volume]

    def inspect(self, name: str) -> list[str]:
        return [*self.docker, "container", "inspect", name]

    def logs(self, name: str) -> list[str]:
        return [*self.docker, "logs", name]

    def stats(self) -> list[str]:
        return [*self.docker, "stats", "--no-stream", "--format", "{{json .}}", self.names["mixer"]]

    def as_dict(self) -> dict:
        control = [{"mode": "manual"}, {"channel": 1, "on": True}, {"channel": 2, "on": True}]
        return {
            "run_id": self.run_id,
            "image": self.image,
            "library": self.library,
            "tools_dir": self.tools_dir,
            "evidence_dir": self.evidence_dir,
            "volume": self.volume,
            "containers": self.names,
            "flows": self.flows,
            "tone_values": {"a": VALUE_A, "b": VALUE_B, "a_plus_b": VALUE_A + VALUE_B},
            "network": "none (no ports published)",
            "bounds_seconds": {
                "start_timeout": self.start_timeout,
                "absent": self.absent_seconds,
                "transition_timeout": self.transition_timeout,
                "gap": self.gap_seconds,
                "hold": self.hold_seconds,
                "max_total": self.max_total_seconds,
            },
            "sequence": [
                {"step": "create_volume", "command": self.volume_create()},
                {"step": "start_writer_b", "command": self.writer("writer_b")},
                {"step": "start_mixer", "command": self.mixer()},
                {"step": "manual_mix_both_on", "command": self.probe(control)},
                {"step": "observe", "phase": "a_absent"},
                {"step": "start_writer_a1", "command": self.writer("writer_a1"), "phase": "a_joined"},
                {"step": "stop_writer_a1_removing_flow", "command": self.stop(self.names["writer_a1"]),
                 "phase": "a_gone"},
                {"step": "remove_writer_a1", "command": self.remove_container(self.names["writer_a1"])},
                {"step": "start_writer_a2_same_flow", "command": self.writer("writer_a2"), "phase": "a_recreated"},
                {"step": "observe", "phase": "hold"},
            ],
            "sample": {"probe": self.probe(), "inspect": self.inspect(self.names["mixer"]), "stats": self.stats()},
            "cleanup": [self.remove_container(name) for name in self.names.values()] + [self.remove_volume()],
            "cleanup_rule": f"only these exact names, each only if labelled {LABEL}={self.run_id}",
        }


# --- docker access -------------------------------------------------------------
class Docker:
    """Runs one docker command; ``run`` is injectable so tests never touch a daemon."""

    def __init__(self, run=subprocess.run, timeout: float = 60.0) -> None:
        self._run = run
        self.timeout = timeout
        self.log: list[list[str]] = []

    def __call__(self, command: list[str]) -> tuple[int, str, str]:
        self.log.append(command)
        try:
            done = self._run(command, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return 124, "", f"timed out after {self.timeout}s"
        except OSError as error:
            return 127, "", str(error)
        return done.returncode, done.stdout, done.stderr


def preflight(plan: Plan, docker: Docker) -> dict:
    """Read-only collision checks. Raises ``Refused``; changes nothing."""

    tools = Path(plan.tools_dir)
    missing = [name for name in REQUIRED_TOOLS if not (tools / name).is_file()]
    if missing:
        raise Refused(f"tools dir {tools} lacks {', '.join(missing)}")
    if Path(plan.evidence_dir).exists():
        raise Refused(f"evidence dir {plan.evidence_dir} already exists; evidence is never overwritten")
    status, _, error = docker([*plan.docker, "version", "--format", "{{.Server.Version}}"])
    if status != 0:
        raise Refused(f"docker is not usable: {error.strip()}")
    status, _, _ = docker([*plan.docker, "image", "inspect", plan.image])
    if status != 0:
        raise Refused(f"image {plan.image} is not present; the gate never pulls or builds")
    for name in plan.names.values():
        status, _, _ = docker(plan.inspect(name))
        if status == 0:
            raise Refused(f"container {name} already exists")
    status, _, _ = docker([*plan.docker, "volume", "inspect", plan.volume])
    if status == 0:
        raise Refused(f"volume {plan.volume} already exists")
    return {"image": plan.image, "names_free": sorted(plan.names.values()), "volume_free": plan.volume}


# --- sampling and verdict ------------------------------------------------------
def _percent(text: str) -> float | None:
    try:
        return float(text.strip().rstrip("%"))
    except (AttributeError, ValueError):
        return None


def _mib(text: str) -> float | None:
    match = re.match(r"\s*([0-9.]+)\s*([KMG]i?B|B)", text or "")
    if not match:
        return None
    scale = {"B": 1 / 2**20, "KiB": 1 / 1024, "KB": 1 / 1024, "MiB": 1, "MB": 1, "GiB": 1024, "GB": 1024}
    return float(match.group(1)) * scale[match.group(2)]


def channel_view(channel: dict) -> dict:
    return {key: channel.get(key) for key in (
        "online", "stalled", "opens", "closes", "on_time", "late", "skipped", "skipped_reasons",
        "recoveries", "last_open_error")}


def take_sample(plan: Plan, docker: Docker, phase: str, clock=time.monotonic) -> dict:
    sample = {"t": round(clock(), 3), "phase": phase, "errors": []}
    status, out, error = docker(plan.inspect(plan.names["mixer"]))
    try:
        info = json.loads(out)[0] if status == 0 else None
    except (ValueError, IndexError):
        info = None
    if info:
        state = info.get("State", {})
        sample["container"] = {"running": state.get("Running"), "status": state.get("Status"),
                               "exit_code": state.get("ExitCode"), "started_at": state.get("StartedAt"),
                               "restart_count": info.get("RestartCount")}
    else:
        sample["container"] = None
        sample["errors"].append(f"inspect: {error.strip()}")
    status, out, error = docker(plan.probe())
    try:
        probe = json.loads(out) if status == 0 else None
    except ValueError:
        probe = None
    if probe and probe.get("state"):
        state = probe["state"]
        sample["output_head"] = state.get("output_head_index")
        sample["output_progress_age_seconds"] = state.get("output_progress_age_seconds")
        sample["blocks_written"] = state.get("blocks_written")
        sample["blocks_degraded"] = state.get("blocks_degraded")
        sample["output_sample"] = probe.get("output_sample")
        sample["channels"] = {number: channel_view(channel)
                              for number, channel in state.get("channels", {}).items()}
    else:
        sample["errors"].append(f"probe: {(error or out).strip()[:300]}")
    status, out, _ = docker(plan.stats())
    try:
        stats = json.loads(out.splitlines()[0]) if status == 0 and out.strip() else {}
    except ValueError:
        stats = {}
    sample["cpu_percent"] = _percent(stats.get("CPUPerc", ""))
    sample["rss_mib"] = _mib(stats.get("MemUsage", ""))
    return sample


def contributing(before: dict, after: dict, number: str) -> bool:
    """True when channel ``number`` delivered blocks between two samples."""

    try:
        first, last = before["channels"][number], after["channels"][number]
    except (KeyError, TypeError):
        return False
    return (last["on_time"] + last["late"]) > (first["on_time"] + first["late"])


def evaluate(samples: list[dict], writer_a2_created: bool | None, failure: str | None = None) -> dict:
    """Pure verdict over the samples of one run."""

    checks: dict[str, dict] = {}

    def check(name: str, ok: bool, detail) -> None:
        checks[name] = {"pass": bool(ok), "detail": detail}

    by_phase = {phase: [s for s in samples if s["phase"] == phase] for phase in PHASES}
    # Probes fail while the mixer's control server starts; that is not a fault.
    measured = [s for s in samples if s["phase"] != "startup"]
    valid = [s for s in measured if s.get("channels") and s.get("container")]

    started = {s["container"]["started_at"] for s in samples if s.get("container")}
    restarts = [s["container"]["restart_count"] for s in samples if s.get("container")]
    not_running = [s["t"] for s in samples if not (s.get("container") or {}).get("running")]
    check("mixer_never_restarted",
          not not_running and len(started) == 1 and all(count == 0 for count in restarts),
          {"start_times": sorted(t for t in started if t), "max_restart_count": max(restarts, default=None),
           "samples_not_running": not_running,
           "exit_codes": sorted({s["container"]["exit_code"] for s in samples
                                  if s.get("container") and not s["container"]["running"]})})

    heads = [s.get("output_head") for s in measured]
    stuck = [measured[i]["t"] for i in range(1, len(heads))
             if heads[i] is None or heads[i - 1] is None or heads[i] <= heads[i - 1]]
    check("output_advanced_every_sample", len(heads) >= 2 and not stuck,
          {"samples": len(heads), "not_advanced_at": stuck})

    def ch(sample, number, key):
        return ((sample.get("channels") or {}).get(number) or {}).get(key)

    def offline_skips(sample):
        return (ch(sample, "1", "skipped_reasons") or {}).get("offline") or 0

    absent = [s for s in by_phase["a_absent"] if s.get("channels")]
    check("a_absent_offline_and_b_airs",
          len(absent) >= 2
          and all(ch(s, "1", "online") is False for s in absent)
          and offline_skips(absent[-1]) > offline_skips(absent[0])
          and contributing(absent[0], absent[-1], "2")
          and all(s.get("output_sample") == VALUE_B for s in absent[1:]),
          {"samples": len(absent),
           "a_online": [ch(s, "1", "online") for s in absent],
           "a_offline_skips": [offline_skips(s) for s in absent],
           "output_samples": [s.get("output_sample") for s in absent]})

    def joined(phase: str, opens: int) -> tuple[bool, dict]:
        rows = [s for s in by_phase[phase] if s.get("channels")]
        last = rows[-1] if rows else {}
        ok = (bool(rows) and ch(last, "1", "online") is True and (ch(last, "1", "opens") or 0) >= opens
              and last.get("output_sample") == VALUE_A + VALUE_B)
        return ok, {"samples": len(rows), "a_opens": ch(last, "1", "opens"), "a_closes": ch(last, "1", "closes"),
                    "a_online": ch(last, "1", "online"), "output_sample": last.get("output_sample")}

    ok, detail = joined("a_joined", 1)
    check("a_joined_without_restart", ok, detail)

    gone = [s for s in by_phase["a_gone"] if s.get("channels")]
    check("a_gone_observed", bool(gone),
          {"samples": len(gone), "a_online": [ch(s, "1", "online") for s in gone],
           "a_stalled": [ch(s, "1", "stalled") for s in gone],
           "output_samples": [s.get("output_sample") for s in gone]})

    ok, detail = joined("a_recreated", 2)
    first_join = [s for s in by_phase["a_joined"] if s.get("channels")]
    closes_before = ch(first_join[-1], "1", "closes") if first_join else None
    rejoined = [s for s in by_phase["a_recreated"] if s.get("channels")]
    closes_after = ch(rejoined[-1], "1", "closes") if rejoined else None
    reopened = closes_before is not None and closes_after is not None and closes_after > closes_before
    check("a_recreated_closed_and_reopened", ok and reopened and writer_a2_created is True,
          {**detail, "a_closes_before": closes_before, "a_closes_after": closes_after,
           "writer_a2_created_flow": writer_a2_created})

    held = [s for s in by_phase["hold"] if s.get("channels")]
    check("hold_both_contribute",
          len(held) >= 2 and contributing(held[0], held[-1], "1") and contributing(held[0], held[-1], "2")
          and all(s.get("output_sample") == VALUE_A + VALUE_B for s in held)
          and all(ch(s, "1", "online") for s in held),
          {"samples": len(held), "output_samples": sorted({s.get("output_sample") for s in held}, key=str)})

    b_stalled = [s["t"] for s in valid if ch(s, "2", "stalled")]
    b_skips = [ch(s, "2", "skipped") for s in valid]
    check("b_never_stalled_or_skipped",
          bool(valid) and not b_stalled and len(set(b_skips)) == 1 and b_skips[0] == 0,
          {"stalled_at": b_stalled, "skipped_first_last": [b_skips[0], b_skips[-1]] if b_skips else None})

    errors = [(s["t"], s["errors"]) for s in measured if s.get("errors")]
    check("sampling_clean", not errors, errors[:10])

    if failure:
        check("sequence_completed", False, failure)
    else:
        check("sequence_completed", True, "all phases reached")

    cpu = [s["cpu_percent"] for s in samples if s.get("cpu_percent") is not None]
    rss = [s["rss_mib"] for s in samples if s.get("rss_mib") is not None]
    transitions = []
    previous = None
    for s in valid:
        view = (s["phase"], ch(s, "1", "online"), ch(s, "1", "stalled"), ch(s, "1", "opens"), ch(s, "1", "closes"))
        if view != previous:
            transitions.append({"t": s["t"], "phase": view[0], "a_online": view[1], "a_stalled": view[2],
                                "a_opens": view[3], "a_closes": view[4],
                                "a_skipped_reasons": ch(s, "1", "skipped_reasons"),
                                "output_sample": s.get("output_sample")})
            previous = view
    final = valid[-1] if valid else {}
    return {
        "verdict": "PASS" if all(c["pass"] for c in checks.values()) else "FAIL",
        "checks": checks,
        "a_transitions": transitions,
        "final_channels": final.get("channels"),
        "blocks": {"written": final.get("blocks_written"), "degraded": final.get("blocks_degraded")},
        "resources": {
            "mixer_cpu_percent": {"min": min(cpu), "max": max(cpu), "avg": round(sum(cpu) / len(cpu), 2)} if cpu else None,
            "mixer_rss_mib": {"min": min(rss), "max": max(rss)} if rss else None,
        },
        "samples": len(samples),
    }


# --- execution -------------------------------------------------------------------
class Gate:
    def __init__(self, plan: Plan, docker: Docker, clock=time.monotonic, sleep=time.sleep) -> None:
        self.plan = plan
        self.docker = docker
        self.clock = clock
        self.sleep = sleep
        self.samples: list[dict] = []
        self.events: list[dict] = []
        self.created: list[str] = []
        self.volume_created = False
        self.writer_a2_created: bool | None = None
        self.deadline = None

    def event(self, step: str, **detail) -> None:
        self.events.append({"t": round(self.clock(), 3), "step": step, **detail})

    def must(self, step: str, command: list[str]) -> str:
        status, out, error = self.docker(command)
        self.event(step, status=status)
        if status != 0:
            raise GateFailure(f"{step} failed ({status}): {error.strip()[:300]}")
        return out

    def start(self, role: str, command: list[str]) -> None:
        name = self.plan.names[role]
        self.created.append(name)  # recorded before the call so a partial start is still cleaned up
        self.must(f"start_{role}", command)

    def check_deadline(self) -> None:
        if self.clock() > self.deadline:
            raise GateFailure(f"total bound of {self.plan.max_total_seconds}s exceeded")

    def sample(self, phase: str) -> dict:
        self.check_deadline()
        sample = take_sample(self.plan, self.docker, phase, self.clock)
        self.samples.append(sample)
        container = sample.get("container")
        if container and not container["running"]:
            raise GateFailure(f"mixer exited during {phase} with code {container['exit_code']}")
        if container and container["restart_count"]:
            raise GateFailure(f"mixer restarted during {phase}")
        return sample

    def observe(self, phase: str, seconds: float, interval: float) -> None:
        end = self.clock() + seconds
        self.sample(phase)
        while self.clock() < end:
            self.sleep(min(interval, max(0.0, end - self.clock())))
            self.sample(phase)

    def wait_for(self, phase: str, timeout: float, predicate, reason: str) -> dict:
        end = self.clock() + timeout
        while True:
            sample = self.sample(phase)
            if sample.get("channels") and predicate(sample):
                self.event(f"{phase}_reached")
                return sample
            if self.clock() >= end:
                raise GateFailure(f"{phase}: {reason} not reached within {timeout:g}s")
            self.sleep(self.plan.interval)

    def wait_log(self, role: str, marker: str, timeout: float) -> str:
        name = self.plan.names[role]
        end = self.clock() + timeout
        while True:
            self.check_deadline()
            status, out, error = self.docker(self.plan.logs(name))
            text = out + error
            for line in text.splitlines():
                if marker in line:
                    return line
            if "tone_writer=error" in text:
                raise GateFailure(f"{name} failed: {text.strip()[-300:]}")
            if self.clock() >= end:
                raise GateFailure(f"{name} did not log {marker!r} within {timeout:g}s")
            self.sleep(min(1.0, self.plan.interval))

    def run(self) -> dict:
        plan = self.plan
        self.deadline = self.clock() + plan.max_total_seconds
        failure = None
        try:
            self.must("create_volume", plan.volume_create())
            self.volume_created = True
            self.start("writer_b", plan.writer("writer_b"))
            self.wait_log("writer_b", "tone_writer=ready", plan.start_timeout)
            self.start("mixer", plan.mixer())
            self.wait_for("startup", plan.start_timeout, lambda s: True, "mixer state")
            self.must("manual_mix_both_on",
                      plan.probe([{"mode": "manual"}, {"channel": 1, "on": True}, {"channel": 2, "on": True}]))
            self.observe("a_absent", plan.absent_seconds, plan.interval)

            self.start("writer_a1", plan.writer("writer_a1"))
            self.wait_log("writer_a1", "tone_writer=ready", plan.start_timeout)
            self.wait_for("a_joined", plan.transition_timeout,
                          lambda s: s["channels"]["1"]["online"] and s.get("output_sample") == VALUE_A + VALUE_B,
                          "A online and output carrying A + B")
            self.observe("a_joined", 2 * plan.interval, plan.interval)

            self.must("stop_writer_a1", plan.stop(plan.names["writer_a1"]))
            self.wait_log("writer_a1", "tone_writer=removed", plan.start_timeout)
            self.must("remove_writer_a1", plan.remove_container(plan.names["writer_a1"]))
            self.created.remove(plan.names["writer_a1"])
            self.observe("a_gone", plan.gap_seconds, plan.interval)

            self.start("writer_a2", plan.writer("writer_a2"))
            ready = self.wait_log("writer_a2", "tone_writer=ready", plan.start_timeout)
            self.writer_a2_created = "created=True" in ready
            self.event("writer_a2_ready", line=ready.strip())
            if not self.writer_a2_created:
                raise GateFailure("writer A2 reopened an existing flow; re-creation was not exercised")
            self.wait_for("a_recreated", plan.transition_timeout,
                          lambda s: s["channels"]["1"]["online"] and s["channels"]["1"]["opens"] >= 2
                          and s["channels"]["1"]["closes"] > 0 and s.get("output_sample") == VALUE_A + VALUE_B,
                          "A closed, reopened, and output carrying A + B")
            self.observe("hold", plan.hold_seconds, plan.hold_interval)
        except (GateFailure, KeyError, TypeError) as error:
            failure = f"{type(error).__name__}: {error}"
            self.event("failure", reason=failure)
        verdict = evaluate(self.samples, self.writer_a2_created, failure)
        verdict["run_id"] = plan.run_id
        verdict["evidence"] = self.preserve_evidence()
        verdict["cleanup"] = self.cleanup()
        return verdict

    def preserve_evidence(self) -> dict:
        directory = Path(self.plan.evidence_dir)
        directory.mkdir(parents=True, exist_ok=False)
        for name in list(self.created):
            status, out, error = self.docker(self.plan.logs(name))
            (directory / f"{name}.log").write_text(out + error)
            status, out, _ = self.docker(self.plan.inspect(name))
            if status == 0:
                (directory / f"{name}.inspect.json").write_text(out)
        with open(directory / "samples.jsonl", "w") as handle:
            for sample in self.samples:
                handle.write(json.dumps(sample, separators=(",", ":")) + "\n")
        (directory / "events.json").write_text(json.dumps(self.events, indent=2))
        (directory / "plan.json").write_text(json.dumps(self.plan.as_dict(), indent=2))
        return {"dir": str(directory), "files": sorted(p.name for p in directory.iterdir())}

    def _labelled(self, kind: str, name: str) -> bool:
        labels = ".Config.Labels" if kind == "container" else ".Labels"
        template = "{{index " + labels + ' "' + LABEL + '"}}'
        status, out, _ = self.docker([*self.plan.docker, kind, "inspect", "--format", template, name])
        return status == 0 and out.strip() == self.plan.run_id

    def cleanup(self) -> dict:
        """Remove only what this run created, by exact name, after checking its run label."""

        removed, refused = [], []
        for name in list(self.created):
            if self._labelled("container", name):
                status, _, error = self.docker(self.plan.remove_container(name))
                (removed if status == 0 else refused).append(name if status == 0 else f"{name}: {error.strip()}")
            else:
                refused.append(f"{name}: label {LABEL} does not match {self.plan.run_id}; left in place")
        if self.volume_created:
            if self._labelled("volume", self.plan.volume):
                status, _, error = self.docker(self.plan.remove_volume())
                (removed if status == 0 else refused).append(
                    self.plan.volume if status == 0 else f"{self.plan.volume}: {error.strip()}")
            else:
                refused.append(f"{self.plan.volume}: label mismatch; left in place")
        result = {"removed": removed, "not_removed": refused}
        (Path(self.plan.evidence_dir) / "cleanup.json").write_text(json.dumps(result, indent=2))
        return result


# --- in-container roles --------------------------------------------------------
RUNNING = True


def _stop(*_args) -> None:
    global RUNNING
    RUNNING = False


def tone_writer(args) -> int:
    """Write a constant stereo tone to ``args.flow`` at MXL index pace until SIGTERM."""

    import mxl_direct as mxl
    import mxl_index_mixer as mixer

    if not FLOW_ID.match(args.flow):
        print(f"tone_writer=error reason=flow id {args.flow!r} is not a UUIDv4", file=sys.stderr, flush=True)
        return 2
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    api = mxl.load_library(args.library)
    instance = api.mxlCreateInstance(args.domain.encode(), b"")
    if not instance:
        print("tone_writer=error reason=mxlCreateInstance failed", file=sys.stderr, flush=True)
        return 1
    try:
        writer = mxl.SampleWriter(api, instance, mixer.flow_definition(
            args.flow, "MXLGateTone", "MXL Gate Tone", "Index mixer lifecycle gate tone"))
    except RuntimeError as error:
        print(f"tone_writer=error reason={error}", file=sys.stderr, flush=True)
        return 1
    block = array("f", [args.value] * BLOCK).tobytes()
    ring = writer.info.continuous.bufferLength or 4 * BLOCK
    end = mixer.mxl_now_index() // BLOCK * BLOCK
    written = errors = 0
    print(f"tone_writer=ready flow={args.flow} created={writer.created} ring={ring}", file=sys.stderr, flush=True)
    try:
        while RUNNING:
            now = mixer.mxl_now_index()
            if end > now:
                time.sleep(min((end - now) / mixer.SAMPLE_RATE, 0.05))
                continue
            if now - end > ring // 2:
                end = now // BLOCK * BLOCK  # far behind: never write a range the ring no longer holds
            if writer.write(end, [block, block]) == mxl.MXL_STATUS_OK:
                written += 1
            else:
                errors += 1
            end += BLOCK
    finally:
        writer.close()
        api.mxlDestroyInstance(instance)
        print(f"tone_writer=stopped blocks={written} errors={errors}", file=sys.stderr, flush=True)
    if args.remove_flow_on_exit:
        path = os.path.join(args.domain, f"{args.flow}.mxl-flow")
        if os.path.isdir(path):
            shutil.rmtree(path)
        print(f"tone_writer=removed flow={args.flow}", file=sys.stderr, flush=True)
    return 0


def probe(args) -> int:
    """Apply optional control commands, then print mixer state and the last output sample."""

    base = f"http://127.0.0.1:{args.port}"
    for item in args.control or []:
        request = urllib.request.Request(base + "/control", data=item.encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=3) as response:
            response.read()
    with urllib.request.urlopen(base + "/state", timeout=3) as response:
        state = json.load(response)
    result = {"state": state, "output_sample": None}
    import mxl_direct as mxl

    if mxl.flow_exists(args.domain, args.output):
        api = mxl.load_library(args.library)
        instance = api.mxlCreateInstance(args.domain.encode(), b"")
        if instance:
            try:
                probe_reader = mxl.Reader(api, instance, args.output)
                ring = probe_reader.config().continuous.bufferLength
                probe_reader.close()
                reader = mxl.SampleReader(api, instance, args.output, ring)
                head = reader.head()
                if head:
                    status, channels, torn = reader.read(head, BLOCK, 0, 2)
                    if status == mxl.MXL_STATUS_OK and channels and not torn:
                        result["output_sample"] = array("f", channels[0])[-1]
                    result["output_read_status"] = status
                reader.close()
            finally:
                api.mxlDestroyInstance(instance)
    print(json.dumps(result, separators=(",", ":")))
    return 0


# --- command line ----------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command")
    gate = sub.add_parser("plan", help="plan (default) or, with --apply, run the gate")
    gate.add_argument("--image", required=True, help="existing local image with python3 and libmxl")
    gate.add_argument("--library", default="libmxl.so")
    gate.add_argument("--tools-dir", default=str(Path(__file__).resolve().parent))
    gate.add_argument("--run-id", help="default: mxl-gate-<utc>-<random>")
    gate.add_argument("--evidence-dir", help="default: ./<run id>-evidence; must not exist")
    gate.add_argument("--docker", default="docker", help='docker command, e.g. "sudo -n docker"')
    gate.add_argument("--hold-seconds", type=float, default=60.0, help="60 for the smoke, 600 to confirm")
    gate.add_argument("--interval", type=float, default=2.0)
    gate.add_argument("--hold-interval", type=float, default=10.0)
    gate.add_argument("--absent-seconds", type=float, default=6.0)
    gate.add_argument("--gap-seconds", type=float, default=3.0)
    gate.add_argument("--start-timeout", type=float, default=30.0)
    gate.add_argument("--reopen-interval-ms", type=float, default=1000.0)
    gate.add_argument("--max-wait-ms", type=float, default=500.0)
    mode = gate.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the plan and change nothing (default)")
    mode.add_argument("--apply", action="store_true", help="run the gate")

    writer = sub.add_parser("tone-writer", help="(in a gate container) write a constant tone")
    writer.add_argument("--domain", required=True)
    writer.add_argument("--flow", required=True)
    writer.add_argument("--value", type=float, required=True)
    writer.add_argument("--library", default="libmxl.so")
    writer.add_argument("--remove-flow-on-exit", action="store_true")

    reader = sub.add_parser("probe", help="(in the mixer container) print state and the output sample")
    reader.add_argument("--port", type=int, default=MIXER_PORT)
    reader.add_argument("--domain", required=True)
    reader.add_argument("--output", required=True)
    reader.add_argument("--library", default="libmxl.so")
    reader.add_argument("--control", action="append")
    return parser


def main(argv=None, docker: Docker | None = None, clock=time.monotonic, sleep=time.sleep) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "tone-writer":
        return tone_writer(args)
    if args.command == "probe":
        return probe(args)
    if args.command != "plan":
        build_parser().print_help()
        return 2
    run_id = args.run_id or new_run_id()
    docker = docker or Docker()
    try:
        plan = Plan(run_id=run_id, image=args.image, tools_dir=args.tools_dir, library=args.library,
                    evidence_dir=args.evidence_dir or f"{run_id}-evidence", docker=args.docker,
                    reopen_interval_ms=args.reopen_interval_ms, max_wait_ms=args.max_wait_ms,
                    interval=args.interval, hold_interval=args.hold_interval, hold_seconds=args.hold_seconds,
                    absent_seconds=args.absent_seconds, gap_seconds=args.gap_seconds,
                    start_timeout=args.start_timeout)
        checked = preflight(plan, docker)
    except Refused as error:
        print(json.dumps({"refused": str(error)}), file=sys.stderr)
        return 2
    if not args.apply:
        print(json.dumps({"mode": "dry_run", "preflight": checked, "plan": plan.as_dict()}, indent=2))
        return 0
    verdict = Gate(plan, docker, clock=clock, sleep=sleep).run()
    (Path(plan.evidence_dir) / "verdict.json").write_text(json.dumps(verdict, indent=2))
    print(json.dumps(verdict, indent=2))
    return 0 if verdict["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
