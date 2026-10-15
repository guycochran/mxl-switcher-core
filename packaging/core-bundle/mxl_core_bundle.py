#!/usr/bin/env python3
"""Immutable MXL Switcher Core deployment bundle: build, verify, preflight, launch, clean up.

Boring on purpose. Every command prints exactly what it will do, the dry run is
the default, and nothing is ever stopped, renamed, replaced, or pruned
implicitly.

- ``tag``: the immutable image reference for a semantic version and a Git revision.
- ``build``: build from a clean checkout, label it, and write a release manifest
  plus a pinned EasyMXL catalog entry.
- ``verify``: check an image's OCI labels against its tag before launch.
- ``launch``: validate a caller-supplied facility config and output definitions,
  refuse an existing container name or an occupied port, refuse two writers to
  one MXL output flow, and print (or, with ``--apply``, run) the exact
  ``docker run``.
- ``cleanup``: remove exactly one named candidate container (and optionally its
  exact image), and only if that container runs the expected image.

Core starts alone. The Skin, EasyMXL recovery UI, Companion, NMOS, Program Audio,
and confidence monitoring stay separate bolt-ons with their own launches.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import shlex
import socket
import subprocess
import sys

REPOSITORY = "mxl-switcher-core"
SOURCE_URL = "https://github.com/guycochran/mxl-switcher-core"
CONFIG_PATH = "/etc/mxl-switcher-core.conf"
DEFINITIONS_PATH = "/etc/mxl-switcher-core/definitions"
BUNDLED_ACCEPTANCE = "/usr/local/share/mxl-switcher-core/acceptance"
DOMAIN_PATH = "/mxl-domain"
CONTAINER_PORT = 18085
LABEL_PREFIX = "org.opencontainers.image."
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z]+(\.[0-9A-Za-z]+)*)?$")
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
SHORT = 12
IMMUTABLE_TAG_RE = re.compile(
    r"^(?P<version>(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z]+(\.[0-9A-Za-z]+)*)?)"
    r"\.g(?P<revision>[0-9a-f]{%d})$" % SHORT)
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]+$")
VOLUME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]+$")


class BundleError(RuntimeError):
    """A validation failed; nothing was changed."""


# --- pure helpers -------------------------------------------------------------


def immutable_tag(version: str, revision: str) -> str:
    """``<semver>.g<12-hex revision>``: unique per commit, valid as a Docker tag."""

    if not SEMVER_RE.match(version or ""):
        raise BundleError(f"version {version!r} is not semantic (MAJOR.MINOR.PATCH[-prerelease])")
    if not REVISION_RE.match(revision or ""):
        raise BundleError("revision must be a full 40-character lowercase Git commit id")
    return f"{version}.g{revision[:SHORT]}"


def image_ref(version: str, revision: str, repository: str = REPOSITORY) -> str:
    return f"{repository}:{immutable_tag(version, revision)}"


def oci_labels(version: str, revision: str, base_name: str, base_digest: str) -> dict[str, str]:
    immutable_tag(version, revision)
    return {
        LABEL_PREFIX + "title": "MXL Switcher Core",
        LABEL_PREFIX + "description": "Deterministic Program and Preview routing for MXL",
        LABEL_PREFIX + "source": SOURCE_URL,
        LABEL_PREFIX + "version": version,
        LABEL_PREFIX + "revision": revision,
        LABEL_PREFIX + "base.name": base_name,
        LABEL_PREFIX + "base.digest": base_digest,
    }


def parse_image_ref(ref: str) -> tuple[str, str | None, str | None]:
    """Split ``repo[:tag][@digest]``; a registry port is not mistaken for a tag."""

    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    slash = ref.rfind("/")
    colon = ref.rfind(":")
    if colon > slash:
        return ref[:colon], ref[colon + 1:], digest
    return ref, None, digest


def check_immutable(ref: str) -> tuple[str, str]:
    """Return (version, short revision) or refuse an ambiguous reference."""

    repository, tag, digest = parse_image_ref(ref)
    if digest:
        raise BundleError("pass the immutable tag; digests are verified from labels, not accepted bare")
    if tag is None or tag in ("latest", "local") or "candidate" == tag:
        raise BundleError(f"image {ref!r} has an ambiguous tag; use <semver>.g<revision>")
    match = IMMUTABLE_TAG_RE.match(tag)
    if not match:
        raise BundleError(f"image {ref!r} is not an immutable <semver>.g<{SHORT}-hex revision> tag")
    return match.group("version"), match.group("revision")


def parse_config(text: str) -> dict:
    """Parse Core's ``key=value`` facility config (mirrors live_config.cpp rules)."""

    values: dict[str, str] = {}
    sources: list[str] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise BundleError(f"config line {number}: expected key=value")
        key, value = (part.strip() for part in line.split("=", 1))
        if not key or not value:
            raise BundleError(f"config line {number}: key and value are required")
        if key == "source":
            sources.append(value)
        elif key in values:
            raise BundleError(f"config line {number}: duplicate key {key}")
        else:
            values[key] = value
    return {"values": values, "sources": sources}


