import contextlib
import copy
import http.server
import io
import json
import os
import pathlib
import random
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import mxl_discovery_planner as planner  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "easy-mxl-discovery"


def uid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def video(n, writer, group, role="Video 1", active=True, status=None, width=1920, height=1080,
          rate=(30, 1), media_type="video/v210", label=None):
    return {
        "id": uid(n), "label": label or f"{group} Video", "groupHint": f"{group}:{role}",
        "group": group, "role": role, "format": "video", "mediaType": media_type,
        "def": {"frame_width": width, "frame_height": height,
                "grain_rate": {"numerator": rate[0], "denominator": rate[1]}, "media_type": media_type},
        "active": active, "status": status or ("active" if active else "inactive"),
        "writerContainer": None if writer is None else {"id": f"{n:064x}", "name": writer},
    }


def audio(n, writer, group, role="Audio 1", active=True, status=None, sample_rate=48000, channels=2,
          media_type="audio/float32"):
    return {
        "id": uid(n), "label": f"{group} Audio", "groupHint": f"{group}:{role}",
        "group": group, "role": role, "format": "audio", "mediaType": media_type,
        "def": {"sample_rate": {"numerator": sample_rate}, "channel_count": channels, "media_type": media_type},
        "active": active, "status": status or ("active" if active else "inactive"),
        "writerContainer": None if writer is None else {"id": f"{n:064x}", "name": writer},
    }


def source(source_id, writer, group, audio_role="Audio 1", required=None, **video_override):
    entry = {"id": source_id, "match": {"writer_container": writer, "group": group, "video_role": "Video 1"}}
    if audio_role:
        entry["match"]["audio_role"] = audio_role
    if required is not None:
        entry["audio"] = {"required": required}
    if video_override:
        entry["video"] = video_override
    return entry


def mapping(*sources):
    return {
        "version": 1,
        "domain": "example-domain",
        "defaults": {
            "video": {"width": 1920, "height": 1080, "rate": "30/1", "media_type": "video/v210"},
            "audio": {"sample_rate": 48000, "channels": 2, "media_type": "audio/float32"},
        },
        "sources": list(sources),
    }


def core_source(source_id, flow_n, audio_n=None):
    return {
        "id": source_id, "flow_id": uid(flow_n), "label": source_id,
        "format": {"width": 1920, "height": 1080, "rate_numerator": 30, "rate_denominator": 1,
                   "encoding": "video/v210"},
        "kind": "camera", "capabilities": ["video"],
        "audio_flow_id": None if audio_n is None else uid(audio_n),
    }


def by_id(result):
    return {entry["id"]: entry for entry in result["sources"]}


