import ctypes
import pathlib
import sys
import unittest
from array import array

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import av_reference as ref  # noqa: E402
import mxl_direct as mxl  # noqa: E402


class FakeRing:
    """A discrete ring with the MXL reader call shapes used by mxl_direct."""

    def __init__(self, grain_count=6, size=64):
        self.grain_count = grain_count
        self.slots = [(ctypes.create_string_buffer(size), None) for _ in range(grain_count)]
        self.head = 0

    def write(self, index, payload=b""):
        buffer, _ = self.slots[index % self.grain_count]
        ctypes.memmove(buffer, payload, len(payload))
        self.slots[index % self.grain_count] = (buffer, index)
        self.head = index

    # --- API surface -------------------------------------------------------
    def mxlCreateFlowReader(self, instance, flow, options, handle):
        handle._obj.value = 1
        return mxl.MXL_STATUS_OK

    def mxlReleaseFlowReader(self, instance, handle):
        return mxl.MXL_STATUS_OK

    def mxlFlowReaderGetRuntimeInfo(self, handle, info):
        info._obj.headIndex = self.head
        return mxl.MXL_STATUS_OK

    def _grain(self, index, info, payload):
        if index > self.head:
            return mxl.MXL_ERR_OUT_OF_RANGE_TOO_EARLY
        buffer, held = self.slots[index % self.grain_count]
        if held != index:
            return mxl.MXL_ERR_OUT_OF_RANGE_TOO_LATE
        info._obj.index = held
        info._obj.grainSize = len(buffer)
        payload._obj.value = ctypes.addressof(buffer)
        return mxl.MXL_STATUS_OK

    def mxlFlowReaderGetGrain(self, handle, index, timeout, info, payload):
        return self._grain(index, info, payload)

    def mxlFlowReaderGetGrainNonBlocking(self, handle, index, info, payload):
        return self._grain(index, info, payload)


class FakeSamples:
    """A planar two-channel Float32 ring of ``length`` samples."""

    def __init__(self, length=16):
        self.length = length
        self.channels = [(ctypes.c_float * length)(), (ctypes.c_float * length)()]
        self.head = 0

    def write_through(self, end):
        for index in range(self.head, end):
            self.channels[0][index % self.length] = float(index)
            self.channels[1][index % self.length] = float(-index)
        self.head = end

    def mxlCreateFlowReader(self, instance, flow, options, handle):
        handle._obj.value = 1
        return mxl.MXL_STATUS_OK

    def mxlFlowReaderGetRuntimeInfo(self, handle, info):
        info._obj.headIndex = self.head
        return mxl.MXL_STATUS_OK

    def mxlFlowReaderGetSamples(self, handle, end, count, timeout, slices):
        if end > self.head:
            return mxl.MXL_ERR_OUT_OF_RANGE_TOO_EARLY
        start = end - count
        first = start % self.length
        first_count = min(count, self.length - first)
        base = ctypes.addressof(self.channels[0])
        stride = ctypes.addressof(self.channels[1]) - base
        slices._obj.fragments[0].pointer = base + 4 * first
        slices._obj.fragments[0].size = 4 * first_count
        slices._obj.fragments[1].pointer = base
        slices._obj.fragments[1].size = 4 * (count - first_count)
        slices._obj.stride = stride
        slices._obj.count = 2
        return mxl.MXL_STATUS_OK


def first_byte(address, size):
    return ctypes.string_at(address, 1)[0]