def _definition_host_path(container_path: str, definitions_dir: pathlib.Path | None, mode: str) -> pathlib.Path | None:
    if container_path.startswith(DEFINITIONS_PATH + "/"):
        if definitions_dir is None:
            raise BundleError(f"{container_path} needs --definitions (mounted read-only at {DEFINITIONS_PATH})")
        relative = container_path[len(DEFINITIONS_PATH) + 1:]
        if "/" in relative or relative in ("", ".", ".."):
            raise BundleError(f"output definition {container_path} must be a file directly under {DEFINITIONS_PATH}")
        return definitions_dir / relative
    if container_path.startswith(BUNDLED_ACCEPTANCE + "/"):
        if mode == "production":
            raise BundleError("production launches must supply their own output definitions, not the bundled acceptance set")
        return None  # verified inside the image by the acceptance gates
    raise BundleError(f"output definition {container_path} must live under {DEFINITIONS_PATH}")


def validate_config(config_text: str, definitions_dir: pathlib.Path | None, mode: str) -> dict:
    """Validate a facility config and its Program/Preview definitions. Returns a summary."""

    parsed = parse_config(config_text)
    values = parsed["values"]
    for key in ("domain", "program_definition", "program_id", "preview_definition", "preview_id",
                "initial_program", "initial_preview", "control_host", "control_port"):
        if key not in values:
            raise BundleError(f"config is missing {key}")
    if values["domain"] != DOMAIN_PATH:
        raise BundleError(f"config domain must be {DOMAIN_PATH} (the explicit domain mount inside the container)")
    if values["control_host"] != "0.0.0.0":
        raise BundleError("config control_host must be 0.0.0.0 inside the container; publish the host side on loopback")
    if values["control_port"] != str(CONTAINER_PORT):
        raise BundleError(f"config control_port must be {CONTAINER_PORT} (the health check and catalog use it)")
    if values["program_id"].lower() == values["preview_id"].lower():
        raise BundleError("Program and Preview output ids must be distinct")
    if len(parsed["sources"]) < 2:
        raise BundleError("config needs at least two source entries")
    source_flows = set()
    for entry in parsed["sources"]:
        fields = entry.split("|")
        if len(fields) != 3 or not all(fields):
            raise BundleError("source entries must be id|flow_uuid|label")
        source_flows.add(fields[1].lower())
    if {values["program_id"].lower(), values["preview_id"].lower()} & source_flows:
        raise BundleError("Program/Preview output ids must not reuse a source flow id")

    outputs = {}
    for role in ("program", "preview"):
        container_path = values[f"{role}_definition"]
        host_path = _definition_host_path(container_path, definitions_dir, mode)
        if host_path is not None:
            if not host_path.is_file():
                raise BundleError(f"{role} output definition {host_path.name} does not exist")
            try:
                definition = json.loads(host_path.read_text())
            except (OSError, json.JSONDecodeError) as error:
                raise BundleError(f"{role} output definition is not valid JSON: {error}") from None
            if str(definition.get("id", "")).lower() != values[f"{role}_id"].lower():
                raise BundleError(f"{role} output definition id does not match {role}_id")
            if definition.get("media_type") != "video/v210":
                raise BundleError(f"{role} output definition must be video/v210")
        outputs[role] = {"id": values[f"{role}_id"].lower(), "definition": container_path}
    return {"outputs": outputs, "sources": len(parsed["sources"])}


# --- Docker access (injectable for tests) ---------------------------------------