class MatchingTest(unittest.TestCase):
    def test_identical_labels_from_two_writers_bind_by_writer_not_label(self):
        flows = [
            video(1, "contrib-a", "Camera Output 1", label="Camera Output 1 Video"),
            audio(2, "contrib-a", "Camera Output 1"),
            video(3, "contrib-b", "Camera Output 1", label="Camera Output 1 Video"),
            audio(4, "contrib-b", "Camera Output 1"),
        ]
        rules = mapping(source("cam-a", "contrib-a", "Camera Output 1"), source("cam-b", "contrib-b", "Camera Output 1"))
        result = by_id(planner.plan(flows, rules, [core_source("cam-a", 90, 91), core_source("cam-b", 92, 93)]))
        self.assertEqual(result["cam-a"]["desired"], {"flow_id": uid(1), "audio_flow_id": uid(2)})
        self.assertEqual(result["cam-b"]["desired"], {"flow_id": uid(3), "audio_flow_id": uid(4)})

    def test_active_flow_wins_and_stale_versions_are_diagnostics(self):
        flows = [
            video(10, "contrib-a", "Camera Output 1", active=False, status="stale"),
            video(11, "contrib-a", "Camera Output 1", active=False, status="inactive"),
            video(12, "contrib-a", "Camera Output 1"),
            audio(13, "contrib-a", "Camera Output 1"),
        ]
        entry = by_id(planner.plan(flows, mapping(source("cam-a", "contrib-a", "Camera Output 1")),
                                   [core_source("cam-a", 12, 13)]))["cam-a"]
        self.assertEqual(entry["status"], "noop")
        self.assertEqual([c["flow_id"] for c in entry["video"]["not_active"]], [uid(10), uid(11)])
        self.assertEqual([c["state"] for c in entry["video"]["not_active"]], ["stale", "inactive"])

    def test_inactive_only_is_missing_not_bound(self):
        flows = [video(10, "contrib-a", "Camera Output 1", active=False), audio(11, "contrib-a", "Camera Output 1")]
        entry = by_id(planner.plan(flows, mapping(source("cam-a", "contrib-a", "Camera Output 1")),
                                   [core_source("cam-a", 10, 11)]))["cam-a"]
        self.assertEqual(entry["status"], "missing")
        self.assertIsNone(entry["desired"])

    def test_audio_pairs_only_within_the_same_writer_and_group(self):
        flows = [
            video(20, "contrib-a", "Camera Output 1"),
            audio(21, "contrib-a", "Camera Output 2"),   # same writer, other group
            audio(22, "contrib-b", "Camera Output 1"),   # other writer, same group
            audio(23, "contrib-a", "Camera Output 1"),   # the pair
        ]
        entry = by_id(planner.plan(flows, mapping(source("cam-a", "contrib-a", "Camera Output 1")),
                                   [core_source("cam-a", 90)]))["cam-a"]
        self.assertEqual(entry["desired"], {"flow_id": uid(20), "audio_flow_id": uid(23)})

    def test_missing_required_audio_blocks(self):
        flows = [video(30, "contrib-a", "Camera Output 1")]
        result = planner.plan(flows, mapping(source("cam-a", "contrib-a", "Camera Output 1")), [core_source("cam-a", 90)])
        entry = by_id(result)["cam-a"]
        self.assertEqual(entry["status"], "missing")
        self.assertIn("audio_missing", entry["reasons"])
        self.assertTrue(result["blocked"])

    def test_missing_optional_audio_binds_video_with_null_audio(self):
        flows = [video(30, "contrib-a", "Camera Output 1")]
        result = planner.plan(flows, mapping(source("cam-a", "contrib-a", "Camera Output 1", required=False)),
                              [core_source("cam-a", 90, 91)])
        entry = by_id(result)["cam-a"]
        self.assertEqual(entry["status"], "rebind")
        self.assertEqual(entry["desired"], {"flow_id": uid(30), "audio_flow_id": None})
        self.assertIn("audio_missing_optional", entry["reasons"])
        self.assertEqual(result["requests"][0]["body"], {"flow_id": uid(30), "audio_flow_id": None})

    def test_video_only_source_without_audio_role(self):
        flows = [video(31, "graphics-a", "Graphics Output 1")]
        entry = by_id(planner.plan(flows, mapping(source("gfx", "graphics-a", "Graphics Output 1", audio_role=None)),
                                   [core_source("gfx", 31)]))["gfx"]
        self.assertEqual(entry["status"], "noop")
        self.assertIsNone(entry["audio"])


