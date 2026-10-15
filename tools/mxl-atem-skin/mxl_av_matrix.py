#!/usr/bin/env python3
"""Qualify a matrix of Program-return captures with the A/V reference analyzer.

Offline only. The runner reads a JSON manifest that names existing
audience-side media captures and the Program-return configuration used for
each. It runs ``mxl_av_analyze.py --media`` once per case and writes one
machine-readable qualification report. It never records, joins, or touches a
meeting, a container, or the network.

Without ``--apply`` it prints the plan: every case, its capture, and the exact
analyzer command. Nothing is read beyond file metadata, and nothing is
written. With ``--apply --result-dir DIR`` it creates exactly ``DIR`` (which
must not exist; its parent must), hashes each capture, runs the analyzer, and
writes ``report.json`` plus each case's raw analyzer output.

A stage is never satisfied by a short capture. The time a case covers is read
from the reference itself: the sum of the decoded grain-code steps divided by
the reference frame rate. A file that is long but frozen, or cut short, covers
less than the stage requires and fails.

Exit status: 0 when every required case passes, 1 when any fails, 2 when the
manifest or arguments are refused (nothing written).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ANALYZER = HERE / "mxl_av_analyze.py"
REFERENCE_RATE = 30  # av_reference.VIDEO_RATE: grain codes advance 30 per second
SCHEMA = "mxl-av-matrix/1"
STAGES = {"smoke": 60, "10min": 600, "30min": 1800, "8h": 28_800}
CASE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# Analyzer limits: manifest key -> (flag, kind, minimum, disable value or None)
LIMITS = {
    "audio_lead_ms": ("--audio-lead-ms", float, 0, None),
    "audio_lag_ms": ("--audio-lag-ms", float, 0, None),
    "max_offset_range_ms": ("--max-offset-range-ms", float, 0, None),
    "max_drift_ppm": ("--max-drift-ppm", float, 0, None),
    "max_undecodable_fraction": ("--max-undecodable-fraction", float, 0, None),
    "max_freeze_ms": ("--max-freeze-ms", float, 0, -1),
    "min_pairs": ("--min-pairs", int, 1, None),
    "max_skipped_grains": ("--max-skipped-grains", int, 0, -1),
    "max_repeats": ("--max-repeats", int, 0, -1),
    "max_unmatched_events": ("--max-unmatched-events", int, 0, None),
}
CHANNELS = {"event_channel": ("--event-channel", ("left", "right", "mix")),
            "counter_channel": ("--counter-channel", ("left", "right"))}
MATRIX_KEYS = {"schema", "matrix", "stage", "defaults", "cases"}
DEFAULT_KEYS = {"limits", "event_channel", "counter_channel", "required"}
CASE_KEYS = {"name", "configuration", "capture", "limits", "event_channel", "counter_channel", "required"}


class Refused(ValueError):
    """The manifest or arguments are unusable; nothing was written."""


# --- manifest ---------------------------------------------------------------------
def _check_keys(where: str, value: dict, allowed: set) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise Refused(f"{where}: unknown key(s) {', '.join(unknown)}")


def validate_limits(where: str, limits) -> dict:
    if not isinstance(limits, dict):
        raise Refused(f"{where}: limits must be an object")
    _check_keys(f"{where} limits", limits, set(LIMITS))
    clean = {}
    for key, value in limits.items():
        _, kind, minimum, disable = LIMITS[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            raise Refused(f"{where}: limit {key} must be a number")
        if kind is int and value != int(value):
            raise Refused(f"{where}: limit {key} must be an integer")
        if disable is not None and value == disable:
            clean[key] = kind(value)
            continue
        if value < minimum:
            hint = f" (or {disable} to disable)" if disable is not None else ""
            raise Refused(f"{where}: limit {key} must be at least {minimum}{hint}")
        if key == "max_undecodable_fraction" and value > 1:
            raise Refused(f"{where}: limit {key} must be a fraction from 0 to 1")
        clean[key] = kind(value)
    return clean


def _channels(where: str, source: dict) -> dict:
    clean = {}
    for key, (_, choices) in CHANNELS.items():
        if key in source:
            if source[key] not in choices:
                raise Refused(f"{where}: {key} must be one of {', '.join(choices)}")
            clean[key] = source[key]
    return clean


def load_manifest(path: str, stage_override: str | None = None) -> dict:
    """Parse and validate a manifest; capture paths resolve relative to it."""

    try:
        with open(path) as handle:
            manifest = json.load(handle)
    except OSError as error:
        raise Refused(f"manifest {path}: {error.strerror}") from None
    except json.JSONDecodeError as error:
        raise Refused(f"manifest {path}: invalid JSON ({error.msg} at line {error.lineno})") from None
    if not isinstance(manifest, dict):
        raise Refused("manifest must be a JSON object")
    _check_keys("manifest", manifest, MATRIX_KEYS)
    if manifest.get("schema", SCHEMA) != SCHEMA:
        raise Refused(f"manifest schema must be {SCHEMA}")
    matrix = manifest.get("matrix")
    if not isinstance(matrix, str) or not matrix.strip():
        raise Refused("manifest needs a non-empty matrix name")
    stage = stage_override or manifest.get("stage")
    if stage not in STAGES:
        raise Refused(f"stage must be one of {', '.join(STAGES)}")
    defaults = manifest.get("defaults", {})
    if not isinstance(defaults, dict):
        raise Refused("defaults must be an object")
    _check_keys("defaults", defaults, DEFAULT_KEYS)
    default_limits = validate_limits("defaults", defaults.get("limits", {}))
    default_channels = _channels("defaults", defaults)
    default_required = defaults.get("required", True)
    if not isinstance(default_required, bool):
        raise Refused("defaults: required must be true or false")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise Refused("the matrix has no cases")
    base = Path(path).resolve().parent
    seen, clean_cases = set(), []
    for index, case in enumerate(cases):
        where = f"case {index + 1}"
        if not isinstance(case, dict):
            raise Refused(f"{where}: must be an object")
        _check_keys(where, case, CASE_KEYS)
        name = case.get("name")
        if not isinstance(name, str) or not CASE_NAME.match(name):
            raise Refused(f"{where}: name must match {CASE_NAME.pattern}")
        where = f"case {name}"
        if name in seen:
            raise Refused(f"duplicate case name {name}")
        seen.add(name)
        configuration = case.get("configuration")
        if not isinstance(configuration, dict) or not isinstance(configuration.get("name"), str) \
                or not configuration["name"].strip():
            raise Refused(f"{where}: configuration must be an object with a name")
        capture = case.get("capture")
        if not isinstance(capture, str) or not capture:
            raise Refused(f"{where}: capture must be a path")
        resolved = (base / capture).resolve()
        if not resolved.is_file():
            raise Refused(f"{where}: capture {capture} does not exist or is not a file")
        required = case.get("required", default_required)
        if not isinstance(required, bool):
            raise Refused(f"{where}: required must be true or false")
        limits = {**default_limits, **validate_limits(where, case.get("limits", {}))}
        clean_cases.append({
            "name": name,
            "configuration": configuration,
            "capture": capture,
            "capture_path": str(resolved),
            "required": required,
            "limits": limits,
            **default_channels,
            **_channels(where, case),
        })
    return {"matrix": matrix, "stage": stage, "minimum_covered_s": STAGES[stage], "cases": clean_cases}


def analyzer_command(case: dict, python: str = sys.executable, analyzer: Path = ANALYZER) -> list[str]:
    command = [python, str(analyzer), "--media", case["capture_path"]]
    for key, value in sorted(case["limits"].items()):
        command += [LIMITS[key][0], str(value)]
    for key, (flag, _) in CHANNELS.items():
        if key in case:
            command += [flag, case[key]]
    return command


# --- evidence -------------------------------------------------------------------
def file_identity(path: str) -> dict:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return {"sha256": digest.hexdigest(), "bytes": size}


def source_revision(root: Path = HERE) -> dict:
    """The analyzer's Git revision and whether its tree is dirty, plus tool hashes."""

    def git(*args):
        try:
            done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    status = git("status", "--porcelain", "--", ".")
    return {
        "git_commit": git("rev-parse", "HEAD"),
        "tools_dirty": None if status is None else bool(status),
        "tools": {name: file_identity(str(root / name))["sha256"]
                  for name in ("mxl_av_matrix.py", "mxl_av_analyze.py", "av_reference.py")
                  if (root / name).is_file()},
    }