class Docker:
    """Thin wrapper; read calls are always allowed, mutations only via ``mutate``."""

    def __init__(self, run=subprocess.run, binary: str = "docker") -> None:
        self._run = run
        self.binary = binary

    def read(self, *args: str) -> subprocess.CompletedProcess:
        return self._run([self.binary, *args], capture_output=True, text=True, check=False)

    def mutate(self, *args: str) -> subprocess.CompletedProcess:
        result = self._run([self.binary, *args], capture_output=True, text=True, check=False)
        if result.returncode:
            raise BundleError(f"docker {args[0]} failed: {(result.stderr or result.stdout).strip()[:400]}")
        return result

    def image_labels(self, ref: str) -> dict | None:
        result = self.read("image", "inspect", "--format", "{{json .Config.Labels}}", ref)
        if result.returncode:
            return None
        return json.loads(result.stdout or "null") or {}

    def image_id(self, ref: str) -> str | None:
        result = self.read("image", "inspect", "--format", "{{.Id}}", ref)
        return result.stdout.strip() if result.returncode == 0 else None

    def container(self, name: str) -> dict | None:
        """Exact-name container lookup (no prefix or pattern matching)."""

        result = self.read("container", "inspect", "--format", "{{json .}}", name)
        if result.returncode:
            return None
        data = json.loads(result.stdout or "null")
        if not isinstance(data, dict) or data.get("Name", "").lstrip("/") != name:
            return None
        return data


def port_free(host: str, port: int, connect_timeout: float = 0.5) -> bool:
    """Can a replacement bind ``host:port`` right now?

    Answers the operational question, not "has the port ever been used":

    1. Bind with ``SO_REUSEADDR``, as servers and Docker's port publisher do.
       This succeeds while the stopped listener's old connections sit in TCP
       ``TIME_WAIT`` (which a plain bind wrongly reports as occupied), and still
       fails while a process is listening on the same address.
    2. Connect to the port. If anything accepts, a listener is live. This
       catches a listener on the wildcard address, which a reuse-bind to a
       specific address can miss on BSD/macOS.

    Maintenance replacement therefore does not wait for ``TIME_WAIT`` to expire.
    """

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    target = {"0.0.0.0": "127.0.0.1", "::": "::1", "": "127.0.0.1"}.get(host, host)
    with socket.socket(family, socket.SOCK_STREAM) as client:
        client.settimeout(connect_timeout)
        try:
            client.connect((target, port))
        except OSError:
            return True
    return False


# --- commands ---------------------------------------------------------------


def verify_image(docker: Docker, ref: str, version: str | None = None, revision: str | None = None) -> dict:
    tag_version, tag_revision = check_immutable(ref)
    labels = docker.image_labels(ref)
    if labels is None:
        raise BundleError(f"image {ref} is not present locally")
    label_version = labels.get(LABEL_PREFIX + "version")
    label_revision = labels.get(LABEL_PREFIX + "revision") or ""
    problems = []
    if label_version != tag_version:
        problems.append(f"version label {label_version!r} does not match tag {tag_version!r}")
    if not REVISION_RE.match(label_revision) or label_revision[:SHORT] != tag_revision:
        problems.append("revision label does not match the tag revision")
    if labels.get(LABEL_PREFIX + "source") != SOURCE_URL:
        problems.append("source label is missing or unexpected")
    if not labels.get(LABEL_PREFIX + "base.digest"):
        problems.append("base image digest label is missing")
    if version is not None and version != tag_version:
        problems.append(f"expected version {version}")
    if revision is not None and revision != label_revision:
        problems.append("expected revision does not match the label")
    if problems:
        raise BundleError(f"image {ref} failed label verification: " + "; ".join(problems))
    return {"image": ref, "image_id": docker.image_id(ref), "version": label_version,
            "revision": label_revision, "base": labels.get(LABEL_PREFIX + "base.name"),
            "base_digest": labels.get(LABEL_PREFIX + "base.digest")}


def build_plan(version: str, revision: str, base_image: str, base_digest: str, context: str = ".") -> dict:
    ref = image_ref(version, revision)
    labels = oci_labels(version, revision, base_image, base_digest)
    command = ["build", "--file", "packaging/easy-mxl/Dockerfile.runtime",
               "--build-arg", f"BASE_IMAGE={base_image}",
               "--build-arg", f"VERSION={version}", "--build-arg", f"REVISION={revision}"]
    for key in sorted(labels):
        command += ["--label", f"{key}={labels[key]}"]
    command += ["--tag", ref, context]
    return {"image": ref, "labels": labels, "docker": command}