class CompatibilityTest(unittest.TestCase):
    def check(self, flow, expected_reason_fragment):
        flows = [flow, audio(41, "contrib-a", "Camera Output 1")]
        entry = by_id(planner.plan(flows, mapping(source("cam-a", "contrib-a", "Camera Output 1")),
                                   [core_source("cam-a", 90, 91)]))["cam-a"]
        self.assertEqual(entry["status"], "incompatible")
        reasons = entry["video"]["incompatible"][0]["reasons"]
        self.assertTrue(any(expected_reason_fragment in reason for reason in reasons), reasons)

    def test_resolution_mismatch(self):
        self.check(video(40, "contrib-a", "Camera Output 1", width=1280, height=720), "width=1280")

    def test_cadence_mismatch(self):
        self.check(video(40, "contrib-a", "Camera Output 1", rate=(30000, 1001)), "rate='30000/1001'")

    def test_media_format_mismatch(self):
        self.check(video(40, "contrib-a", "Camera Output 1", media_type="video/v210a"), "media_type='video/v210a'")

    def test_audio_format_mismatch(self):
        flows = [video(40, "contrib-a", "Camera Output 1"), audio(41, "contrib-a", "Camera Output 1", sample_rate=44100)]
        entry = by_id(planner.plan(flows, mapping(source("cam-a", "contrib-a", "Camera Output 1")),
                                   [core_source("cam-a", 90, 91)]))["cam-a"]
        self.assertEqual(entry["status"], "incompatible")
        self.assertIn("audio_incompatible", entry["reasons"])

    def test_core_operating_point_mismatch_is_incompatible(self):
        flows = [video(40, "contrib-a", "Camera Output 1"), audio(41, "contrib-a", "Camera Output 1")]
        core = [core_source("cam-a", 90, 91)]
        core[0]["format"]["rate_numerator"], core[0]["format"]["rate_denominator"] = 30000, 1001
        entry = by_id(planner.plan(flows, mapping(source("cam-a", "contrib-a", "Camera Output 1")), core))["cam-a"]
        self.assertEqual(entry["status"], "incompatible")
        self.assertIn("video_differs_from_core_operating_point", entry["reasons"])


class DecisionTest(unittest.TestCase):
    def test_two_valid_candidates_are_ambiguous(self):
        flows = [video(50, "contrib-a", "Camera Output 1"), video(51, "contrib-a", "Camera Output 1"),
                 audio(52, "contrib-a", "Camera Output 1")]
        result = planner.plan(flows, mapping(source("cam-a", "contrib-a", "Camera Output 1")), [core_source("cam-a", 90, 91)])
        entry = by_id(result)["cam-a"]
        self.assertEqual(entry["status"], "ambiguous")
        self.assertEqual([c["flow_id"] for c in entry["video"]["compatible"]], [uid(50), uid(51)])
        self.assertEqual(result["requests"], [])

    def test_regenerated_uuids_rebind_the_same_stable_source(self):
        flows = [video(60, "contrib-a", "Camera Output 1"), audio(61, "contrib-a", "Camera Output 1"),
                 video(10, "contrib-a", "Camera Output 1", active=False, status="stale")]
        result = planner.plan(flows, mapping(source("cam-a", "contrib-a", "Camera Output 1")), [core_source("cam-a", 10, 11)])
        entry = by_id(result)["cam-a"]
        self.assertEqual(entry["status"], "rebind")
        self.assertEqual(entry["current"], {"flow_id": uid(10), "audio_flow_id": uid(11)})
        self.assertEqual(result["requests"], [{
            "source": "cam-a", "method": "PUT", "path": "/api/mxl/v1/sources/cam-a",
            "body": {"flow_id": uid(60), "audio_flow_id": uid(61)},
        }])

    def test_current_bindings_are_noop(self):
        flows = [video(70, "contrib-a", "Camera Output 1"), audio(71, "contrib-a", "Camera Output 1")]
        result = planner.plan(flows, mapping(source("cam-a", "contrib-a", "Camera Output 1")), [core_source("cam-a", 70, 71)])
        self.assertEqual(by_id(result)["cam-a"]["status"], "noop")
        self.assertEqual(result["requests"], [])
        self.assertFalse(result["blocked"])

    def test_unregistered_stable_source_is_create_required(self):
        flows = [video(72, "contrib-a", "Camera Output 1"), audio(73, "contrib-a", "Camera Output 1")]
        result = planner.plan(flows, mapping(source("cam-a", "contrib-a", "Camera Output 1")), [])
        self.assertEqual(by_id(result)["cam-a"]["status"], "create_required")
        self.assertEqual(result["requests"], [])