def run_analyzer(command: list[str]) -> tuple[int, str, str]:
    done = subprocess.run(command, capture_output=True, text=True)
    return done.returncode, done.stdout, done.stderr


def covered_seconds(report: dict) -> float:
    """Reference time spanned by the capture: forward grain steps / frame rate."""

    histogram = (report.get("video") or {}).get("step_histogram") or {}
    steps = sum(int(step) * count for step, count in histogram.items() if int(step) > 0)
    return round(steps / REFERENCE_RATE, 3)


def evaluate_case(case: dict, status: int, stdout: str, stderr: str, minimum_s: float, stage: str) -> dict:
    result = {"analyzer_exit": status, "failures": []}
    try:
        report = json.loads(stdout) if stdout.strip() else None
    except json.JSONDecodeError:
        report = None
    if status not in (0, 1) or not isinstance(report, dict) or "passed" not in report:
        result.update(verdict="ERROR", report=report,
                      failures=[f"analyzer error (exit {status}): {stderr.strip()[-300:] or 'no report'}"])
        return result
    covered = covered_seconds(report)
    result["report"] = report
    result["covered_s"] = covered
    result["failures"] = list(report.get("failures", []))
    if status == 0 and not report["passed"] or status == 1 and report["passed"]:
        result["failures"].append("analyzer exit status disagrees with its report")
    if covered < minimum_s:
        result["failures"].append(
            f"capture covers {covered:g} s of reference; stage {stage} needs at least {minimum_s} s")
    result["verdict"] = "PASS" if not result["failures"] else "FAIL"
    return result


