#!/usr/bin/env python3
"""Observer harness for the controlled real-ZoomISO writer-recovery gate.

Implements ``docs/REAL_ZOOMISO_RECOVERY_GATE.md``. The harness prepares an
isolated candidate Core and two output probes, then watches while **the
operator** interrupts and restores one ZoomISO output by hand. It never calls a
ZoomISO API, never runs a command in a ZoomISO container, and never changes a
meeting, a mapping, networking, VM resources, or a permanent service.

Subcommands:

- ``plan``: by default (or ``--dry-run``), run every read-only preflight and
  print the exact plan; change nothing. ``--apply`` runs the gate.
- ``probe``: (inside a probe container) read one output flow by index and
  print cumulative frame, drop, and stall counts once per second.
- ``domain-check``: (inside a one-shot container) report which flows exist.
- ``remove-flows``: (inside a one-shot container, after the candidate is
  gone) remove exactly the candidate's own output flow directories.

Phases, each with a bound (a missing transition fails the gate):

``preflight → start → baseline → awaiting_interruption →
interruption_observed → awaiting_restoration → recovery_observed →
stable_hold → verdict``

Evidence is written before cleanup, on success, failure, timeout, or Ctrl-C.
Cleanup removes only containers labelled with this run's identity, and only
the two candidate output flow directories this run created.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

LABEL = "org.mxl.recovery-gate.run"
DOMAIN_MOUNT = "/mxl-domain"
TOOLS_MOUNT = "/gate-tools"
CORE_CONFIG = "/etc/mxl-switcher-core.conf"
CORE_DEFINITIONS = "/etc/mxl-switcher-core/definitions"
CORE_PORT = 18085
API = "/api/mxl/v1"
FRAME_RATE = 30
RUN_ID = re.compile(r"^mxl-recovery-[a-z0-9][a-z0-9-]{3,40}$")
FLOW_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
REQUIRED_TOOLS = ("mxl_recovery_observer.py", "mxl_direct.py")
PHASES = ("start", "baseline", "awaiting_interruption", "interruption_observed",
          "awaiting_restoration", "recovery_observed", "stable_hold")

# Counter-delta policy (baseline → final), from the runbook's pass criteria.
MUST_NOT_MOVE = ("invalid", "too_early", "too_late", "timeouts", "selected_input_regressions")
OUTAGE_MEASUREMENTS = ("held", "selected_input_repeats", "presentation_fallbacks",
                       "presentation_fallback_entries", "sustained_unavailable_alarms",
                       "input_timeline_resets", "reader_reopens")
SIGNATURE_COUNTERS = ("selected_input_skip_events", "selected_input_skipped_grains")
SYNTHETIC_SKIP_LIMIT = 1_000_000  # anything this large is an epoch artefact, never a real outage


class Refused(RuntimeError):
    """Unsafe or incomplete; nothing was changed."""


class GateFailure(RuntimeError):
    """A bounded phase did not complete or an invariant broke."""


class Interrupted(RuntimeError):
    """SIGINT/SIGTERM: preserve evidence, clean up, stop."""


# --- plan --------------------------------------------------------------------
def new_run_id(now=None, token=None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"mxl-recovery-{now:%Y%m%dt%H%M%S}-{token or uuid.uuid4().hex[:8]}"


def port_free(host: str, port: int, connect_timeout: float = 0.5) -> bool:
    """Same operational probe as the Core bundle: reuse-bind, then connect."""

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    with socket.socket(family, socket.SOCK_STREAM) as client:
        client.settimeout(connect_timeout)
        try:
            client.connect(("127.0.0.1" if host in ("0.0.0.0", "") else host, port))
        except OSError:
            return True
    return False


def definition(flow_id: str, role: str, group: str) -> dict:
    return {
        "id": flow_id, "description": f"Recovery gate candidate {role}", "label": f"Recovery Gate {role}",
        "parents": [], "tags": {"urn:x-nmos:tag:grouphint/v1.0": [f"{group}:Video"]},
        "format": "urn:x-nmos:format:video", "media_type": "video/v210",
        "grain_rate": {"numerator": FRAME_RATE, "denominator": 1},
        "frame_width": 1920, "frame_height": 1080, "interlace_mode": "progressive", "colorspace": "BT709",
        "components": [{"name": "Y", "width": 1920, "height": 1080, "bit_depth": 10},
                       {"name": "Cb", "width": 960, "height": 1080, "bit_depth": 10},
                       {"name": "Cr", "width": 960, "height": 1080, "bit_depth": 10}],
    }


class Plan:
    """Every name, identity, command, and bound for one recovery-gate run."""

    def __init__(self, a, run_id: str, outputs: dict | None = None) -> None:
        if not RUN_ID.match(run_id):
            raise Refused(f"run id {run_id!r} must match {RUN_ID.pattern}")
        for name in ("selected_flow", "control_flow"):
            if not FLOW_ID.match(getattr(a, name) or ""):
                raise Refused(f"--{name.replace('_', '-')} must be a flow UUID")
        for name in ("selected_source", "control_source"):
            if not SOURCE_ID.match(getattr(a, name) or ""):
                raise Refused(f"--{name.replace('_', '-')} must be a short source id")
        if a.selected_flow == a.control_flow or a.selected_source == a.control_source:
            raise Refused("the selected and control sources must differ")
        if (a.domain_path is None) == (a.domain_volume is None):
            raise Refused("give exactly one of --domain-path or --domain-volume")
        if not 1024 <= a.host_port <= 65535:
            raise Refused("--host-port must be 1024-65535")
        if a.host_port in a.forbid_port:
            raise Refused(f"--host-port {a.host_port} is a declared permanent port")
        for name in ("baseline_seconds", "stable_seconds", "interval", "interruption_timeout",
                     "restoration_timeout", "recovery_timeout", "start_timeout"):
            if not getattr(a, name) > 0:
                raise Refused(f"--{name.replace('_', '-')} must be positive")
        if a.stable_seconds < 30:
            raise Refused("--stable-seconds must be at least 30 (runbook)")
        self.a = a
        self.run_id = run_id
        self.docker = a.docker.split()
        self.tools_dir = str(Path(a.tools_dir).resolve())
        self.evidence_dir = str(Path(a.evidence_dir or f"{run_id}-evidence").resolve())
        self.outputs = dict(outputs or {"program": str(uuid.uuid4()), "preview": str(uuid.uuid4())})
        if set(self.outputs.values()) & {a.selected_flow, a.control_flow} or len(set(self.outputs.values())) != 2:
            raise Refused("candidate output ids must be fresh and distinct")
        self.names = {"core": f"{run_id}-core", "probe_program": f"{run_id}-probe-program",
                      "probe_preview": f"{run_id}-probe-preview"}
        self.helper = f"{run_id}-helper"
        self.config_dir = str(Path(self.evidence_dir) / "candidate-config")
        self.core_url = f"http://127.0.0.1:{a.host_port}{API}"

    @property
    def domain_mount(self) -> str:
        source = self.a.domain_volume or str(Path(self.a.domain_path).resolve())
        kind = "volume" if self.a.domain_volume else "bind"
        return f"type={kind},source={source},target={DOMAIN_MOUNT}"

    @property
    def max_total_seconds(self) -> float:
        a = self.a
        return round(2 * a.start_timeout + a.baseline_seconds * 3 + a.interruption_timeout
                     + a.restoration_timeout + a.recovery_timeout + a.stable_seconds + 120)

    def core_config(self) -> str:
        a = self.a
        return "\n".join([
            f"# Recovery gate candidate {self.run_id} (generated; do not edit)",
            f"domain={DOMAIN_MOUNT}",
            f"source={a.selected_source}|{a.selected_flow}|Recovery selected",
            f"source={a.control_source}|{a.control_flow}|Recovery control",
            f"program_definition={CORE_DEFINITIONS}/program.json",
            f"program_id={self.outputs['program']}",
            f"preview_definition={CORE_DEFINITIONS}/preview.json",
            f"preview_id={self.outputs['preview']}",
            f"initial_program={a.selected_source}",
            f"initial_preview={a.control_source}",
            "width=1920", "height=1080", f"rate_numerator={FRAME_RATE}", "rate_denominator=1",
            f"input_delay_frames={a.input_delay_frames}",
            "control_host=0.0.0.0", f"control_port={CORE_PORT}", "",
        ])

    def _labels(self) -> list[str]:
        return ["--label", f"{LABEL}={self.run_id}"]

    def core(self) -> list[str]:
        return [*self.docker, "run", "--detach", "--name", self.names["core"], *self._labels(),
                "--restart", "no", "--publish", f"127.0.0.1:{self.a.host_port}:{CORE_PORT}",
                "--mount", self.domain_mount,
                "--mount", f"type=bind,source={self.config_dir}/core.conf,target={CORE_CONFIG},readonly",
                "--mount", f"type=bind,source={self.config_dir}/definitions,target={CORE_DEFINITIONS},readonly",
                self.a.image, "--config", CORE_CONFIG]

    def _python(self, name: str, *entry: str, detach: bool = True, writable: bool = False) -> list[str]:
        mount = self.domain_mount + ("" if writable or not self.a.probe_readonly else ",readonly")
        return [*self.docker, "run", *(["--detach"] if detach else ["--rm"]), "--name", name, *self._labels(),
                "--restart", "no", "--network", "none", "--mount", mount,
                "--mount", f"type=bind,source={self.tools_dir},target={TOOLS_MOUNT},readonly",
                "-w", TOOLS_MOUNT, "--entrypoint", "python3", self.a.probe_image,
                f"{TOOLS_MOUNT}/mxl_recovery_observer.py", *entry, "--library", self.a.library]

    def probe(self, role: str) -> list[str]:
        return self._python(self.names[f"probe_{role}"], "probe", "--domain", DOMAIN_MOUNT,
                            "--flow", self.outputs[role])

    def domain_check(self) -> list[str]:
        flows = [self.a.selected_flow, self.a.control_flow, *self.outputs.values()]
        return self._python(self.helper, "domain-check", "--domain", DOMAIN_MOUNT,
                            *[x for flow in flows for x in ("--flow", flow)], detach=False)

    def remove_flows(self) -> list[str]:
        return self._python(self.helper, "remove-flows", "--domain", DOMAIN_MOUNT,
                            *[x for flow in self.outputs.values() for x in ("--flow", flow)],
                            detach=False, writable=True)

    def as_dict(self) -> dict:
        a = self.a
        return {
            "run_id": self.run_id,
            "candidate_image": a.image,
            "probe_image": a.probe_image,
            "domain_mount": self.domain_mount,
            "selected": {"source": a.selected_source, "flow": a.selected_flow, "candidate_bus": "program"},
            "control": {"source": a.control_source, "flow": a.control_flow, "candidate_bus": "preview"},
            "candidate_outputs": self.outputs,
            "containers": self.names,
            "core_url": self.core_url,
            "operator_core_url": a.operator_core_url,
            "program_audio_url": a.program_audio_url,
            "permanent_services": a.permanent,
            "bounds_seconds": {"start": a.start_timeout, "baseline": a.baseline_seconds,
                               "interruption": a.interruption_timeout, "restoration": a.restoration_timeout,
                               "recovery": a.recovery_timeout, "stable_hold": a.stable_seconds,
                               "max_total": self.max_total_seconds},
            "commands": {"domain_check": self.domain_check(), "core": self.core(),
                         "probe_program": self.probe("program"), "probe_preview": self.probe("preview"),
                         "remove_candidate_flows": self.remove_flows()},
            "operator_actions": ["interrupt the selected ZoomISO output when prompted",
                                 "restore its exact original assignment when prompted"],
            "harness_never": ["calls a ZoomISO API or container", "changes a meeting, mapping, or network",
                              "touches a permanent service or the operator Core"],
            "cleanup": [f"docker rm -f {name} (only if labelled {LABEL}={self.run_id})"
                        for name in self.names.values()]
                       + [f"remove {DOMAIN_MOUNT}/{flow}.mxl-flow (candidate output; only after the core is gone)"
                          for flow in self.outputs.values()],
        }


# --- I/O seams ------------------------------------------------------------------
class Docker:
    def __init__(self, run=subprocess.run, timeout: float = 60.0) -> None:
        self._run, self.timeout, self.log = run, timeout, []

    def __call__(self, command: list[str]) -> tuple[int, str, str]:
        self.log.append(command)
        try:
            done = self._run(command, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return 124, "", "timed out"
        except OSError as error:
            return 127, "", str(error)
        return done.returncode, done.stdout, done.stderr


def http_get(url: str, timeout: float = 3.0):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def fetch_json(fetch, url: str):
    try:
        return fetch(url)
    except (OSError, ValueError, urllib.error.URLError) as error:
        raise GateFailure(f"GET {redact_url(url)} failed: {error}") from None


def redact_url(url: str) -> str:
    return re.sub(r"//[^/@]*@", "//***@", url)


# --- preflight -------------------------------------------------------------------
def source_flow(sources: list, source_id) -> str | None:
    for source in sources or []:
        if source.get("id") == source_id:
            return source.get("flow_id")
    return None


def operator_exclusion(plan: Plan, fetch) -> dict:
    """Read-only: refuse when the selected flow is on air for the operator."""

    a = plan.a
    state = fetch_json(fetch, a.operator_core_url.rstrip("/") + f"{API}/state")
    sources = fetch_json(fetch, a.operator_core_url.rstrip("/") + f"{API}/sources")
    sources = sources.get("sources", sources) if isinstance(sources, dict) else sources
    program_flow = source_flow(sources, state.get("program"))
    preview_flow = source_flow(sources, state.get("preview"))
    if program_flow is None or preview_flow is None:
        raise Refused("cannot resolve the operator Core's Program/Preview flows; refusing")
    if a.selected_flow == program_flow:
        raise Refused("the selected output is the operator Core's Program source")
    if a.selected_flow == preview_flow and not a.allow_operator_preview:
        raise Refused("the selected output is on the operator Preview monitor")
    audio = fetch_json(fetch, a.program_audio_url.rstrip("/") + "/state")
    channels = audio.get("channels") or {}
    by_flow = [n for n, c in channels.items() if c.get("flow_id") == a.selected_audio_flow] \
        if a.selected_audio_flow else []
    numbers = by_flow or ([str(a.selected_audio_channel)] if a.selected_audio_channel else [])
    if not numbers:
        raise Refused("identify the selected output's Program Audio input with --selected-audio-flow "
                      "(index mixer) or --selected-audio-channel; refusing without it")
    for number in numbers:
        channel = channels.get(number)
        if channel is None:
            raise Refused(f"Program Audio has no channel {number}")
        if channel.get("active"):
            raise Refused(f"the selected output is active in Program Audio (channel {number})")
    return {"operator_program_flow_differs": True, "operator_preview_flow_differs": a.selected_flow != preview_flow,
            "program_audio_channels_checked": numbers, "program_audio_mode": audio.get("mode")}


def preflight(plan: Plan, docker: Docker, fetch, port_check=port_free) -> dict:
    a = plan.a
    tools = Path(plan.tools_dir)
    missing = [name for name in REQUIRED_TOOLS if not (tools / name).is_file()]
    if missing:
        raise Refused(f"tools dir lacks {', '.join(missing)}")
    if Path(plan.evidence_dir).exists():
        raise Refused("evidence dir already exists; evidence is never overwritten")
    if a.domain_path and not Path(a.domain_path).is_dir():
        raise Refused("--domain-path is not a directory")
    if docker([*plan.docker, "version", "--format", "{{.Server.Version}}"])[0] != 0:
        raise Refused("docker is not usable")
    for image in {a.image, a.probe_image}:
        if docker([*plan.docker, "image", "inspect", image])[0] != 0:
            raise Refused(f"image {image} is not present; the gate never pulls or builds")
    for name in [*plan.names.values(), plan.helper]:
        if docker([*plan.docker, "container", "inspect", name])[0] == 0:
            raise Refused(f"container {name} already exists")
    if not port_check("127.0.0.1", a.host_port):
        raise Refused(f"host port {a.host_port} is in use")
    permanent = {}
    for name in a.permanent:
        status, out, _ = docker([*plan.docker, "container", "inspect", name])
        try:
            info = json.loads(out)[0] if status == 0 else None
        except (ValueError, IndexError):
            info = None
        if not info or not (info.get("State") or {}).get("Running"):
            raise Refused(f"permanent service {name} is not running")
        permanent[name] = {"restart_count": info.get("RestartCount"),
                           "started_at": (info.get("State") or {}).get("StartedAt")}
    exclusion = operator_exclusion(plan, fetch)
    return {"permanent_services": permanent, "operator_exclusion": exclusion}


def domain_check(plan: Plan, docker: Docker, allow_container: bool) -> dict:
    """Sources must exist and candidate outputs must not (no second writer).

    With ``--domain-path`` this reads the host directory. With a volume it needs
    a one-shot read-only helper container, which only ``--apply`` may start.
    """

    a = plan.a
    flows = [a.selected_flow, a.control_flow, *plan.outputs.values()]
    if a.domain_path:
        present = {flow: (Path(a.domain_path) / f"{flow}.mxl-flow").is_dir() for flow in flows}
    elif not allow_container:
        return {"deferred": "volume domain: checked by a read-only helper container at --apply"}
    else:
        status, out, error = docker(plan.domain_check())
        try:
            present = json.loads(out.strip().splitlines()[-1])["present"]
        except (ValueError, IndexError, KeyError):
            raise Refused(f"domain check failed ({status}): {error.strip()[-200:]}") from None
    for flow, role in ((a.selected_flow, "selected"), (a.control_flow, "control")):
        if not present.get(flow):
            raise Refused(f"the {role} source flow is not in the domain")
    for role, flow in plan.outputs.items():
        if present.get(flow):
            raise Refused(f"candidate {role} output already exists in the domain (second writer)")
    return present


# --- sampling ----------------------------------------------------------------------
def last_json_line(text: str) -> dict | None:
    for line in reversed(text.strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def container_state(docker: Docker, plan: Plan, name: str) -> dict | None:
    status, out, _ = docker([*plan.docker, "container", "inspect", name])
    try:
        info = json.loads(out)[0] if status == 0 else None
    except (ValueError, IndexError):
        return None
    if not info:
        return None
    state = info.get("State") or {}
    return {"running": state.get("Running"), "exit_code": state.get("ExitCode"),
            "started_at": state.get("StartedAt"), "restart_count": info.get("RestartCount"),
            "label": ((info.get("Config") or {}).get("Labels") or {}).get(LABEL)}


def selected_view(health: dict, flow: str) -> dict:
    for source in health.get("sources") or []:
        if source.get("flow_id") == flow:
            return source
    return {}


class Observer:
    """Bounded phase machine. All I/O is injected so tests drive it offline."""

    def __init__(self, plan: Plan, docker: Docker, fetch, clock=time.monotonic, sleep=time.sleep,
                 say=lambda text: print(text, file=sys.stderr, flush=True)) -> None:
        self.plan, self.docker, self.fetch = plan, docker, fetch
        self.clock, self.sleep, self.say = clock, sleep, say
        self.samples: list[dict] = []
        self.events: list[dict] = []
        self.created: list[str] = []
        self.flows_created = False
        self.baseline: dict | None = None
        self.marks: dict[str, float] = {}
        self.preflight_result: dict = {}
        self.deadline = None
        self.stop_requested = False

    # infrastructure
    def event(self, name: str, **detail) -> None:
        self.events.append({"t": round(self.clock(), 3), "event": name, **detail})

    def mark(self, name: str) -> None:
        self.marks[name] = round(self.clock(), 3)
        self.event(name)

    def must(self, step: str, command: list[str]) -> str:
        status, out, error = self.docker(command)
        self.event(step, status=status)
        if status != 0:
            raise GateFailure(f"{step} failed ({status}): {error.strip()[-200:]}")
        return out

    def check(self) -> None:
        if self.stop_requested:
            raise Interrupted("stopped by signal")
        if self.clock() > self.deadline:
            raise GateFailure(f"total bound of {self.plan.max_total_seconds}s exceeded")

    def pause(self) -> None:
        self.sleep(self.plan.a.interval)
        self.check()

    def sample(self, phase: str) -> dict:
        self.check()
        plan = self.plan
        health = fetch_json(self.fetch, plan.core_url + "/health")
        state = fetch_json(self.fetch, plan.core_url + "/state")
        sample = {"t": round(self.clock(), 3), "phase": phase, "health": health,
                  "program": state.get("program"), "preview": state.get("preview"),
                  "selected": selected_view(health, plan.a.selected_flow),
                  "containers": {}, "probes": {}}
        for key, name in plan.names.items():
            sample["containers"][key] = container_state(self.docker, plan, name)
        for role in ("program", "preview"):
            status, out, err = self.docker([*plan.docker, "logs", "--tail", "5", plan.names[f"probe_{role}"]])
            sample["probes"][role] = last_json_line(out + err) if status == 0 else None
        self.samples.append(sample)
        self.invariants(sample)
        return sample

    def invariants(self, sample: dict) -> None:
        for key, state in sample["containers"].items():
            if not state or not state["running"]:
                raise GateFailure(f"{key} is not running (exit {None if not state else state['exit_code']})")
            if state["restart_count"]:
                raise GateFailure(f"{key} restarted")
        for role, probe in sample["probes"].items():
            if probe is None or "frames" not in probe:
                if self.baseline:  # probes report once per second; only a missing report after baseline fails
                    raise GateFailure(f"{role} probe has no report")
                continue
            if self.baseline and probe.get("drops", 0) > self.baseline["probes"][role].get("drops", 0):
                raise GateFailure(f"{role} probe dropped frames")
            if self.baseline and probe.get("stalls", 0) > self.baseline["probes"][role].get("stalls", 0):
                raise GateFailure(f"{role} probe stalled")
        if self.baseline:
            for counter in MUST_NOT_MOVE:
                if sample["health"].get(counter, 0) != self.baseline["health"].get(counter, 0):
                    raise GateFailure(f"{counter} moved")
        if sample["program"] != self.plan.a.selected_source or sample["preview"] != self.plan.a.control_source:
            raise GateFailure("candidate Program/Preview no longer selected/control")

    def wait(self, phase: str, timeout: float, predicate, reason: str) -> dict:
        end = self.clock() + timeout
        while True:
            sample = self.sample(phase)
            if predicate(sample):
                return sample
            if self.clock() >= end:
                raise GateFailure(f"{phase}: {reason} not reached within {timeout:g}s")
            self.pause()

    # phases
    def start(self) -> None:
        plan = self.plan
        config = Path(plan.config_dir)
        (config / "definitions").mkdir(parents=True)
        (config / "core.conf").write_text(plan.core_config())
        for role, group in (("program", "MXLRecoveryGateProgram"), ("preview", "MXLRecoveryGatePreview")):
            (config / "definitions" / f"{role}.json").write_text(
                json.dumps(definition(plan.outputs[role], role, group), indent=2))
        self.created.append(plan.names["core"])
        self.flows_created = True
        self.must("start_core", plan.core())
        end = self.clock() + plan.a.start_timeout
        while True:
            self.check()
            try:
                state = self.fetch(plan.core_url + "/state")
                if state.get("program") == plan.a.selected_source and state.get("preview") == plan.a.control_source:
                    break
                raise GateFailure("candidate did not start with selected on Program and control on Preview")
            except (OSError, ValueError, urllib.error.URLError):
                if self.clock() >= end:
                    raise GateFailure("candidate control API not ready within start timeout") from None
                self.pause()
        self.event("candidate_routing_confirmed", program=plan.a.selected_source, preview=plan.a.control_source)
        for role in ("program", "preview"):
            self.created.append(plan.names[f"probe_{role}"])
            self.must(f"start_probe_{role}", plan.probe(role))

    def establish_baseline(self) -> None:
        a = self.plan.a
        end = self.clock() + a.start_timeout + 3 * a.baseline_seconds
        window: list[dict] = []
        while True:
            sample = self.sample("baseline")
            window.append(sample)
            window = [s for s in window if s["t"] >= sample["t"] - a.baseline_seconds]
            if self.stable(window, a.baseline_seconds):
                self.baseline = sample
                self.mark("baseline")
                return
            if self.clock() >= end:
                raise GateFailure("baseline: probes never sustained ~30 fps with zero drops and quiet counters")
            self.pause()

    @staticmethod
    def stable(window: list[dict], seconds: float) -> bool:
        if len(window) < 2 or window[-1]["t"] - window[0]["t"] < seconds * 0.9:
            return False
        first, last = window[0], window[-1]
        elapsed = last["t"] - first["t"]
        for role in ("program", "preview"):
            p0, p1 = first["probes"].get(role), last["probes"].get(role)
            if not p0 or not p1 or "frames" not in p0 or "frames" not in p1:
                return False
            fps = (p1["frames"] - p0["frames"]) / elapsed
            if not 29.0 <= fps <= 31.0 or p1["drops"] != p0["drops"] or p1["stalls"] != p0["stalls"]:
                return False
        for counter in ("presentation_fallbacks", "selected_input_skip_events", "held"):
            if last["health"].get(counter) != first["health"].get(counter):
                return False
        return True

    def await_interruption(self) -> None:
        a = self.plan.a
        self.say(f"[{self.plan.run_id}] BASELINE OK. Operator: interrupt the selected ZoomISO output now "
                 f"(source {a.selected_source}). Waiting up to {a.interruption_timeout:g}s.")
        self.mark("awaiting_interruption")
        frozen_for = max(1, round(a.freeze_seconds / a.interval))
        heads: list = []

        def frozen(sample):
            heads.append(sample["selected"].get("latest_head_index"))
            reopened = sample["selected"].get("reader_reopens", 0) > self.baseline["selected"].get("reader_reopens", 0)
            return reopened or (len(heads) > frozen_for and len(set(heads[-(frozen_for + 1):])) == 1)

        self.wait("awaiting_interruption", a.interruption_timeout, frozen, "a frozen selected head or reader reopen")
        self.mark("interruption_observed")
        self.interruption_head = heads[-1]

    def await_restoration(self) -> None:
        a = self.plan.a
        self.say(f"[{self.plan.run_id}] INTERRUPTION OBSERVED. Operator: hold it absent until the alarm/hold "
                 f"is visible, then restore the exact original assignment. Waiting up to {a.restoration_timeout:g}s.")
        heads: list = []

        def advancing(sample):
            head = sample["selected"].get("latest_head_index")
            heads.append(head)
            return len(heads) >= 3 and heads[-1] != self.interruption_head and heads[-1] > heads[-2] > heads[-3]

        self.wait("awaiting_restoration", a.restoration_timeout, advancing, "the selected head advancing again")
        self.mark("recovery_observed")

    def await_recovery(self) -> None:
        a = self.plan.a
        limit = a.input_delay_frames + 1
        previous: list = []

        def recovered(sample):
            previous.append(sample)
            if len(previous) < 2:
                return False
            before, now = previous[-2]["health"], sample["health"]
            lag = sample["selected"].get("latest_input_lag_frames")
            return (lag is not None and lag <= limit
                    and now.get("presentation_fallbacks") == before.get("presentation_fallbacks")
                    and now.get("sustained_unavailable_active", 0) == 0)

        self.wait("recovery_observed", a.recovery_timeout, recovered, "bounded lag, no fallback, alarm clear")
        self.mark("fallback_exit")

    def stable_hold(self) -> None:
        a = self.plan.a
        start = self.sample("stable_hold")
        end = self.clock() + a.stable_seconds
        while self.clock() < end:
            self.pause()
            sample = self.sample("stable_hold")
            for counter in ("presentation_fallbacks", "presentation_fallback_entries"):
                if sample["health"].get(counter) != start["health"].get(counter):
                    raise GateFailure(f"stable_hold: {counter} moved")
            lag = sample["selected"].get("latest_input_lag_frames")
            if lag is None or lag > a.input_delay_frames + 1:
                raise GateFailure(f"stable_hold: selected lag {lag} exceeds {a.input_delay_frames + 1}")
        self.mark("stable_recovery")

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
            self.establish_baseline()
            self.await_interruption()
            self.await_restoration()
            self.await_recovery()
            self.stable_hold()
        except (GateFailure, Interrupted) as error:
            failure = f"{type(error).__name__}: {error}"
            self.event("failure", reason=failure)
        finally:
            for signum, handler in handlers.items():
                signal.signal(signum, handler)
        verdict = evaluate(self.samples, self.baseline, self.plan.a, failure)
        verdict["permanent_services"] = self.permanent_check()
        if not verdict["permanent_services"]["unchanged"]:
            verdict["verdict"] = "FAIL"
        verdict["run_id"] = self.plan.run_id
        verdict["marks"] = self.marks
        verdict["evidence"] = self.preserve()
        verdict["cleanup"] = self.cleanup()
        self.write("verdict.json", verdict)
        self.write("public-summary.json", public_summary(verdict, self.plan))
        return verdict

    def _on_signal(self, *_args) -> None:
        self.stop_requested = True

    def permanent_check(self) -> dict:
        before = self.preflight_result.get("permanent_services", {})
        now, changed = {}, []
        for name, original in before.items():
            state = container_state(self.docker, self.plan, name)
            now[name] = state
            if not state or not state["running"] or state["restart_count"] != original["restart_count"] \
                    or state["started_at"] != original["started_at"]:
                changed.append(name)
        return {"unchanged": not changed, "changed": changed}

    def write(self, name: str, value) -> None:
        Path(self.plan.evidence_dir).mkdir(parents=True, exist_ok=True)
        (Path(self.plan.evidence_dir) / name).write_text(json.dumps(value, indent=2) + "\n")

    def preserve(self) -> list[str]:
        directory = Path(self.plan.evidence_dir)
        directory.mkdir(parents=True, exist_ok=True)
        for name in self.created:
            status, out, err = self.docker([*self.plan.docker, "logs", name])
            (directory / f"{name}.log").write_text(out + err)
        with open(directory / "samples.jsonl", "w") as handle:
            for sample in self.samples:
                handle.write(json.dumps(sample, separators=(",", ":")) + "\n")
        self.write("events.json", self.events)
        self.write("plan.json", self.plan.as_dict())
        self.write("preflight.json", self.preflight_result)
        return sorted(p.name for p in directory.iterdir())

    def cleanup(self) -> dict:
        removed, kept = [], []
        for name in self.created:
            state = container_state(self.docker, self.plan, name)
            if state is None:
                continue
            if state["label"] != self.plan.run_id:
                kept.append(f"{name}: label is not {self.plan.run_id}; left in place")
                continue
            status, _, error = self.docker([*self.plan.docker, "rm", "-f", name])
            (removed if status == 0 else kept).append(name if status == 0 else f"{name}: {error.strip()}")
        flows = None
        core_gone = container_state(self.docker, self.plan, self.plan.names["core"]) is None
        if self.flows_created and core_gone:
            status, out, error = self.docker(self.plan.remove_flows())
            flows = last_json_line(out) if status == 0 else {"error": error.strip()[-200:]}
        elif self.flows_created:
            flows = {"error": "candidate core still present; output flows left in place"}
        result = {"removed": removed, "not_removed": kept, "candidate_flows": flows}
        self.write("cleanup.json", result)
        return result


# --- verdict -----------------------------------------------------------------------
def delta(baseline: dict, final: dict, key: str) -> int:
    return (final.get(key) or 0) - (baseline.get(key) or 0)


def evaluate(samples: list[dict], baseline: dict | None, a, failure: str | None) -> dict:
    """Pure: baseline → final counter deltas, signature match, and the pass criteria."""

    checks: dict[str, dict] = {}

    def check(name, ok, detail):
        checks[name] = {"pass": bool(ok), "detail": detail}

    check("sequence_completed", failure is None, failure or "all phases reached")
    if not baseline or not samples:
        check("baseline_established", False, "no baseline")
        return {"verdict": "FAIL", "checks": checks, "signature": None, "deltas": {}}
    final = samples[-1]
    h0, h1 = baseline["health"], final["health"]
    s0, s1 = baseline["selected"], final["selected"]
    deltas = {key: delta(h0, h1, key) for key in (*MUST_NOT_MOVE, *OUTAGE_MEASUREMENTS, *SIGNATURE_COUNTERS)}
    deltas["selected_reader_reopens"] = delta(s0, s1, "reader_reopens")
    check("must_not_move", all(deltas[key] == 0 for key in MUST_NOT_MOVE),
          {key: deltas[key] for key in MUST_NOT_MOVE})
    check("no_synthetic_skip", deltas["selected_input_skipped_grains"] < SYNTHETIC_SKIP_LIMIT,
          deltas["selected_input_skipped_grains"])
    check("alarm_clear_at_end", h1.get("sustained_unavailable_active", 0) == 0,
          h1.get("sustained_unavailable_active"))
    lag = s1.get("latest_input_lag_frames")
    check("final_lag_bounded", lag is not None and lag <= a.input_delay_frames + 1,
          {"lag": lag, "limit": a.input_delay_frames + 1})
    probes = {}
    for role in ("program", "preview"):
        p0, p1 = baseline["probes"][role], final["probes"][role]
        elapsed = max(1e-9, final["t"] - baseline["t"])
        probes[role] = {"frames": p1["frames"] - p0["frames"], "drops": p1["drops"] - p0["drops"],
                        "stalls": p1["stalls"] - p0["stalls"],
                        "fps": round((p1["frames"] - p0["frames"]) / elapsed, 2),
                        "longest_gap_ms": p1.get("longest_gap_ms")}
    check("probes_clean", all(p["drops"] == 0 and p["stalls"] == 0 for p in probes.values()), probes)

    # Measured outage in output frames: the Program holds while the selected source is absent.
    outage_frames = deltas["held"]
    recreated = (deltas["selected_reader_reopens"] >= 1 and deltas["input_timeline_resets"] >= 1
                 and deltas["selected_input_skip_events"] == 0 and deltas["sustained_unavailable_alarms"] >= 1)
    continued = (deltas["selected_reader_reopens"] == 0 and deltas["sustained_unavailable_alarms"] == 0
                 and deltas["selected_input_skip_events"] == 1
                 and abs(deltas["selected_input_skipped_grains"] - outage_frames) <= 1)
    signature = "destroyed_recreated" if recreated and not continued else \
        "stopped_continued" if continued and not recreated else None
    check("exactly_one_recovery_signature", signature is not None,
          {"destroyed_recreated": recreated, "stopped_continued": continued, "outage_frames": outage_frames,
           "skip_events": deltas["selected_input_skip_events"],
           "skipped_grains": deltas["selected_input_skipped_grains"],
           "reader_reopens": deltas["selected_reader_reopens"],
           "alarms": deltas["sustained_unavailable_alarms"]})
    return {"verdict": "PASS" if all(c["pass"] for c in checks.values()) else "FAIL",
            "checks": checks, "signature": signature, "deltas": deltas, "probes": probes,
            "outage_frames": outage_frames}


PRIVATE_KEYS = {"flow", "flow_id", "selected_flow", "control_flow", "candidate_outputs", "domain_mount",
                "core_url", "operator_core_url", "program_audio_url", "evidence", "dir"}


def public_summary(verdict: dict, plan: Plan) -> dict:
    """Aggregates only: no flow ids, paths, addresses, or source names."""

    secrets = {plan.a.selected_flow, plan.a.control_flow, *plan.outputs.values(), plan.evidence_dir,
               plan.a.selected_source, plan.a.control_source}

    def scrub(value):
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items() if k not in PRIVATE_KEYS}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        if isinstance(value, str):
            for secret in secrets:
                if secret:
                    value = value.replace(secret, "<redacted>")
            return value
        return value

    keep = {k: verdict.get(k) for k in ("verdict", "signature", "checks", "deltas", "probes", "outage_frames")}
    return scrub(keep)


# --- in-container roles --------------------------------------------------------------
RUNNING = True


def _stop(*_args) -> None:
    global RUNNING
    RUNNING = False


def run_probe(args) -> int:
    """Read one output flow by index; print cumulative counts each second."""

    import mxl_direct as mxl

    signal.signal(signal.SIGTERM, _stop)
    if not FLOW_ID.match(args.flow):
        print(json.dumps({"error": "flow id is not a UUID"}), flush=True)
        return 2
    deadline = time.monotonic() + args.wait_seconds
    while not mxl.flow_exists(args.domain, args.flow):
        if time.monotonic() > deadline:
            print(json.dumps({"error": "output flow never appeared"}), flush=True)
            return 1
        time.sleep(0.2)
    api = mxl.load_library(args.library)
    instance = api.mxlCreateInstance(args.domain.encode(), b"")
    probe = mxl.Reader(api, instance, args.flow)
    grains = probe.config().discrete.grainCount
    probe.close()
    reader = mxl.GrainReader(api, instance, args.flow, grains)
    counts = {"frames": 0, "drops": 0, "stalls": 0, "torn": 0, "longest_gap_ms": 0.0}
    index = reader.head()
    while not index:
        time.sleep(0.01)
        index = reader.head()
    last_frame = time.monotonic()
    next_report = last_frame + 1.0
    stall_ms = args.stall_ms
    while RUNNING:
        result = reader.read(index, int(stall_ms * 1e6), lambda address, size: True)
        now = time.monotonic()
        if result.status == mxl.MXL_STATUS_OK:
            gap = (now - last_frame) * 1000
            counts["longest_gap_ms"] = round(max(counts["longest_gap_ms"], gap), 1)
            counts["frames"] += 1
            counts["torn"] += int(result.torn)
            last_frame = now
            index += 1
        elif result.status == mxl.MXL_ERR_TIMEOUT:
            counts["stalls"] += 1  # no grain within stall_ms: the output did not advance
        elif result.status == mxl.MXL_ERR_OUT_OF_RANGE_TOO_LATE:
            head = reader.head() or index
            resume = max(index + 1, head - grains + 2)
            counts["drops"] += resume - index
            index = resume
        else:
            counts["drops"] += 1
            index += 1
        if now >= next_report:
            print(json.dumps({"t": round(time.time(), 3), "index": index, **counts}), flush=True)
            next_report = now + 1.0
    reader.close()
    api.mxlDestroyInstance(instance)
    return 0


def frame_signature(address: int, size: int, samples: int = 64, width: int = 64) -> bytes:
    """A cheap content fingerprint: ``samples`` evenly spaced ``width``-byte windows of the grain."""

    import ctypes

    if size <= width:
        return ctypes.string_at(address, size)
    step = (size - width) // max(1, samples - 1)
    return b"".join(ctypes.string_at(address + i * step, width) for i in range(samples))


def run_motion(args) -> int:
    """Prove changing content: count frame-to-frame changes over ``--seconds``.

    An advancing MXL head is not live content. ZoomISO keeps publishing cadence
    while an output is unassigned or a meeting is between restarts, so the
    gate measures how often consecutive grains actually differ.
    """

    import mxl_direct as mxl

    if not FLOW_ID.match(args.flow):
        print(json.dumps({"error": "flow id is not a UUID"}))
        return 2
    if not mxl.flow_exists(args.domain, args.flow):
        print(json.dumps({"flow": args.flow, "error": "flow is not in the domain"}))
        return 1
    api = mxl.load_library(args.library)
    instance = api.mxlCreateInstance(args.domain.encode(), b"")
    probe = mxl.Reader(api, instance, args.flow)
    grains = probe.config().discrete.grainCount
    probe.close()
    reader = mxl.GrainReader(api, instance, args.flow, grains)
    index = reader.head() or 0
    frames = changes = 0
    previous = None
    started = time.monotonic()
    while time.monotonic() - started < args.seconds:
        result = reader.read(index, 200_000_000, frame_signature)
        if result.status == mxl.MXL_STATUS_OK and result.value is not None:
            frames += 1
            changes += int(previous is not None and result.value != previous)
            previous = result.value
            index += 1
        elif result.status == mxl.MXL_ERR_OUT_OF_RANGE_TOO_LATE:
            index = reader.head() or index + 1
        elif result.status != mxl.MXL_ERR_TIMEOUT:
            index += 1
    elapsed = time.monotonic() - started
    reader.close()
    api.mxlDestroyInstance(instance)
    print(json.dumps({"flow": args.flow, "frames": frames, "changes": changes, "seconds": round(elapsed, 3),
                      "change_fps": round(changes / elapsed, 2) if elapsed else 0.0}))
    return 0


def run_domain_check(args) -> int:
    present = {flow: os.path.isdir(os.path.join(args.domain, f"{flow}.mxl-flow")) for flow in args.flow
               if FLOW_ID.match(flow)}
    print(json.dumps({"present": present}))
    return 0


def run_remove_flows(args) -> int:
    removed = []
    for flow in args.flow:
        if not FLOW_ID.match(flow):
            continue
        path = os.path.join(args.domain, f"{flow}.mxl-flow")
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
            removed.append(flow)
    print(json.dumps({"removed": removed}))
    return 0


# --- command line ----------------------------------------------------------------------
def add_plan_arguments(p: argparse.ArgumentParser) -> None:
    """Options shared by every isolated-candidate gate (this one and the flow-rotation gate)."""

    p.add_argument("--image", required=True, help="immutable candidate Core image")
    p.add_argument("--probe-image", required=True, help="local image with python3 and libmxl")
    p.add_argument("--library", default="libmxl.so")
    p.add_argument("--domain-path")
    p.add_argument("--domain-volume")
    p.add_argument("--probe-readonly", action=argparse.BooleanOptionalAction, default=True,
                   help="mount the domain read-only in probes (default)")
    p.add_argument("--selected-source", required=True, help="candidate source id for the interrupted output")
    p.add_argument("--selected-flow", required=True, help="video flow id of the interrupted ZoomISO output")
    p.add_argument("--selected-audio-flow", help="its audio flow id (index mixer state exposes flow ids)")
    p.add_argument("--selected-audio-channel", type=int, help="its Program Audio channel number")
    p.add_argument("--control-source", required=True)
    p.add_argument("--control-flow", required=True)
    p.add_argument("--operator-core-url", required=True, help="read-only; e.g. http://127.0.0.1:<port>")
    p.add_argument("--program-audio-url", required=True, help="read-only Program Audio control URL")
    p.add_argument("--allow-operator-preview", action="store_true",
                   help="permit a source on the operator Preview (only if it is not a required monitor)")
    p.add_argument("--permanent", action="append", default=[], help="permanent container to watch (repeat)")
    p.add_argument("--host-port", type=int, required=True, help="unused loopback port for the candidate")
    p.add_argument("--forbid-port", type=int, action="append", default=[], help="declared permanent ports")
    p.add_argument("--input-delay-frames", type=int, default=1)
    p.add_argument("--tools-dir", default=str(Path(__file__).resolve().parent))
    p.add_argument("--run-id")
    p.add_argument("--evidence-dir")
    p.add_argument("--docker", default="docker")
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--start-timeout", type=float, default=60.0)
    p.add_argument("--baseline-seconds", type=float, default=20.0)
    p.add_argument("--freeze-seconds", type=float, default=1.0)
    p.add_argument("--interruption-timeout", type=float, default=300.0)
    p.add_argument("--restoration-timeout", type=float, default=600.0)
    p.add_argument("--recovery-timeout", type=float, default=120.0)
    p.add_argument("--stable-seconds", type=float, default=30.0)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the plan; change nothing (default)")
    mode.add_argument("--apply", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command")
    add_plan_arguments(sub.add_parser("plan", help="plan (default) or, with --apply, observe the gate"))

    probe = sub.add_parser("probe")
    probe.add_argument("--domain", required=True)
    probe.add_argument("--flow", required=True)
    probe.add_argument("--library", default="libmxl.so")
    probe.add_argument("--stall-ms", type=float, default=200.0)
    probe.add_argument("--wait-seconds", type=float, default=30.0)
    motion = sub.add_parser("motion")
    motion.add_argument("--domain", required=True)
    motion.add_argument("--flow", required=True)
    motion.add_argument("--seconds", type=float, default=5.0)
    motion.add_argument("--library", default="libmxl.so")
    for name in ("domain-check", "remove-flows"):
        helper = sub.add_parser(name)
        helper.add_argument("--domain", required=True)
        helper.add_argument("--flow", action="append", default=[])
        helper.add_argument("--library", default="libmxl.so")
    return parser


def main(argv=None, docker: Docker | None = None, fetch=http_get, port_check=port_free,
         clock=time.monotonic, sleep=time.sleep, say=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "probe":
        return run_probe(args)
    if args.command == "domain-check":
        return run_domain_check(args)
    if args.command == "motion":
        return run_motion(args)
    if args.command == "remove-flows":
        return run_remove_flows(args)
    if args.command != "plan":
        build_parser().print_help()
        return 2
    docker = docker or Docker()
    try:
        plan = Plan(args, args.run_id or new_run_id())
        checked = preflight(plan, docker, fetch, port_check)
        checked["domain"] = domain_check(plan, docker, allow_container=args.apply)
    except (Refused, GateFailure) as error:
        print(json.dumps({"refused": str(error)}), file=sys.stderr)
        return 2
    if not args.apply:
        print(json.dumps({"mode": "dry_run", "preflight": checked, "plan": plan.as_dict()}, indent=2))
        return 0
    observer = Observer(plan, docker, fetch, clock=clock, sleep=sleep,
                        **({"say": say} if say else {}))
    observer.preflight_result = checked
    verdict = observer.run()
    print(json.dumps({k: verdict[k] for k in ("verdict", "signature", "checks")}, indent=2))
    return 0 if verdict["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
