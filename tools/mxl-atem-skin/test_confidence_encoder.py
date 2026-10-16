import importlib.util
import pathlib
import sys
from types import SimpleNamespace
import unittest
import unittest.mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
if HAVE_NUMPY:
    import numpy as np

    import mxl_confidence_encoder as enc


def pack_v210(y, cb, cr):
    """Pack 10-bit 4:2:2 planes (Y: HxW, Cb/Cr: HxW/2) into (H, W/6*4) words."""

    h, w = y.shape
    y = y.astype(np.uint32).reshape(h, w // 6, 6)
    cb = cb.astype(np.uint32).reshape(h, w // 6, 3)
    cr = cr.astype(np.uint32).reshape(h, w // 6, 3)
    words = np.empty((h, w // 6, 4), np.uint32)
    words[:, :, 0] = cb[:, :, 0] | (y[:, :, 0] << 10) | (cr[:, :, 0] << 20)
    words[:, :, 1] = y[:, :, 1] | (cb[:, :, 1] << 10) | (y[:, :, 2] << 20)
    words[:, :, 2] = cr[:, :, 1] | (y[:, :, 3] << 10) | (cb[:, :, 2] << 20)
    words[:, :, 3] = y[:, :, 4] | (cr[:, :, 2] << 10) | (y[:, :, 5] << 20)
    return words.reshape(h, -1)


@unittest.skipUnless(HAVE_NUMPY, "numpy is required for the confidence encoder")
class DecimateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rng = np.random.default_rng(7)
        cls.y = rng.integers(64, 940, (1080, 1920))
        cls.cb = rng.integers(64, 960, (1080, 960))
        cls.cr = rng.integers(64, 960, (1080, 960))
        cls.words = pack_v210(cls.y, cls.cb, cls.cr)

    def test_exact_decimation_at_each_factor(self):
        for factor, (w, h) in {2: (960, 540), 3: (640, 360), 6: (320, 180)}.items():
            with self.subTest(factor=factor):
                y, u, v = enc.decimate(self.words, factor)
                self.assertEqual(y.shape, (h, w))
                self.assertEqual(u.shape, (h // 2, w // 2))
                columns = np.arange(0, 1920, factor)
                np.testing.assert_array_equal(y, (self.y[::factor, ::factor] >> 2).astype(np.uint8))
                expected_u = (self.cb[::factor][:, columns // 2] >> 2)[::2, ::2]
                expected_v = (self.cr[::factor][:, columns // 2] >> 2)[::2, ::2]
                np.testing.assert_array_equal(u, expected_u.astype(np.uint8))
                np.testing.assert_array_equal(v, expected_v.astype(np.uint8))

    def test_rejects_unsupported_factor(self):
        with self.assertRaises(ValueError):
            enc.decimate(self.words, 4)

    def test_zero_copy_view_of_payload_address(self):
        buffer = np.ascontiguousarray(self.words, dtype=np.uint32)
        view = enc.words_at(buffer.ctypes.data)
        self.assertEqual(view.shape, (1080, enc.WORDS_PER_ROW))
        self.assertTrue(np.shares_memory(view, buffer) or view.ctypes.data == buffer.ctypes.data)
        np.testing.assert_array_equal(view, buffer)


@unittest.skipUnless(HAVE_NUMPY, "numpy is required for the confidence encoder")
class WallGeometryTest(unittest.TestCase):
    def test_matches_skin_overlay_inset_and_grid(self):
        # The skin overlays a 4x2 click grid inset 19.4% from top and bottom.
        self.assertAlmostEqual(enc.WALL_TOP / enc.WALL_HEIGHT, 0.194, places=3)
        self.assertEqual(enc.tile_origin(0), (0, enc.WALL_TOP))
        self.assertEqual(enc.tile_origin(3), (960, enc.WALL_TOP))
        self.assertEqual(enc.tile_origin(5), (320, enc.WALL_TOP + 220))
        bottom = enc.tile_origin(7)[1] + enc.TILE_VIDEO_HEIGHT + enc.TILE_BAR_HEIGHT
        self.assertEqual(enc.WALL_HEIGHT - bottom, enc.WALL_TOP)

    def test_pipeline_is_baseline_rtmp_without_bframes(self):
        description = enc.pipeline_description(1280, 720, 10, 1500, "rtmp://127.0.0.1:1935/mxl-multiview")
        self.assertIn("profile=constrained-baseline", description)
        self.assertIn("tune=zerolatency", description)
        self.assertIn("key-int-max=20", description)
        self.assertIn('rtmpsink location="rtmp://127.0.0.1:1935/mxl-multiview live=1"', description)


@unittest.skipUnless(HAVE_NUMPY, "numpy is required for the confidence encoder")
class SourceReadRaceTest(unittest.TestCase):
    class Reader:
        def __init__(self, results, heads):
            self.results = iter(results)
            self.heads = iter(heads)

        def head(self):
            return next(self.heads)

        def read(self, _head, _timeout, _sample):
            return next(self.results)

    def source(self, results, heads):
        source = enc.Source(None, None, "domain", "label", "flow", 6)
        source.reader = self.Reader(results, heads)
        source.grain_count = 6
        source.grain_rate = 30000 / 1001
        return source

    def poll(self, source, now, clock_values):
        clock = iter(clock_values)
        with unittest.mock.patch.object(enc.time, "monotonic", lambda: next(clock)):
            source.poll(now)

    def test_one_torn_read_retries_fresh_head_and_recovers(self):
        planes = (object(), object(), object())
        source = self.source([
            SimpleNamespace(status=enc.mxl.MXL_STATUS_OK, torn=True, value=None),
            SimpleNamespace(status=enc.mxl.MXL_STATUS_OK, torn=False, value=planes),
        ], [100, 101])

        self.poll(source, 12.5, [10.0, 10.002, 10.003])

        self.assertIs(source.planes, planes)
        self.assertEqual(source.updated, 12.5)
        self.assertEqual(source.stats["reads"], 2)
        self.assertEqual(source.stats["torn_retries"], 1)
        self.assertEqual(source.stats["torn_recovered"], 1)
        self.assertEqual(source.stats["torn"], 0)
        self.assertEqual(source.stats["torn_reader_stall"], 1)

    def test_two_torn_reads_report_one_terminal_torn(self):
        torn = SimpleNamespace(status=enc.mxl.MXL_STATUS_OK, torn=True, value=None)
        source = self.source([torn, torn], [100, 101, 107])

        self.poll(source, 12.5, [10.0, 10.002, 10.003, 10.005])

        self.assertIsNone(source.planes)
        self.assertEqual(source.stats["reads"], 2)
        self.assertEqual(source.stats["torn_retries"], 1)
        self.assertEqual(source.stats["torn_recovered"], 0)
        self.assertEqual(source.stats["torn"], 1)
        self.assertEqual(source.stats["torn_reader_stall"], 1)
        self.assertEqual(source.stats["torn_writer_jump"], 1)

    def test_head_jump_is_reported_between_polls(self):
        ok = SimpleNamespace(status=enc.mxl.MXL_STATUS_OK, torn=False,
                             value=(object(), object(), object()))
        source = self.source([ok, ok], [100, 112])
        self.poll(source, 10.0, [1.0])
        self.poll(source, 10.1, [2.0])
        self.assertEqual(source.stats["head_jumps"], 1)


if __name__ == "__main__":
    unittest.main()
