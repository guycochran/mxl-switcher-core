import json
import pathlib
import struct
import tempfile
import unittest

from mxl_audio_meter import AudioRing, HEAD_INDEX_OFFSET


class AudioRingTest(unittest.TestCase):
    def test_reads_stereo_float32_ring(self):
        with tempfile.TemporaryDirectory() as temporary:
            domain = pathlib.Path(temporary)
            flow = domain / "flow-id.mxl-flow"
            flow.mkdir()
            (flow / "flow_def.json").write_text(json.dumps({
                "media_type": "audio/float32",
                "channel_count": 2,
                "bit_depth": 32,
            }))
            metadata = bytearray(512)
            struct.pack_into("<Q", metadata, HEAD_INDEX_OFFSET, 7)
            (flow / "data").write_bytes(metadata)
            left = [0.25] * 8
            right = [0.5] * 8
            (flow / "channels").write_bytes(struct.pack("<16f", *(left + right)))

            value = AudioRing(domain, "zoomiso-1", "flow-id", 4).sample()

            self.assertEqual(value["state"], "ok")
            self.assertEqual(value["head_index"], 7)
            self.assertEqual(value["peak_dbfs"], [-12.0, -6.0])
            self.assertEqual(value["rms_dbfs"], [-12.0, -6.0])


if __name__ == "__main__":
    unittest.main()
