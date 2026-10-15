import contextlib
import io
import json
import pathlib
import shutil
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

    def close(self):
        self.dir.cleanup()


def launch_args(facility, **overrides):
    values = dict(image=IMAGE, name="mxl-core-candidate-1", config=str(facility.config),
                  definitions=str(facility.definitions), domain_volume="example-mxl-domain", domain_path=None,
                  host_port=18195, bind="127.0.0.1", restart="unless-stopped",
                  rollback_container="mxl-core-operator", mode="production", apply=False)
    values.update(overrides)
    return SimpleNamespace(**values)


OPERATOR = {"Name": "/mxl-core-operator", "Image": "sha256:" + "c" * 64,
            "Config": {"Image": "mxl-switcher-core:0.9.0.g" + "9" * 12}}


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
                "--host-port", "18195", "--rollback-container", "mxl-core-operator"]
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
                "--host-port", "18195", "--rollback-container", "mxl-core-operator", "--apply"]
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
