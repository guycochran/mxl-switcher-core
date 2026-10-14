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