class SafetyTest(unittest.TestCase):
    def test_identical_match_rules_are_rejected(self):
        rules = mapping(source("cam-a", "contrib-a", "Camera Output 1"), source("cam-b", "contrib-a", "Camera Output 1"))
        with self.assertRaises(planner.PlanInputError):
            planner.plan([], rules, [])

    def test_flow_bound_to_another_core_source_is_not_stolen(self):
        flows = [video(80, "contrib-a", "Camera Output 1"), audio(81, "contrib-a", "Camera Output 1")]
        core = [core_source("cam-a", 90, 91), core_source("legacy", 80)]
        result = planner.plan(flows, mapping(source("cam-a", "contrib-a", "Camera Output 1")), core)
        entry = by_id(result)["cam-a"]
        self.assertEqual(entry["status"], "ambiguous")
        self.assertIn("video_flow_bound_to_other_source:legacy", entry["reasons"])
        self.assertEqual(result["requests"], [])

    def test_duplicate_video_assignment_rejected(self):
        entries = [
            {"id": "a", "status": "rebind", "reasons": [], "desired": {"flow_id": uid(1), "audio_flow_id": None}, "request": None},
            {"id": "b", "status": "rebind", "reasons": [], "desired": {"flow_id": uid(1), "audio_flow_id": None}, "request": None},
        ]
        planner._reject_conflicts(entries, {})
        self.assertEqual([e["status"] for e in entries], ["ambiguous", "ambiguous"])
        self.assertTrue(all("duplicate_video_assignment" in e["reasons"] for e in entries))

    def test_release_before_claim_ordering(self):
        # cam-b moves to a new flow, releasing 85 which cam-a now claims.
        flows = [video(85, "contrib-a", "Camera Output 1"), audio(86, "contrib-a", "Camera Output 1"),
                 video(87, "contrib-b", "Camera Output 1"), audio(88, "contrib-b", "Camera Output 1")]
        rules = mapping(source("cam-a", "contrib-a", "Camera Output 1"), source("cam-b", "contrib-b", "Camera Output 1"))
        core = [core_source("cam-a", 90, 91), core_source("cam-b", 85, 92)]
        result = planner.plan(flows, rules, core)
        self.assertEqual([r["source"] for r in result["requests"]], ["cam-b", "cam-a"])

    def test_binding_swap_cycle_requires_operator(self):
        flows = [video(1, "contrib-a", "Camera Output 1"), audio(2, "contrib-a", "Camera Output 1"),
                 video(3, "contrib-b", "Camera Output 1"), audio(4, "contrib-b", "Camera Output 1")]
        rules = mapping(source("cam-a", "contrib-a", "Camera Output 1"), source("cam-b", "contrib-b", "Camera Output 1"))
        core = [core_source("cam-a", 3, 4), core_source("cam-b", 1, 2)]
        result = planner.plan(flows, rules, core)
        self.assertEqual({e["status"] for e in result["sources"]}, {"ambiguous"})
        self.assertTrue(all("rebinding_cycle_requires_operator" in e["reasons"] for e in result["sources"]))
        self.assertEqual(result["requests"], [])

    def test_unattributed_and_unclaimed_flows_are_diagnostics(self):
        flows = [video(1, "contrib-a", "Camera Output 1"), audio(2, "contrib-a", "Camera Output 1"),
                 video(3, "contrib-a", "Camera Output 2"), video(4, None, "Camera Output 1")]
        result = planner.plan(flows, mapping(source("cam-a", "contrib-a", "Camera Output 1")), [core_source("cam-a", 1, 2)])
        kinds = [(d["kind"], d["flow_id"]) for d in result["diagnostics"]]
        self.assertEqual(kinds, [("active_flow_without_writer_attribution", uid(4)), ("unclaimed_active_flow", uid(3))])

    def test_mapping_requires_identity_fields_not_labels(self):
        bad = mapping({"id": "cam-a", "match": {"label": "Camera Output 1 Video"}})
        with self.assertRaises(planner.PlanInputError):
            planner.plan([], bad, [])


def rule(source_id, writer, group, video_role="Video 1", audio_role="Audio 1", required=None):
    entry = {"id": source_id, "match": {"writer_container": writer, "group": group,
                                        "video_role": video_role, "audio_role": audio_role}}
    if audio_role is None:
        del entry["match"]["audio_role"]
    if required is not None:
        entry["audio"] = {"required": required}
    return entry


