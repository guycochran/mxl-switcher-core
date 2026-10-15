import contextlib
import io
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import mxl_core_bundle as bundle  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
REVISION = "0123456789abcdef0123456789abcdef01234567"
IMAGE = f"mxl-switcher-core:1.0.0.g{REVISION[:12]}"
PROGRAM_ID = "00000000-0000-4000-8000-000000000901"
PREVIEW_ID = "00000000-0000-4000-8000-000000000902"
OPERATOR_PROGRAM_ID = "00000000-0000-4000-8000-000000000801"
OPERATOR_PREVIEW_ID = "00000000-0000-4000-8000-000000000802"
READS = {("image", "inspect"), ("container", "inspect")}


class FakeDocker:
    """Records docker invocations; serves images and containers from dictionaries."""

    def __init__(self, images=None, containers=None, fail_mutations=False):
        self.images = images or {}
        self.containers = containers or {}
        self.calls = []
        self.fail_mutations = fail_mutations

    def __call__(self, argv, **_):
        args = argv[1:]
        self.calls.append(args)
        if tuple(args[:2]) == ("image", "inspect"):
            ref = args[-1]
            if ref not in self.images:
                return subprocess.CompletedProcess(argv, 1, "", "No such image")
            field = args[3]
            out = json.dumps(self.images[ref]["labels"]) if "Labels" in field else self.images[ref]["id"]
            return subprocess.CompletedProcess(argv, 0, out + "\n", "")
        if tuple(args[:2]) == ("container", "inspect"):
            name = args[-1]
            if name not in self.containers:
                return subprocess.CompletedProcess(argv, 1, "", "No such container")
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.containers[name]) + "\n", "")
        return subprocess.CompletedProcess(argv, 1 if self.fail_mutations else 0, "", "")

    @property
    def mutations(self):
        return [call for call in self.calls if tuple(call[:2]) not in READS]


def good_image(ref=IMAGE, revision=REVISION, version="1.0.0"):
    return {ref: {"id": "sha256:" + "a" * 64,
                  "labels": bundle.oci_labels(version, revision, "mxl-lab:latest", "sha256:" + "b" * 64)}}


def definition(output_id):
    return json.dumps({"id": output_id, "media_type": "video/v210", "label": "Synthetic output"})


class Facility:
    """A synthetic facility: config plus an output-definitions directory."""

    def __init__(self, config_overrides=None, write_preview=True, preview_id_in_file=PREVIEW_ID):
        self.dir = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.dir.name)
        self.definitions = root / "definitions"
        self.definitions.mkdir()
        (self.definitions / "program.json").write_text(definition(PROGRAM_ID))
        if write_preview:
            (self.definitions / "preview.json").write_text(definition(preview_id_in_file))
        values = {
            "domain": "/mxl-domain",
            "program_definition": "/etc/mxl-switcher-core/definitions/program.json",
            "program_id": PROGRAM_ID,
            "preview_definition": "/etc/mxl-switcher-core/definitions/preview.json",
            "preview_id": PREVIEW_ID,
            "initial_program": "cam-1",
            "initial_preview": "cam-2",
            "control_host": "0.0.0.0",
            "control_port": "18085",
        }
        values.update(config_overrides or {})
        lines = ["# synthetic facility config"] + [f"{k}={v}" for k, v in values.items() if v is not None]
        lines += ["source=cam-1|00000000-0000-4000-8000-000000000101|Camera 1",
                  "source=cam-2|00000000-0000-4000-8000-000000000102|Camera 2"]
        self.config = root / "core.conf"
        self.config.write_text("\n".join(lines) + "\n")
        self.rollback_config = self.write_rollback(OPERATOR_PROGRAM_ID, OPERATOR_PREVIEW_ID)

    def write_rollback(self, program_id, preview_id, name="operator.conf"):
        path = pathlib.Path(self.dir.name) / name
        path.write_text(f"domain=/mxl-domain\nprogram_id={program_id}\npreview_id={preview_id}\n"
                        "source=cam-1|00000000-0000-4000-8000-000000000101|Camera 1\n")
        return path

    def close(self):
        self.dir.cleanup()


def launch_args(facility, **overrides):
    values = dict(image=IMAGE, name="mxl-core-candidate-1", config=str(facility.config),
                  definitions=str(facility.definitions), domain_volume="example-mxl-domain", domain_path=None,
                  host_port=18195, bind="127.0.0.1", restart="unless-stopped",
                  rollback_container="mxl-core-operator", rollback_config=str(facility.rollback_config),
                  mode="production", apply=False)
    values.update(overrides)
    return SimpleNamespace(**values)