def pinned_catalog(template: list, ref: str) -> list:
    """The repository's catalog entry with ``image`` pinned to one immutable reference."""

    entries = json.loads(json.dumps(template))
    for entry in entries:
        if entry.get("id") == "mxl-switcher-core":
            entry["image"] = ref
            entry["imagePolicy"] = "local"
            entry["imageRepository"] = parse_image_ref(ref)[0]
    return entries


STOPPED_STATES = ("created", "exited", "dead")


def rollback_outputs(config_path: str) -> dict:
    """Program/Preview output ids from the rollback service's own facility config."""

    path = pathlib.Path(config_path)
    if not path.is_file():
        raise BundleError(f"rollback config {config_path} does not exist")
    try:
        values = parse_config(path.read_text())["values"]
    except (OSError, UnicodeDecodeError) as error:
        raise BundleError(f"rollback config {config_path} cannot be read: {error}") from None
    missing = [key for key in ("program_id", "preview_id") if key not in values]
    if missing:
        raise BundleError(f"rollback config {config_path} is missing {', '.join(missing)}")
    return {"program": values["program_id"].lower(), "preview": values["preview_id"].lower()}


def check_output_collision(candidate: dict, rollback_state: str, rollback: dict | None, config_path: str | None) -> dict:
    """Two writers to one MXL flow are forbidden.

    A *running* rollback (anything not created/exited/dead) requires its own
    config and fully disjoint Program/Preview ids: parallel qualification.
    A *stopped* rollback may share ids: maintenance-window replacement.
    """

    running = rollback_state not in STOPPED_STATES
    if running and rollback is None:
        raise BundleError(f"the rollback container is {rollback_state}; pass --rollback-config so its "
                          "Program/Preview outputs can be proven disjoint from the candidate's")
    overlap = []
    if rollback is not None:
        for candidate_role, candidate_id in sorted(candidate.items()):
            for rollback_role, rollback_id in sorted(rollback.items()):
                if candidate_id == rollback_id:
                    overlap.append({"flow_id": candidate_id, "candidate": candidate_role, "rollback": rollback_role})
    if running and overlap:
        flows = ", ".join(f"{item['flow_id']} (candidate {item['candidate']} = rollback {item['rollback']})"
                          for item in overlap)
        raise BundleError("refusing: the running rollback container already writes " + flows
                          + "; launching would create concurrent writers to the same MXL flow. "
                          "Use disjoint output ids for parallel qualification, or stop the rollback "
                          "container in a maintenance window to replace it")
    return {
        "rollback_state": rollback_state,
        "rollback_config": config_path,
        "candidate_outputs": candidate,
        "rollback_outputs": rollback,
        "overlapping_outputs": overlap,
        "launch_kind": "parallel_disjoint_outputs" if running else "replacement_with_rollback_stopped",
    }