class GrainReaderTest(unittest.TestCase):
    def test_accepts_a_stable_grain(self):
        ring = FakeRing()
        for index in range(100, 104):
            ring.write(index, bytes([index % 256]))
        reader = mxl.GrainReader(ring, None, "flow", ring.grain_count)
        result = reader.read(103, 0, first_byte)
        self.assertEqual((result.status, result.value, result.torn), (mxl.MXL_STATUS_OK, 103, False))

    def test_reports_torn_when_the_writer_laps_during_sampling(self):
        ring = FakeRing()
        for index in range(100, 104):
            ring.write(index, bytes([1]))
        reader = mxl.GrainReader(ring, None, "flow", ring.grain_count)

        def preempted(address, size):
            value = ctypes.string_at(address, 1)[0]
            for index in range(104, 111):  # the writer reuses slot 104 % 6 for 110
                ring.write(index, bytes([9]))
            return value

        ring.write(104, bytes([1]))
        result = reader.read(104, 0, preempted)
        self.assertTrue(result.torn)
        self.assertIsNone(result.value)

    def test_head_margin_rejects_a_slot_the_writer_may_have_opened(self):
        ring = FakeRing()
        for index in range(100, 105):
            ring.write(index, bytes([1]))
        reader = mxl.GrainReader(ring, None, "flow", ring.grain_count)
        # Slot of 100 is intact, but head 104 means grain 106 may already be open on it.
        self.assertFalse(reader.read(104, 0, first_byte).torn)
        ring.write(105, b"")
        self.assertTrue(reader.read(100, 0, first_byte).torn)

    def test_decodes_frame_code_in_place(self):
        width, height = 480, 270
        grain = 53_760_658_271
        luma = bytearray([40]) * (width * height)
        sx, sy = width / ref.REFERENCE_WIDTH, height / ref.REFERENCE_HEIGHT
        for (x0, y0, x1, y1), bit in ref.code_blocks(grain):
            for y in range(round(y0 * sy), round(y1 * sy)):
                luma[y * width + round(x0 * sx):y * width + round(x1 * sx)] = bytes([235 if bit else 16]) * (round(x1 * sx) - round(x0 * sx))
        packed = bytes(ref.v210_from_luma(luma, width, height))
        ring = FakeRing(size=len(packed))
        ring.write(grain, packed)
        reader = mxl.GrainReader(ring, None, "flow", ring.grain_count)
        sampler = mxl.v210_code_sampler(lambda view: ref.decode_v210_frame_code(view, width, height))
        result = reader.read(grain, 0, sampler)
        self.assertEqual(result.value, grain % ref.CODE_MODULUS)


class SampleReaderTest(unittest.TestCase):
    def test_assembles_wrapped_planar_channels(self):
        ring = FakeSamples(length=16)
        ring.write_through(20)  # indices 4..19 retained; slot 0 holds 16
        reader = mxl.SampleReader(ring, None, "flow", ring.length)
        status, channels, torn = reader.read(19, 6, 0, 2)  # samples 13..18 wrap the ring
        self.assertEqual((status, torn), (mxl.MXL_STATUS_OK, False))
        self.assertEqual(list(array("f", channels[0])), [13.0, 14.0, 15.0, 16.0, 17.0, 18.0])
        self.assertEqual(list(array("f", channels[1])), [-13.0, -14.0, -15.0, -16.0, -17.0, -18.0])

    def test_reports_torn_when_range_may_be_overwritten(self):
        ring = FakeSamples(length=16)
        ring.write_through(40)
        reader = mxl.SampleReader(ring, None, "flow", ring.length)
        status, channels, torn = reader.read(30, 6, 0, 2)  # starts at 24, head 40 - 24 >= 16
        self.assertTrue(torn)
        self.assertIsNone(channels)


class LayoutTest(unittest.TestCase):
    def test_struct_sizes_match_the_sdk(self):
        self.assertEqual(ctypes.sizeof(mxl.GrainInfo), 4096)
        self.assertEqual(ctypes.sizeof(mxl.RuntimeInfo), 64)
        self.assertEqual(ctypes.sizeof(mxl.FlowConfigInfo), 192)
        self.assertEqual(ctypes.sizeof(mxl.WrappedMultiBufferSlice), 48)
        self.assertEqual(mxl.FlowConfigInfo.u.offset, 128)


if __name__ == "__main__":
    unittest.main()