class AudioOwnershipTest(unittest.TestCase):
    def test_two_sources_claiming_one_active_audio_flow_are_ambiguous(self):
        # Two video roles in one group, both (wrongly) mapped to the group's only audio role.
        flows = [video(1, "contrib-a", "Camera Output 1", role="Video 1"),
                 video(2, "contrib-a", "Camera Output 1", role="Video 2"),
                 audio(3, "contrib-a", "Camera Output 1")]
        rules = mapping(rule("cam-wide", "contrib-a", "Camera Output 1", video_role="Video 1"),
                        rule("cam-tight", "contrib-a", "Camera Output 1", video_role="Video 2"))
        result = planner.plan(flows, rules, [core_source("cam-wide", 90), core_source("cam-tight", 91)])
        entries = by_id(result)
        for source_id in ("cam-wide", "cam-tight"):
            self.assertEqual(entries[source_id]["status"], "ambiguous")
            self.assertIn("duplicate_audio_assignment", entries[source_id]["reasons"])
        self.assertEqual(result["requests"], [])
        self.assertTrue(result["blocked"])

    def test_video_from_other_writer_never_borrows_audio(self):
        # contrib-b publishes the same group name but no audio; it must not take contrib-a's.
        flows = [video(1, "contrib-a", "Camera Output 1"), audio(2, "contrib-a", "Camera Output 1"),
                 video(3, "contrib-b", "Camera Output 1")]
        rules = mapping(rule("cam-a", "contrib-a", "Camera Output 1"), rule("cam-b", "contrib-b", "Camera Output 1"))
        entries = by_id(planner.plan(flows, rules, [core_source("cam-a", 1), core_source("cam-b", 3)]))
        self.assertEqual(entries["cam-a"]["desired"], {"flow_id": uid(1), "audio_flow_id": uid(2)})
        self.assertEqual(entries["cam-b"]["status"], "missing")
        self.assertIn("audio_missing", entries["cam-b"]["reasons"])

    def test_stale_audio_copy_cannot_reserve_ownership(self):
        flows = [video(1, "contrib-a", "Camera Output 1"),
                 audio(2, "contrib-a", "Camera Output 1", active=False, status="stale"),
                 audio(3, "contrib-a", "Camera Output 1")]
        # Core still records the stale audio on an unrelated legacy source.
        core = [core_source("cam-a", 1), core_source("legacy", 50, 2)]
        result = planner.plan(flows, mapping(rule("cam-a", "contrib-a", "Camera Output 1")), core)
        entry = by_id(result)["cam-a"]
        self.assertEqual(entry["status"], "rebind")
        self.assertEqual(entry["desired"], {"flow_id": uid(1), "audio_flow_id": uid(3)})
        self.assertEqual([c["flow_id"] for c in entry["audio"]["not_active"]], [uid(2)])

    def test_optional_null_audio_never_collides(self):
        flows = [video(1, "gfx-a", "Graphics 1"), video(2, "gfx-b", "Graphics 1")]
        rules = mapping(rule("gfx-1", "gfx-a", "Graphics 1", required=False),
                        rule("gfx-2", "gfx-b", "Graphics 1", required=False))
        result = planner.plan(flows, rules, [core_source("gfx-1", 90, 91), core_source("gfx-2", 92, 93)])
        entries = by_id(result)
        for source_id, flow in (("gfx-1", 1), ("gfx-2", 2)):
            self.assertEqual(entries[source_id]["status"], "rebind")
            self.assertEqual(entries[source_id]["desired"], {"flow_id": uid(flow), "audio_flow_id": None})
        self.assertFalse(result["blocked"])

    def test_distinct_audio_pairs_are_valid(self):
        flows = [video(1, "contrib-a", "Camera Output 1"), audio(2, "contrib-a", "Camera Output 1"),
                 video(3, "contrib-a", "Camera Output 2"), audio(4, "contrib-a", "Camera Output 2")]
        rules = mapping(rule("cam-1", "contrib-a", "Camera Output 1"), rule("cam-2", "contrib-a", "Camera Output 2"))
        result = planner.plan(flows, rules, [core_source("cam-1", 1), core_source("cam-2", 3)])
        self.assertEqual({e["status"] for e in result["sources"]}, {"rebind"})
        self.assertEqual([r["body"]["audio_flow_id"] for r in result["requests"]], [uid(2), uid(4)])

    def test_audio_owned_by_another_core_source_is_not_stolen(self):
        flows = [video(1, "contrib-a", "Camera Output 1"), audio(2, "contrib-a", "Camera Output 1")]
        core = [core_source("cam-a", 1), core_source("program-audio", 60, 2)]
        entry = by_id(planner.plan(flows, mapping(rule("cam-a", "contrib-a", "Camera Output 1")), core))["cam-a"]
        self.assertEqual(entry["status"], "ambiguous")
        self.assertIn("audio_flow_bound_to_other_source:program-audio", entry["reasons"])

    def test_audio_released_before_it_is_claimed(self):
        # cam-b moves to its new audio, releasing the flow cam-a now claims.
        flows = [video(1, "contrib-a", "Camera Output 1"), audio(2, "contrib-a", "Camera Output 1"),
                 video(3, "contrib-b", "Camera Output 1"), audio(4, "contrib-b", "Camera Output 1")]
        rules = mapping(rule("cam-a", "contrib-a", "Camera Output 1"), rule("cam-b", "contrib-b", "Camera Output 1"))
        core = [core_source("cam-a", 1), core_source("cam-b", 3, 2)]
        result = planner.plan(flows, rules, core)
        self.assertEqual([r["source"] for r in result["requests"]], ["cam-b", "cam-a"])

    def test_audio_swap_cycle_requires_operator(self):
        flows = [video(1, "contrib-a", "Camera Output 1"), audio(2, "contrib-a", "Camera Output 1"),
                 video(3, "contrib-b", "Camera Output 1"), audio(4, "contrib-b", "Camera Output 1")]
        rules = mapping(rule("cam-a", "contrib-a", "Camera Output 1"), rule("cam-b", "contrib-b", "Camera Output 1"))
        core = [core_source("cam-a", 1, 4), core_source("cam-b", 3, 2)]
        result = planner.plan(flows, rules, core)
        self.assertEqual({e["status"] for e in result["sources"]}, {"ambiguous"})
        self.assertEqual(result["requests"], [])

    def test_claim_relying_on_a_blocked_release_is_also_blocked(self):
        # cam-a claims the video cam-b holds; cam-b would move away, but cam-b is itself
        # blocked by an audio conflict, so cam-a must not proceed either.
        flows = [video(1, "contrib-a", "Camera Output 1"), audio(2, "contrib-a", "Camera Output 1"),
                 video(3, "contrib-b", "Camera Output 1"), audio(4, "contrib-b", "Camera Output 1")]
        rules = mapping(rule("cam-a", "contrib-a", "Camera Output 1"), rule("cam-b", "contrib-b", "Camera Output 1"))
        core = [core_source("cam-a", 90), core_source("cam-b", 1), core_source("program-audio", 61, 4)]
        entries = by_id(planner.plan(flows, rules, core))
        self.assertEqual(entries["cam-b"]["status"], "ambiguous")
        self.assertEqual(entries["cam-a"]["status"], "ambiguous")
        self.assertIn("video_flow_bound_to_other_source:cam-b", entries["cam-a"]["reasons"])

    def test_conflict_plans_are_byte_stable_under_shuffling(self):
        flows = [video(1, "contrib-a", "Camera Output 1", role="Video 1"),
                 video(2, "contrib-a", "Camera Output 1", role="Video 2"),
                 audio(3, "contrib-a", "Camera Output 1"),
                 audio(5, "contrib-a", "Camera Output 1", active=False, status="stale"),
                 video(6, None, "Camera Output 9")]
        rules = mapping(rule("cam-wide", "contrib-a", "Camera Output 1", video_role="Video 1"),
                        rule("cam-tight", "contrib-a", "Camera Output 1", video_role="Video 2"))
        core = [core_source("cam-wide", 90), core_source("cam-tight", 91), core_source("legacy", 92, 3)]
        expected = planner.render(planner.plan(flows, rules, core))
        rng = random.Random(11)
        for _ in range(8):
            f, r, c = copy.deepcopy(flows), copy.deepcopy(rules), copy.deepcopy(core)
            rng.shuffle(f); rng.shuffle(r["sources"]); rng.shuffle(c)
            self.assertEqual(planner.render(planner.plan(f, r, c)), expected)