def launch_plan(docker: Docker, args, port_check=port_free) -> dict:
    if not NAME_RE.match(args.name or ""):
        raise BundleError("container --name must match ^[a-zA-Z0-9][a-zA-Z0-9_.-]+$")
    if args.mode == "production":
        check_immutable(args.image)
        identity = verify_image(docker, args.image)
    else:
        identity = verify_image(docker, args.image) if IMMUTABLE_TAG_RE.match(parse_image_ref(args.image)[1] or "") \
            else {"image": args.image, "warning": "test mode: mutable tag accepted, labels not verified"}
    config_path = pathlib.Path(args.config).resolve()
    if not config_path.is_file():
        raise BundleError(f"facility config {args.config} does not exist")
    definitions = pathlib.Path(args.definitions).resolve() if args.definitions else None
    if definitions is not None and not definitions.is_dir():
        raise BundleError(f"definitions directory {args.definitions} does not exist")
    summary = validate_config(config_path.read_text(), definitions, args.mode)

    if bool(args.domain_volume) == bool(args.domain_path):
        raise BundleError("give exactly one explicit MXL domain: --domain-volume NAME or --domain-path /abs/path")
    if args.domain_volume and not VOLUME_RE.match(args.domain_volume):
        raise BundleError("--domain-volume must be a single named volume")
    if args.domain_path and (not args.domain_path.startswith("/") or args.domain_path in ("/", "/dev/shm")):
        raise BundleError("--domain-path must be an absolute, domain-specific directory")
    domain_source = args.domain_volume or args.domain_path

    if docker.container(args.name) is not None:
        raise BundleError(f"a container named {args.name} already exists; this tool never stops, renames, "
                          "or replaces a service. Choose a new candidate name or follow the documented promotion")
    if not port_check(args.bind, args.host_port):
        raise BundleError(f"host port {args.bind}:{args.host_port} is occupied")

    rollback = None
    rollback_config = getattr(args, "rollback_config", None)
    if args.rollback_container:
        current = docker.container(args.rollback_container)
        if current is None:
            raise BundleError(f"rollback container {args.rollback_container} does not exist")
        state = str((current.get("State") or {}).get("Status") or "unknown").lower()
        rollback_ids = rollback_outputs(rollback_config) if rollback_config else None
        candidate_ids = {role: summary["outputs"][role]["id"] for role in ("program", "preview")}
        rollback = {"container": args.rollback_container, "image": current.get("Config", {}).get("Image"),
                    "image_id": current.get("Image"),
                    "action": "left untouched; restart only it if the candidate is withdrawn",
                    **check_output_collision(candidate_ids, state, rollback_ids,
                                             str(pathlib.Path(rollback_config).resolve()) if rollback_config else None)}
    elif rollback_config:
        raise BundleError("--rollback-config needs --rollback-container")
    elif args.mode == "production":
        raise BundleError("production launches must name --rollback-container (the service the candidate would succeed)")

    run = ["run", "--detach", "--name", args.name, "--restart", args.restart,
           "--publish", f"{args.bind}:{args.host_port}:{CONTAINER_PORT}",
           "--mount", f"type={'volume' if args.domain_volume else 'bind'},source={domain_source},target={DOMAIN_PATH}",
           "--mount", f"type=bind,source={config_path},target={CONFIG_PATH},readonly",
           "--label", "io.mxl-switcher-core.bundle.role=core"]
    if definitions is not None:
        run += ["--mount", f"type=bind,source={definitions},target={DEFINITIONS_PATH},readonly"]
    run += [args.image, "--config", CONFIG_PATH]
    return {
        "mode": args.mode,
        "image": identity,
        "container": args.name,
        "restart": args.restart,
        "publish": f"{args.bind}:{args.host_port}->{CONTAINER_PORT}/tcp",
        "domain_mount": {"type": "volume" if args.domain_volume else "bind", "source": domain_source, "target": DOMAIN_PATH},
        "config": {"host": str(config_path), "target": CONFIG_PATH},
        "definitions": None if definitions is None else {"host": str(definitions), "target": DEFINITIONS_PATH},
        "outputs": summary["outputs"],
        "sources_configured": summary["sources"],
        "rollback": rollback,
        "cleanup": cleanup_commands(args.name, None),
        "bolt_ons_started": [],
        "docker": run,
        "command": "docker " + shlex.join(run),
    }


def cleanup_commands(name: str, image: str | None) -> list[str]:
    commands = [f"docker rm --force {shlex.quote(name)}"]
    if image:
        commands.append(f"docker image rm {shlex.quote(image)}")
    return commands


def cleanup(docker: Docker, name: str, image: str | None, expected_image: str, apply: bool) -> dict:
    if not NAME_RE.match(name or "") or any(ch in name for ch in "*?[]"):
        raise BundleError("cleanup takes one exact container name")
    check_immutable(expected_image)
    container = docker.container(name)
    removals = []
    if container is not None:
        running_image = container.get("Config", {}).get("Image")
        if running_image != expected_image:
            raise BundleError(f"{name} runs {running_image!r}, not {expected_image!r}; refusing to remove it")
        removals.append(["rm", "--force", name])
    if image:
        if image != expected_image:
            raise BundleError("--remove-image must equal --expected-image exactly")
        removals.append(["image", "rm", image])
    if apply:
        for command in removals:
            docker.mutate(*command)
    return {"applied": apply, "commands": ["docker " + shlex.join(command) for command in removals]}


# --- CLI ------------------------------------------------------------------------


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()