OPERATOR = {"Name": "/mxl-core-operator", "Image": "sha256:" + "c" * 64, "State": {"Status": "running"},
            "Config": {"Image": "mxl-switcher-core:0.9.0.g" + "9" * 12}}
STOPPED_OPERATOR = {**OPERATOR, "State": {"Status": "exited"}}


class TagAndLabelTest(unittest.TestCase):
    def test_tag_is_deterministic_semver_plus_revision(self):
        self.assertEqual(bundle.image_ref("1.0.0", REVISION), IMAGE)
        self.assertEqual(bundle.image_ref("1.0.0", REVISION), bundle.image_ref("1.0.0", REVISION))
        self.assertEqual(bundle.immutable_tag("0.3.0-rc.1", REVISION), "0.3.0-rc.1.g0123456789ab")

    def test_rejects_non_semver_and_short_revisions(self):
        for version, revision in (("latest", REVISION), ("1.0", REVISION), ("1.0.0", "abc1234"),
                                  ("1.0.0", REVISION.upper())):
            with self.subTest(version=version, revision=revision), self.assertRaises(bundle.BundleError):
                bundle.immutable_tag(version, revision)

    def test_oci_labels_are_exact(self):
        labels = bundle.oci_labels("1.0.0", REVISION, "mxl-lab:latest", "sha256:" + "b" * 64)
        self.assertEqual(labels["org.opencontainers.image.version"], "1.0.0")
        self.assertEqual(labels["org.opencontainers.image.revision"], REVISION)
        self.assertEqual(labels["org.opencontainers.image.base.digest"], "sha256:" + "b" * 64)
        self.assertEqual(labels["org.opencontainers.image.source"], bundle.SOURCE_URL)

    def test_build_plan_is_deterministic_and_labelled(self):
        first = bundle.build_plan("1.0.0", REVISION, "mxl-lab:latest", "sha256:" + "b" * 64)
        second = bundle.build_plan("1.0.0", REVISION, "mxl-lab:latest", "sha256:" + "b" * 64)
        self.assertEqual(first, second)
        self.assertEqual(first["image"], IMAGE)
        self.assertIn(f"org.opencontainers.image.revision={REVISION}", first["docker"])
        self.assertEqual(first["docker"][-3:], ["--tag", IMAGE, "."])

    def test_pinned_catalog_changes_only_the_image(self):
        template = json.loads((REPO / "packaging/easy-mxl/mxl-switcher-core.catalog.json").read_text())
        pinned = bundle.pinned_catalog(template, IMAGE)
        self.assertEqual(pinned[0]["image"], IMAGE)
        self.assertEqual(pinned[0]["imagePolicy"], "local")
        unchanged = {k: v for k, v in pinned[0].items() if k not in ("image", "imagePolicy", "imageRepository")}
        self.assertEqual(unchanged, {k: v for k, v in template[0].items() if k not in ("image", "imagePolicy", "imageRepository")})


class AmbiguousTagTest(unittest.TestCase):
    def test_production_refuses_ambiguous_tags(self):
        for ref in ("mxl-switcher-core", "mxl-switcher-core:latest", "mxl-switcher-core:local",
                    "mxl-switcher-core:0.2.0-candidate", "mxl-switcher-core:1.0.0",
                    "mxl-switcher-core@sha256:" + "a" * 64, "registry.example:5000/mxl-switcher-core"):
            with self.subTest(ref=ref), self.assertRaises(bundle.BundleError):
                bundle.check_immutable(ref)

    def test_registry_port_is_not_a_tag(self):
        self.assertEqual(bundle.parse_image_ref("registry.example:5000/core:1.0.0.g" + "a" * 12),
                         ("registry.example:5000/core", "1.0.0.g" + "a" * 12, None))

    def test_label_mismatch_is_refused(self):
        images = good_image()
        images[IMAGE]["labels"]["org.opencontainers.image.revision"] = "f" * 40
        with self.assertRaisesRegex(bundle.BundleError, "revision label"):
            bundle.verify_image(bundle.Docker(FakeDocker(images=images)), IMAGE)