# --- plan and run ----------------------------------------------------------------
def plan(matrix: dict, python: str = sys.executable) -> dict:
    return {
        "mode": "dry_run",
        "matrix": matrix["matrix"],
        "stage": matrix["stage"],
        "minimum_covered_s": matrix["minimum_covered_s"],
        "cases": [{
            "name": case["name"],
            "configuration": case["configuration"],
            "required": case["required"],
            "capture": case["capture"],
            "capture_bytes": os.path.getsize(case["capture_path"]),
            "limits": case["limits"],
            "analyzer": analyzer_command(case, python),
        } for case in matrix["cases"]],
        "writes": "nothing; add --apply --result-dir DIR to run",
    }


def run(matrix: dict, result_dir: str, executor=run_analyzer, revision=None, python: str = sys.executable,
        clock=time.time) -> dict:
    directory = Path(result_dir)
    directory.mkdir()  # exactly this directory; refuses if it exists or its parent does not
    started = clock()
    cases = []
    for case in matrix["cases"]:
        identity = file_identity(case["capture_path"])
        command = analyzer_command(case, python)
        case_started = clock()
        status, stdout, stderr = executor(command)
        (directory / f"{case['name']}.analyzer.json").write_text(stdout)
        if stderr:
            (directory / f"{case['name']}.analyzer.stderr.txt").write_text(stderr)
        outcome = evaluate_case(case, status, stdout, stderr, matrix["minimum_covered_s"], matrix["stage"])
        cases.append({
            "name": case["name"],
            "configuration": case["configuration"],
            "required": case["required"],
            "capture": {"path": case["capture"], **identity},
            "limits": case["limits"],
            **{key: case[key] for key in CHANNELS if key in case},
            "analyzer_command": command,
            "analyzer_seconds": round(clock() - case_started, 3),
            **outcome,
        })
    required = [case for case in cases if case["required"]]
    failed = [case["name"] for case in required if case["verdict"] != "PASS"]
    configurations: dict[str, dict] = {}
    for case in cases:
        entry = configurations.setdefault(case["configuration"]["name"], {"cases": [], "verdict": "PASS"})
        entry["cases"].append(case["name"])
        if case["verdict"] != "PASS":
            entry["verdict"] = "FAIL"
    report = {
        "schema": SCHEMA,
        "matrix": matrix["matrix"],
        "stage": matrix["stage"],
        "minimum_covered_s": matrix["minimum_covered_s"],
        "verdict": "PASS" if required and not failed else "FAIL",
        "required_failed": failed if required else ["no required cases"],
        "counts": {
            "cases": len(cases), "required": len(required),
            "passed": sum(case["verdict"] == "PASS" for case in cases),
            "failed": sum(case["verdict"] == "FAIL" for case in cases),
            "errors": sum(case["verdict"] == "ERROR" for case in cases),
        },
        "configurations": configurations,
        "source": revision if revision is not None else source_revision(),
        "started_unix": round(started, 3),
        "elapsed_s": round(clock() - started, 3),
        "cases": cases,
    }
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main(argv=None, executor=run_analyzer, revision=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("manifest")
    parser.add_argument("--stage", choices=sorted(STAGES), help="override the manifest stage")
    parser.add_argument("--result-dir", help="new directory for the report; required with --apply")
    parser.add_argument("--apply", action="store_true", help="hash captures, run the analyzer, write the report")
    args = parser.parse_args(argv)
    try:
        matrix = load_manifest(args.manifest, args.stage)
        if args.apply:
            if not args.result_dir:
                raise Refused("--apply needs --result-dir")
            target = Path(args.result_dir)
            if target.exists():
                raise Refused(f"result dir {target} already exists; evidence is never overwritten")
            if not target.resolve().parent.is_dir():
                raise Refused(f"result dir parent {target.resolve().parent} does not exist")
    except Refused as error:
        print(json.dumps({"refused": str(error)}), file=sys.stderr)
        return 2
    if not args.apply:
        print(json.dumps(plan(matrix), indent=2))
        return 0
    report = run(matrix, args.result_dir, executor=executor, revision=revision)
    summary = {key: report[key] for key in ("matrix", "stage", "verdict", "required_failed", "counts")}
    print(json.dumps({**summary, "report": str(Path(args.result_dir) / "report.json")}, indent=2))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