def main(argv=None, docker: Docker | None = None, port_check=port_free) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    tag = sub.add_parser("tag", help="print the immutable image reference")
    tag.add_argument("--version", required=True)
    tag.add_argument("--revision", required=True)

    build = sub.add_parser("build", help="build and label an immutable image (dry run by default)")
    build.add_argument("--version", required=True)
    build.add_argument("--base-image", default="mxl-lab:latest", help="licensed local base; resolved to its image id")
    build.add_argument("--release-dir", default="build/release")
    build.add_argument("--apply", action="store_true")

    verify = sub.add_parser("verify", help="verify an image's OCI labels against its tag")
    verify.add_argument("--image", required=True)
    verify.add_argument("--version")
    verify.add_argument("--revision")

    launch = sub.add_parser("launch", help="preflight and print (or with --apply, run) the Core launch")
    launch.add_argument("--image", required=True)
    launch.add_argument("--name", required=True, help="new, unused container name")
    launch.add_argument("--config", required=True, help="caller-supplied facility config")
    launch.add_argument("--definitions", help=f"directory of output definitions mounted at {DEFINITIONS_PATH}")
    launch.add_argument("--domain-volume")
    launch.add_argument("--domain-path")
    launch.add_argument("--host-port", type=int, required=True)
    launch.add_argument("--bind", default="127.0.0.1")
    launch.add_argument("--restart", default="unless-stopped", choices=("no", "unless-stopped", "on-failure"))
    launch.add_argument("--rollback-container", help="the existing service this candidate would succeed (read only)")
    launch.add_argument("--rollback-config",
                        help="that service's own facility config; required while it runs, to prove disjoint outputs")
    launch.add_argument("--mode", default="production", choices=("production", "test"))
    launch.add_argument("--apply", action="store_true")

    clean = sub.add_parser("cleanup", help="remove exactly one candidate container (dry run by default)")
    clean.add_argument("--name", required=True)
    clean.add_argument("--expected-image", required=True)
    clean.add_argument("--remove-image", help="also remove this exact image (must equal --expected-image)")
    clean.add_argument("--apply", action="store_true")

    args = parser.parse_args(argv)
    docker = docker or Docker()
    try:
        if args.command == "tag":
            result = {"image": image_ref(args.version, args.revision)}
        elif args.command == "verify":
            result = verify_image(docker, args.image, args.version, args.revision)
        elif args.command == "launch":
            result = launch_plan(docker, args, port_check)
            result["applied"] = False
            if args.apply:
                docker.mutate(*result["docker"])
                result["applied"] = True
        elif args.command == "cleanup":
            result = cleanup(docker, args.name, args.remove_image, args.expected_image, args.apply)
        else:  # build
            if _git("status", "--porcelain"):
                raise BundleError("the checkout has uncommitted changes; build only from a clean commit")
            revision = _git("rev-parse", "HEAD")
            base_digest = docker.image_id(args.base_image)
            if not base_digest:
                raise BundleError(f"base image {args.base_image} is not present locally")
            plan = build_plan(args.version, revision, args.base_image, base_digest)
            template = json.loads(pathlib.Path("packaging/easy-mxl/mxl-switcher-core.catalog.json").read_text())
            result = {**plan, "command": "docker " + shlex.join(plan["docker"]), "applied": False}
            if args.apply:
                if docker.image_id(plan["image"]):
                    raise BundleError(f"{plan['image']} already exists; an immutable tag is never rebuilt")
                docker.mutate(*plan["docker"])
                if docker.image_id(args.base_image) != base_digest:
                    raise BundleError(f"base image {args.base_image} changed during the build; "
                                      f"remove {plan['image']} and rebuild")
                identity = verify_image(docker, plan["image"], args.version, revision)
                release = pathlib.Path(args.release_dir) / immutable_tag(args.version, revision)
                release.mkdir(parents=True, exist_ok=False)
                (release / "manifest.json").write_text(json.dumps({"bundle": 1, **identity}, indent=2, sort_keys=True) + "\n")
                (release / "mxl-switcher-core.catalog.json").write_text(
                    json.dumps(pinned_catalog(template, plan["image"]), indent=2) + "\n")
                result.update(applied=True, identity=identity, release_dir=str(release))
    except (BundleError, subprocess.CalledProcessError, OSError, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