class LaunchTest(unittest.TestCase):
    def setUp(self):
        self.facility = Facility()
        self.addCleanup(self.facility.close)

    def test_dry_run_prints_everything_and_mutates_nothing(self):
        fake = FakeDocker(images=good_image(), containers={"mxl-core-operator": OPERATOR})
        plan = bundle.launch_plan(bundle.Docker(fake), launch_args(self.facility), port_check=lambda *_: True)
        self.assertEqual(fake.mutations, [])
        self.assertEqual(plan["image"]["revision"], REVISION)
        self.assertEqual(plan["publish"], "127.0.0.1:18195->18085/tcp")
        self.assertEqual(plan["domain_mount"], {"type": "volume", "source": "example-mxl-domain", "target": "/mxl-domain"})
        self.assertEqual(plan["outputs"]["program"]["id"], PROGRAM_ID)
        self.assertEqual(plan["outputs"]["preview"]["id"], PREVIEW_ID)
        self.assertEqual(plan["rollback"]["container"], "mxl-core-operator")
        self.assertEqual(plan["bolt_ons_started"], [])
        self.assertIn(f"type=bind,source={self.facility.config.resolve()},target=/etc/mxl-switcher-core.conf,readonly",
                      plan["docker"])
        self.assertEqual(plan["docker"][-3:], [IMAGE, "--config", "/etc/mxl-switcher-core.conf"])

    def test_cli_dry_run_has_zero_mutations(self):
        fake = FakeDocker(images=good_image(), containers={"mxl-core-operator": OPERATOR})
        argv = ["launch", "--image", IMAGE, "--name", "mxl-core-candidate-1", "--config", str(self.facility.config),
                "--definitions", str(self.facility.definitions), "--domain-volume", "example-mxl-domain",
                "--host-port", "18195", "--rollback-container", "mxl-core-operator",
                "--rollback-config", str(self.facility.rollback_config)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = bundle.main(argv, docker=bundle.Docker(fake), port_check=lambda *_: True)
        self.assertEqual(status, 0)
        self.assertFalse(json.loads(out.getvalue())["applied"])
        self.assertEqual(fake.mutations, [])

    def test_apply_runs_exactly_one_docker_run(self):
        fake = FakeDocker(images=good_image(), containers={"mxl-core-operator": OPERATOR})
        argv = ["launch", "--image", IMAGE, "--name", "mxl-core-candidate-1", "--config", str(self.facility.config),
                "--definitions", str(self.facility.definitions), "--domain-volume", "example-mxl-domain",
                "--host-port", "18195", "--rollback-container", "mxl-core-operator",
                "--rollback-config", str(self.facility.rollback_config), "--apply"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bundle.main(argv, docker=bundle.Docker(fake), port_check=lambda *_: True), 0)
        self.assertEqual(len(fake.mutations), 1)
        self.assertEqual(fake.mutations[0][:5], ["run", "--detach", "--name", "mxl-core-candidate-1", "--restart"])

    def test_existing_container_name_is_refused(self):
        fake = FakeDocker(images=good_image(), containers={"mxl-core-operator": OPERATOR,
                                                          "mxl-core-candidate-1": {"Name": "/mxl-core-candidate-1"}})
        with self.assertRaisesRegex(bundle.BundleError, "already exists"):
            bundle.launch_plan(bundle.Docker(fake), launch_args(self.facility), port_check=lambda *_: True)
        self.assertEqual(fake.mutations, [])

    def test_container_id_prefix_is_not_mistaken_for_a_name(self):
        # docker inspect also resolves ID prefixes; the tool must compare the exact name.
        fake = FakeDocker(images=good_image(), containers={"mxl-core-operator": OPERATOR,
                                                          "mxl-core-candidate-1": {"Name": "/something-else"}})
        plan = bundle.launch_plan(bundle.Docker(fake), launch_args(self.facility), port_check=lambda *_: True)
        self.assertEqual(plan["container"], "mxl-core-candidate-1")

    def test_occupied_port_is_refused(self):
        fake = FakeDocker(images=good_image(), containers={"mxl-core-operator": OPERATOR})
        with self.assertRaisesRegex(bundle.BundleError, "occupied"):
            bundle.launch_plan(bundle.Docker(fake), launch_args(self.facility), port_check=lambda *_: False)

    def test_missing_config_is_refused(self):
        fake = FakeDocker(images=good_image(), containers={"mxl-core-operator": OPERATOR})
        with self.assertRaisesRegex(bundle.BundleError, "does not exist"):
            bundle.launch_plan(bundle.Docker(fake), launch_args(self.facility, config="/nonexistent/core.conf"),
                               port_check=lambda *_: True)

    def test_missing_output_definition_is_refused(self):
        facility = Facility(write_preview=False)
        self.addCleanup(facility.close)
        fake = FakeDocker(images=good_image(), containers={"mxl-core-operator": OPERATOR})
        with self.assertRaisesRegex(bundle.BundleError, "preview output definition preview.json does not exist"):
            bundle.launch_plan(bundle.Docker(fake), launch_args(facility), port_check=lambda *_: True)

    def test_definition_id_must_match_config(self):
        facility = Facility(preview_id_in_file="00000000-0000-4000-8000-000000000999")
        self.addCleanup(facility.close)
        fake = FakeDocker(images=good_image(), containers={"mxl-core-operator": OPERATOR})
        with self.assertRaisesRegex(bundle.BundleError, "does not match preview_id"):
            bundle.launch_plan(bundle.Docker(fake), launch_args(facility), port_check=lambda *_: True)

    def test_identical_output_ids_are_refused(self):
        with self.assertRaisesRegex(bundle.BundleError, "distinct"):
            bundle.validate_config(f"""domain=/mxl-domain
program_definition=/etc/mxl-switcher-core/definitions/program.json
program_id={PROGRAM_ID}
preview_definition=/etc/mxl-switcher-core/definitions/preview.json
preview_id={PROGRAM_ID}
initial_program=a
initial_preview=b
control_host=0.0.0.0
control_port=18085
source=a|00000000-0000-4000-8000-000000000101|A
source=b|00000000-0000-4000-8000-000000000102|B
""", None, "production")

    def test_domain_mount_must_be_explicit(self):
        fake = FakeDocker(images=good_image(), containers={"mxl-core-operator": OPERATOR})
        with self.assertRaisesRegex(bundle.BundleError, "exactly one explicit MXL domain"):
            bundle.launch_plan(bundle.Docker(fake), launch_args(self.facility, domain_volume=None),
                               port_check=lambda *_: True)

    def test_production_refuses_bundled_acceptance_definitions(self):
        facility = Facility(config_overrides={
            "program_definition": "/usr/local/share/mxl-switcher-core/acceptance/program.json"})
        self.addCleanup(facility.close)
        fake = FakeDocker(images=good_image(), containers={"mxl-core-operator": OPERATOR})
        with self.assertRaisesRegex(bundle.BundleError, "own output definitions"):
            bundle.launch_plan(bundle.Docker(fake), launch_args(facility), port_check=lambda *_: True)

    def test_production_requires_a_rollback_target(self):
        fake = FakeDocker(images=good_image())
        with self.assertRaisesRegex(bundle.BundleError, "rollback-container"):
            bundle.launch_plan(bundle.Docker(fake), launch_args(self.facility, rollback_container=None),
                               port_check=lambda *_: True)

    def test_production_refuses_ambiguous_image(self):
        fake = FakeDocker(images={"mxl-switcher-core:0.2.0-candidate": good_image()[IMAGE]},
                          containers={"mxl-core-operator": OPERATOR})
        with self.assertRaisesRegex(bundle.BundleError, "ambiguous|immutable"):
            bundle.launch_plan(bundle.Docker(fake), launch_args(self.facility, image="mxl-switcher-core:0.2.0-candidate"),
                               port_check=lambda *_: True)


class OutputCollisionTest(unittest.TestCase):
    """Two writers to one MXL flow are forbidden."""

    def setUp(self):
        self.facility = Facility()
        self.addCleanup(self.facility.close)

    def plan(self, operator=OPERATOR, **overrides):
        self.fake = FakeDocker(images=good_image(), containers={"mxl-core-operator": operator})
        return bundle.launch_plan(bundle.Docker(self.fake), launch_args(self.facility, **overrides),
                                  port_check=lambda *_: True)

    def test_running_rollback_without_config_refuses(self):
        with self.assertRaisesRegex(bundle.BundleError, "rollback container is running; pass --rollback-config"):
            self.plan(rollback_config=None)
        self.assertEqual(self.fake.mutations, [])

    def test_unknown_or_transitional_state_counts_as_running(self):
        for status in ("restarting", "paused", None):
            operator = {**OPERATOR, "State": {"Status": status} if status else {}}
            with self.subTest(status=status), self.assertRaisesRegex(bundle.BundleError, "pass --rollback-config"):
                self.plan(operator=operator, rollback_config=None)

    def test_running_rollback_with_same_outputs_refuses(self):
        overlapping = self.facility.write_rollback(PROGRAM_ID, PREVIEW_ID, "overlap.conf")
        with self.assertRaisesRegex(bundle.BundleError, "concurrent writers to the same MXL flow") as raised:
            self.plan(rollback_config=str(overlapping))
        self.assertIn(PROGRAM_ID, str(raised.exception))
        self.assertIn(PREVIEW_ID, str(raised.exception))
        self.assertEqual(self.fake.mutations, [])

    def test_cross_role_collision_refuses(self):
        # The candidate's Program is the running rollback's Preview.
        crossed = self.facility.write_rollback(OPERATOR_PROGRAM_ID, PROGRAM_ID, "crossed.conf")
        with self.assertRaisesRegex(bundle.BundleError, "candidate program = rollback preview"):
            self.plan(rollback_config=str(crossed))

    def test_running_rollback_with_disjoint_outputs_passes_without_mutation(self):
        plan = self.plan()
        self.assertEqual(self.fake.mutations, [])
        rollback = plan["rollback"]
        self.assertEqual(rollback["rollback_state"], "running")
        self.assertEqual(rollback["launch_kind"], "parallel_disjoint_outputs")
        self.assertEqual(rollback["candidate_outputs"], {"program": PROGRAM_ID, "preview": PREVIEW_ID})
        self.assertEqual(rollback["rollback_outputs"], {"program": OPERATOR_PROGRAM_ID, "preview": OPERATOR_PREVIEW_ID})
        self.assertEqual(rollback["overlapping_outputs"], [])
        self.assertEqual(rollback["rollback_config"], str(self.facility.rollback_config.resolve()))

    def test_stopped_rollback_may_share_outputs(self):
        overlapping = self.facility.write_rollback(PROGRAM_ID, PREVIEW_ID, "overlap.conf")
        plan = self.plan(operator=STOPPED_OPERATOR, rollback_config=str(overlapping))
        rollback = plan["rollback"]
        self.assertEqual(rollback["rollback_state"], "exited")
        self.assertEqual(rollback["launch_kind"], "replacement_with_rollback_stopped")
        self.assertEqual([item["flow_id"] for item in rollback["overlapping_outputs"]], [PREVIEW_ID, PROGRAM_ID])
        self.assertEqual(self.fake.mutations, [])

    def test_stopped_rollback_does_not_require_config(self):
        plan = self.plan(operator=STOPPED_OPERATOR, rollback_config=None)
        self.assertIsNone(plan["rollback"]["rollback_outputs"])
        self.assertIsNone(plan["rollback"]["rollback_config"])

    def test_missing_or_malformed_rollback_config_refuses(self):
        with self.assertRaisesRegex(bundle.BundleError, "rollback config .* does not exist"):
            self.plan(rollback_config="/nonexistent/operator.conf")
        malformed = pathlib.Path(self.facility.dir.name) / "malformed.conf"
        malformed.write_text("program_id=00000000-0000-4000-8000-000000000801\nnot a key value line\n")
        with self.assertRaisesRegex(bundle.BundleError, "expected key=value"):
            self.plan(rollback_config=str(malformed))
        incomplete = pathlib.Path(self.facility.dir.name) / "incomplete.conf"
        incomplete.write_text("program_id=00000000-0000-4000-8000-000000000801\n")
        with self.assertRaisesRegex(bundle.BundleError, "missing preview_id"):
            self.plan(rollback_config=str(incomplete))

    def test_rollback_config_without_container_refuses(self):
        with self.assertRaisesRegex(bundle.BundleError, "needs --rollback-container"):
            self.plan(rollback_container=None)

    def test_cli_reports_rollback_state_and_compared_ids(self):
        fake = FakeDocker(images=good_image(), containers={"mxl-core-operator": OPERATOR})
        argv = ["launch", "--image", IMAGE, "--name", "mxl-core-candidate-1", "--config", str(self.facility.config),
                "--definitions", str(self.facility.definitions), "--domain-volume", "example-mxl-domain",
                "--host-port", "18195", "--rollback-container", "mxl-core-operator",
                "--rollback-config", str(self.facility.rollback_config)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(bundle.main(argv, docker=bundle.Docker(fake), port_check=lambda *_: True), 0)
        rollback = json.loads(out.getvalue())["rollback"]
        self.assertEqual(rollback["rollback_state"], "running")
        self.assertEqual(sorted(rollback["candidate_outputs"].values()), [PROGRAM_ID, PREVIEW_ID])
        self.assertEqual(fake.mutations, [])

    def test_cli_apply_with_overlap_never_runs_docker(self):
        overlapping = self.facility.write_rollback(PROGRAM_ID, PREVIEW_ID, "overlap.conf")
        fake = FakeDocker(images=good_image(), containers={"mxl-core-operator": OPERATOR})
        argv = ["launch", "--image", IMAGE, "--name", "mxl-core-candidate-1", "--config", str(self.facility.config),
                "--definitions", str(self.facility.definitions), "--domain-volume", "example-mxl-domain",
                "--host-port", "18195", "--rollback-container", "mxl-core-operator",
                "--rollback-config", str(overlapping), "--apply"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bundle.main(argv, docker=bundle.Docker(fake), port_check=lambda *_: True), 2)
        self.assertIn("concurrent writers", err.getvalue())
        self.assertEqual(fake.mutations, [])


def listener(host="127.0.0.1", port=0):
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen()
    return server


def stop_after_served_connection(server):
    """Serve one client, close the server side first, then stop listening.

    The listening port is left with an accepted socket in TCP TIME_WAIT: the
    state that made the first live cutover refuse a free port.
    """

    client = socket.create_connection(server.getsockname()[:2])
    accepted, _ = server.accept()
    accepted.close()
    client.recv(1)
    client.close()
    server.close()


def naive_bind_free(host, port):
    with socket.socket() as probe:
        try:
            probe.bind((host, port))
            return True
        except OSError:
            return False


class PortProbeTest(unittest.TestCase):
    """Real loopback sockets: can a replacement bind the port now?"""

    def test_active_listener_is_occupied(self):
        server = listener()
        self.addCleanup(server.close)
        self.assertFalse(bundle.port_free("127.0.0.1", server.getsockname()[1]))

    def test_port_is_free_right_after_listener_stops_despite_time_wait(self):
        server = listener()
        port = server.getsockname()[1]
        stop_after_served_connection(server)
        self.assertTrue(bundle.port_free("127.0.0.1", port))
        if naive_bind_free("127.0.0.1", port):
            self.skipTest("this platform let a plain bind through; the regression is shown elsewhere")
        # The previous probe (a plain bind) reported this free port as occupied.

    def test_wildcard_listener_is_seen_from_a_loopback_probe(self):
        server = listener("0.0.0.0")
        self.addCleanup(server.close)
        self.assertFalse(bundle.port_free("127.0.0.1", server.getsockname()[1]))

    def test_wildcard_bind_address_probes_loopback(self):
        server = listener("127.0.0.1")
        self.addCleanup(server.close)
        self.assertFalse(bundle.port_free("0.0.0.0", server.getsockname()[1]))


class PostStopLaunchTest(unittest.TestCase):
    def setUp(self):
        self.facility = Facility()
        self.addCleanup(self.facility.close)

    def test_launch_accepts_the_post_stop_port(self):
        server = listener()
        port = server.getsockname()[1]
        stop_after_served_connection(server)
        fake = FakeDocker(images=good_image(), containers={"mxl-core-operator": STOPPED_OPERATOR})
        plan = bundle.launch_plan(bundle.Docker(fake), launch_args(self.facility, host_port=port),
                                  port_check=bundle.port_free)
        self.assertEqual(plan["publish"], f"127.0.0.1:{port}->18085/tcp")
        self.assertEqual(fake.mutations, [])

    def test_live_listener_still_refuses_before_any_mutation(self):
        server = listener()
        self.addCleanup(server.close)
        fake = FakeDocker(images=good_image(), containers={"mxl-core-operator": STOPPED_OPERATOR})
        argv = ["launch", "--image", IMAGE, "--name", "mxl-core-candidate-1", "--config", str(self.facility.config),
                "--definitions", str(self.facility.definitions), "--domain-volume", "example-mxl-domain",
                "--host-port", str(server.getsockname()[1]), "--rollback-container", "mxl-core-operator", "--apply"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bundle.main(argv, docker=bundle.Docker(fake)), 2)
        self.assertIn("occupied", err.getvalue())
        self.assertEqual(fake.mutations, [])


def easy_flow(flow_id, active, status=None, writer="core-operator"):
    return {"id": flow_id, "active": active, "status": status or ("active" if active else "inactive"),
            "writerContainer": {"id": "f" * 64, "name": writer} if active else None}


class ActiveWriterGuardTest(unittest.TestCase):
    """Optional: ask EasyMXL whether any candidate output flow already has a live writer."""

    def setUp(self):
        self.facility = Facility()
        self.addCleanup(self.facility.close)
        self.requests = []

    def fetch_returning(self, inventory):
        def fetch(url, token):
            self.requests.append((url, token))
            if isinstance(inventory, Exception):
                raise inventory
            return inventory
        return fetch

    def plan(self, inventory, operator=OPERATOR, **overrides):
        values = {"easy_mxl_url": "http://127.0.0.1:19700", "easy_mxl_domain": "example-domain",
                  "easy_mxl_token_env": "TEST_EASY_MXL_TOKEN", **overrides}
        self.fake = FakeDocker(images=good_image(), containers={"mxl-core-operator": operator})
        return bundle.launch_plan(bundle.Docker(self.fake), launch_args(self.facility, **values),
                                  port_check=lambda *_: True, fetch=self.fetch_returning(inventory))

    def test_disabled_by_default(self):
        fake = FakeDocker(images=good_image(), containers={"mxl-core-operator": OPERATOR})
        calls = []
        plan = bundle.launch_plan(bundle.Docker(fake), launch_args(self.facility), port_check=lambda *_: True,
                                  fetch=lambda *a: calls.append(a))
        self.assertIsNone(plan["active_writer_check"])
        self.assertEqual(calls, [])

    def test_url_without_domain_refuses(self):
        with self.assertRaisesRegex(bundle.BundleError, "needs both"):
            self.plan([], easy_mxl_domain=None)
        self.assertEqual(self.requests, [])

    def test_live_writer_on_program_refuses_and_names_it(self):
        inventory = [easy_flow(PROGRAM_ID, True, writer="core-operator"), easy_flow(PREVIEW_ID, False)]
        with self.assertRaisesRegex(bundle.BundleError, r"live writer on program .* \(writer: core-operator\)"):
            self.plan(inventory)
        self.assertEqual(self.fake.mutations, [])

    def test_live_writer_on_preview_refuses_even_for_a_stopped_rollback(self):
        # Something other than the named (stopped) rollback is still writing.
        inventory = [easy_flow(PROGRAM_ID, False), easy_flow(PREVIEW_ID, True, writer="stray-core")]
        with self.assertRaisesRegex(bundle.BundleError, "stray-core"):
            self.plan(inventory, operator=STOPPED_OPERATOR)

    def test_inactive_stale_or_absent_outputs_pass(self):
        inventory = {"flows": [easy_flow(PROGRAM_ID, False, status="stale"),
                               easy_flow("00000000-0000-4000-8000-000000000777", True)]}
        plan = self.plan(inventory, operator=STOPPED_OPERATOR)
        check = plan["active_writer_check"]
        self.assertEqual(check["source"], "http://127.0.0.1:19700/api/domains/example-domain/flows")
        self.assertEqual(check["outputs"][PROGRAM_ID]["statuses"], ["stale"])
        self.assertFalse(check["outputs"][PREVIEW_ID]["present"])
        self.assertEqual(self.fake.mutations, [])

    def test_unknown_writer_state_fails_closed(self):
        inventory = [easy_flow(PROGRAM_ID, None, status="inactive"), easy_flow(PREVIEW_ID, False)]
        with self.assertRaisesRegex(bundle.BundleError, "cannot determine the writer state"):
            self.plan(inventory)

    def test_malformed_inventory_and_fetch_errors_fail_closed(self):
        with self.assertRaisesRegex(bundle.BundleError, "malformed"):
            self.plan({"unexpected": True})
        with self.assertRaisesRegex(bundle.BundleError, "EasyMXL GET"):
            self.plan(bundle.BundleError("EasyMXL GET http://127.0.0.1:19700/... failed: URLError"))

    def test_real_http_get_sends_token_but_never_echoes_it(self):
        import http.server
        import threading

        token = "synthetic-bundle-token-42"
        seen = []
        body = json.dumps([easy_flow(PROGRAM_ID, False), easy_flow(PREVIEW_ID, False)]).encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                seen.append((self.command, self.path, self.headers.get("Authorization")))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        fake = FakeDocker(images=good_image(), containers={"mxl-core-operator": OPERATOR})
        argv = ["launch", "--image", IMAGE, "--name", "mxl-core-candidate-1", "--config", str(self.facility.config),
                "--definitions", str(self.facility.definitions), "--domain-volume", "example-mxl-domain",
                "--host-port", "18195", "--rollback-container", "mxl-core-operator",
                "--rollback-config", str(self.facility.rollback_config),
                "--easy-mxl-url", f"http://127.0.0.1:{server.server_address[1]}", "--easy-mxl-domain", "example domain",
                "--easy-mxl-token-env", "TEST_BUNDLE_EASY_MXL_TOKEN"]
        out, err = io.StringIO(), io.StringIO()
        saved = os.environ.get("TEST_BUNDLE_EASY_MXL_TOKEN")
        os.environ["TEST_BUNDLE_EASY_MXL_TOKEN"] = token
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                status = bundle.main(argv, docker=bundle.Docker(fake), port_check=lambda *_: True)
        finally:
            if saved is None:
                del os.environ["TEST_BUNDLE_EASY_MXL_TOKEN"]
            else:
                os.environ["TEST_BUNDLE_EASY_MXL_TOKEN"] = saved
        self.assertEqual(status, 0, err.getvalue())
        self.assertEqual(seen, [("GET", "/api/domains/example%20domain/flows", f"Bearer {token}")])
        self.assertNotIn(token, out.getvalue() + err.getvalue())
        self.assertEqual(fake.mutations, [])


class CleanupTest(unittest.TestCase):
    def test_cleanup_targets_only_the_named_container_and_image(self):
        fake = FakeDocker(containers={"mxl-core-candidate-1": {"Name": "/mxl-core-candidate-1", "Config": {"Image": IMAGE}}})
        dry = bundle.cleanup(bundle.Docker(fake), "mxl-core-candidate-1", IMAGE, IMAGE, apply=False)
        self.assertEqual(dry["commands"], ["docker rm --force mxl-core-candidate-1", f"docker image rm {IMAGE}"])
        self.assertEqual(fake.mutations, [])
        bundle.cleanup(bundle.Docker(fake), "mxl-core-candidate-1", IMAGE, IMAGE, apply=True)
        self.assertEqual(fake.mutations, [["rm", "--force", "mxl-core-candidate-1"], ["image", "rm", IMAGE]])
        self.assertFalse(any("prune" in " ".join(call) or "volume" in call for call in fake.calls))

    def test_cleanup_refuses_a_container_running_another_image(self):
        fake = FakeDocker(containers={"mxl-core-operator": OPERATOR})
        with self.assertRaisesRegex(bundle.BundleError, "refusing to remove"):
            bundle.cleanup(bundle.Docker(fake), "mxl-core-operator", None, IMAGE, apply=True)
        self.assertEqual(fake.mutations, [])

    def test_cleanup_refuses_patterns_and_mismatched_image(self):
        fake = FakeDocker()
        with self.assertRaises(bundle.BundleError):
            bundle.cleanup(bundle.Docker(fake), "mxl-core-*", None, IMAGE, apply=True)
        with self.assertRaisesRegex(bundle.BundleError, "must equal"):
            bundle.cleanup(bundle.Docker(fake), "mxl-core-candidate-1", "mxl-switcher-core:latest", IMAGE, apply=True)
        self.assertEqual(fake.mutations, [])


class BuildTest(unittest.TestCase):
    def test_build_dry_run_mutates_nothing(self):
        fake = FakeDocker(images={"mxl-lab:latest": {"id": "sha256:" + "b" * 64, "labels": {}}})
        original = bundle._git
        bundle._git = lambda *args: "" if args[0] == "status" else REVISION
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                status = bundle.main(["build", "--version", "1.0.0"], docker=bundle.Docker(fake))
        finally:
            bundle._git = original
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out.getvalue())["image"], IMAGE)
        self.assertEqual(fake.mutations, [])

    def test_build_refuses_a_dirty_checkout(self):
        original = bundle._git
        bundle._git = lambda *args: " M packaging/easy-mxl/mxl-switcher-core.catalog.json" if args[0] == "status" else REVISION
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                status = bundle.main(["build", "--version", "1.0.0"], docker=bundle.Docker(FakeDocker()))
        finally:
            bundle._git = original
        self.assertEqual(status, 2)
        self.assertIn("uncommitted", err.getvalue())

    def test_build_shell_wrapper_syntax(self):
        if shutil.which("bash") is None:
            self.skipTest("bash is not installed")
        subprocess.run(["bash", "-n", str(REPO / "packaging/easy-mxl/build-local-image.sh")], check=True)


if __name__ == "__main__":
    unittest.main()