class DeterminismTest(unittest.TestCase):
    def test_output_is_byte_stable_under_input_reordering(self):
        flows = json.loads((FIXTURES / "flows.json").read_text())
        rules = json.loads((FIXTURES / "mapping.json").read_text())
        core = json.loads((FIXTURES / "core-sources.json").read_text())
        expected = planner.render(planner.plan(flows, rules, core))
        rng = random.Random(4)
        for _ in range(5):
            shuffled_flows = copy.deepcopy(flows)
            shuffled_rules = copy.deepcopy(rules)
            shuffled_core = copy.deepcopy(core)
            rng.shuffle(shuffled_flows)
            rng.shuffle(shuffled_rules["sources"])
            rng.shuffle(shuffled_core)
            self.assertEqual(planner.render(planner.plan(shuffled_flows, shuffled_rules, shuffled_core)), expected)

    def test_fixture_matches_golden_plan(self):
        flows = json.loads((FIXTURES / "flows.json").read_text())
        rules = json.loads((FIXTURES / "mapping.json").read_text())
        core = json.loads((FIXTURES / "core-sources.json").read_text())
        self.assertEqual(planner.render(planner.plan(flows, rules, core)), (FIXTURES / "expected-plan.json").read_text())


class CliTest(unittest.TestCase):
    def run_cli(self, args, env=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            if env is None:
                status = planner.main(args)
            else:
                saved = dict(os.environ)
                os.environ.update(env)
                try:
                    status = planner.main(args)
                finally:
                    os.environ.clear()
                    os.environ.update(saved)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_file_mode_exit_status_reflects_blocking_entries(self):
        status, out, _ = self.run_cli(["--mapping", str(FIXTURES / "mapping.json"),
                                       "--inventory", str(FIXTURES / "flows.json"),
                                       "--core-snapshot", str(FIXTURES / "core-sources.json")])
        self.assertEqual(out, (FIXTURES / "expected-plan.json").read_text())
        self.assertEqual(status, 1 if json.loads(out)["blocked"] else 0)

    def test_bad_mapping_is_an_input_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory, "mapping.json")
            path.write_text(json.dumps({"version": 2}))
            status, _, err = self.run_cli(["--mapping", str(path), "--inventory", str(FIXTURES / "flows.json"),
                                           "--core-snapshot", str(FIXTURES / "core-sources.json")])
        self.assertEqual(status, 2)
        self.assertIn("version 1", err)

    def test_http_mode_is_get_only_and_never_echoes_the_token(self):
        token = "synthetic-test-token-123"
        seen = []
        flows_body = (FIXTURES / "flows.json").read_bytes()
        core_body = (FIXTURES / "core-sources.json").read_bytes()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                seen.append((self.command, self.path, self.headers.get("Authorization")))
                body = flows_body if self.path.startswith("/api/domains/") else core_body
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            status, out, err = self.run_cli(
                ["--mapping", str(FIXTURES / "mapping.json"), "--easy-mxl-url", base,
                 "--domain", "example-domain", "--core-url", base, "--token-env", "TEST_EASY_MXL_TOKEN"],
                env={"TEST_EASY_MXL_TOKEN": token})
        finally:
            server.shutdown()
            server.server_close()
        self.assertIn(status, (0, 1))
        self.assertEqual({method for method, _, _ in seen}, {"GET"})
        self.assertIn(("GET", "/api/domains/example-domain/flows", f"Bearer {token}"), seen)
        self.assertIn(("GET", "/api/mxl/v1/sources", None), seen)   # the token is for EasyMXL only
        self.assertNotIn(token, out + err)

    def test_http_errors_redact_token_query(self):
        self.assertEqual(planner._redact("http://user:pw@host:1/a?token=secret&x=1"), "http://host:1/a?token=REDACTED&x=1")


if __name__ == "__main__":
    unittest.main()
