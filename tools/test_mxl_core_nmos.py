#!/usr/bin/env python3
"""Focused tests for the MXL Core NMOS routing bridge."""

from __future__ import annotations

import importlib.util
import pathlib
import unittest


SCRIPT = pathlib.Path(__file__).with_name("mxl_core_nmos.py")
SPEC = importlib.util.spec_from_file_location("mxl_core_nmos", SCRIPT)
NMOS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(NMOS)


class FakeModel(NMOS.Model):
    def __init__(self):
        super().__init__("http://core", "http://node:8021/", 8021, "domain-1")
        self.sources_data = [
            {
                "id": "camera-1",
                "flow_id": "flow-1",
                "label": "Camera 1",
                "kind": "camera",
                "capabilities": ["video", "audio"],
                "format": {
                    "width": 1920,
                    "height": 1080,
                    "rate_numerator": 30,
                    "rate_denominator": 1,
                    "encoding": "video/v210",
                },
            },
            {
                "id": "camera-2",
                "flow_id": "flow-2",
                "label": "Camera 2",
                "format": {
                    "width": 1920,
                    "height": 1080,
                    "rate_numerator": 30,
                    "rate_denominator": 1,
                    "encoding": "video/v210",
                },
            },
        ]
        self.core = {"program": "camera-1", "preview": "camera-2"}
        self.posts = []

    def post_json(self, path, value):
        self.posts.append((path, value))
        bus = "program" if path.endswith("/cut") else "preview"
        self.core[bus] = value["source"]
        return 202, {"command_id": 1, "accepted_revision": 1}

    def refresh(self):
        pass


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.model = FakeModel()

    def test_device_advertises_is05_control(self):
        self.assertEqual(
            self.model.device()["controls"],
            [
                {
                    "type": "urn:x-nmos:control:sr-ctrl/v1.1",
                    "href": "http://node:8021/x-nmos/connection/v1.1/",
                }
            ],
        )

    def test_default_instance_preserves_legacy_resource_ids(self):
        self.assertEqual(self.model.node_id, NMOS.stable("node", "facility"))
        self.assertEqual(self.model.device_id, NMOS.stable("device", "switcher"))
        self.assertEqual(
            self.model.senders()[0]["id"], NMOS.stable("sender", "camera-1")
        )

    def test_parallel_instances_have_disjoint_resource_ids(self):
        first = FakeModel()
        second = NMOS.Model(
            "http://core",
            "http://node:8022/",
            8022,
            "domain-1",
            instance_id="qualification-gate",
        )
        second.sources_data = first.sources_data
        second.core = first.core

        first_ids = {
            first.node_id,
            first.device_id,
            *(value["id"] for value in first.sources()),
            *(value["id"] for value in first.flows()),
            *(value["id"] for value in first.senders()),
            *(value["id"] for value in first.receivers()),
        }
        second_ids = {
            second.node_id,
            second.device_id,
            *(value["id"] for value in second.sources()),
            *(value["id"] for value in second.flows()),
            *(value["id"] for value in second.senders()),
            *(value["id"] for value in second.receivers()),
        }
        self.assertTrue(first_ids.isdisjoint(second_ids))
        self.assertNotEqual(
            first.attached_sender("program"), second.attached_sender("program")
        )
        self.assertEqual(
            second.source_name_for_sender(second.attached_sender("program")),
            "camera-1",
        )

    def test_program_receiver_activation_maps_to_cut(self):
        sender = NMOS.stable("sender", "camera-2")
        result = self.model.activate_receiver(
            "program",
            {
                "sender_id": sender,
                "master_enable": True,
                "activation": {"mode": "activate_immediate"},
            },
        )
        self.assertEqual(
            self.model.posts,
            [("/api/mxl/v1/cut", {"source": "camera-2"})],
        )
        self.assertEqual(result["sender_id"], sender)
        self.assertEqual(result["activation"]["mode"], "activate_immediate")
        self.assertEqual(
            result["transport_params"],
            [{"mxl_domain_id": "domain-1", "mxl_flow_id": "flow-2"}],
        )

    def test_transport_parameters_can_route_without_sender_id(self):
        result = self.model.activate_receiver(
            "program",
            {
                "transport_params": [
                    {"mxl_domain_id": "domain-1", "mxl_flow_id": "flow-2"}
                ],
                "master_enable": True,
                "activation": {"mode": "activate_immediate"},
            },
        )
        self.assertEqual(
            self.model.posts,
            [("/api/mxl/v1/cut", {"source": "camera-2"})],
        )
        self.assertEqual(result["sender_id"], NMOS.stable("sender", "camera-2"))

    def test_receiver_tags_preserve_program_preview_grouping(self):
        receivers = {value["label"]: value for value in self.model.receivers()}
        self.assertEqual(
            receivers["MXL Core Program"]["tags"]["urn:x-nmos:tag:grouphint/v1.0"],
            ["PGM"],
        )
        self.assertEqual(
            receivers["MXL Core Preview"]["tags"]["urn:x-nmos:tag:grouphint/v1.0"],
            ["PVW"],
        )

    def test_dynamic_source_metadata_is_exposed_as_nmos_tags(self):
        source = next(
            value for value in self.model.sources() if value["label"] == "Camera 1"
        )
        sender = next(
            value for value in self.model.senders() if value["label"] == "Camera 1"
        )
        expected = {
            "urn:x-mxl:tag:source_kind": ["camera"],
            "urn:x-mxl:tag:capabilities": ["video", "audio"],
        }
        self.assertEqual(source["tags"], expected)
        self.assertEqual(sender["tags"]["urn:x-mxl:tag:source_kind"], ["camera"])
        self.assertEqual(sender["tags"]["urn:x-mxl:tag:capabilities"], ["video", "audio"])

    def test_registry_withdraws_dynamic_resources_in_dependency_order(self):
        registered = {
            "node": {"node"},
            "device": {"device"},
            "source": {"source-kept", "source-gone"},
            "flow": {"flow-kept", "flow-gone"},
            "sender": {"sender-kept", "sender-gone"},
            "receiver": {"receiver"},
        }
        current = {
            "node": {"node"},
            "device": {"device"},
            "source": {"source-kept"},
            "flow": {"flow-kept"},
            "sender": {"sender-kept"},
            "receiver": {"receiver"},
        }
        self.assertEqual(
            NMOS.withdrawal_plan(registered, current),
            [
                ("sender", "sender-gone"),
                ("flow", "flow-gone"),
                ("source", "source-gone"),
            ],
        )

    def test_preview_receiver_activation_maps_to_preview(self):
        sender = NMOS.stable("sender", "camera-1")
        self.model.activate_receiver(
            "preview",
            {"sender_id": sender, "activation": {"mode": "activate_immediate"}},
        )
        self.assertEqual(
            self.model.posts,
            [("/api/mxl/v1/preview", {"source": "camera-1"})],
        )

    def test_disconnect_and_scheduled_activation_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "cannot be disconnected"):
            self.model.activate_receiver(
                "program",
                {
                    "sender_id": NMOS.stable("sender", "camera-1"),
                    "master_enable": False,
                },
            )
        with self.assertRaisesRegex(ValueError, "activate_immediate"):
            self.model.activate_receiver(
                "program",
                {
                    "sender_id": NMOS.stable("sender", "camera-1"),
                    "activation": {"mode": "activate_scheduled_relative"},
                },
            )


if __name__ == "__main__":
    unittest.main()
