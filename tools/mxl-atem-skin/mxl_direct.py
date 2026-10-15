"""Minimal ctypes binding to the MXL reader API for measurement tools.

Reads grains and samples in place from the flow rings and validates them
*after* reading, so a reader preempted while the writer laps the ring reports a
torn read instead of emitting a newer grain under an older index (the race seen
through generic GStreamer readers on a CPU-contended host). Only the pixels a
caller asks for are touched, so a 1080p grain costs a few hundred reads rather
than a 5.5 MB copy.

Standard library only; the MXL SDK shared library is loaded at runtime.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass

MXL_STATUS_OK = 0
MXL_ERR_OUT_OF_RANGE_TOO_LATE = 3
MXL_ERR_OUT_OF_RANGE_TOO_EARLY = 4
MXL_ERR_TIMEOUT = 7
MXL_ERR_FLOW_INVALID = 11
MXL_GRAIN_FLAG_INVALID = 0x1


class GrainInfo(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("size", ctypes.c_uint32),
        ("index", ctypes.c_uint64),
        ("flags", ctypes.c_uint32),
        ("grainSize", ctypes.c_uint32),
        ("totalSlices", ctypes.c_uint16),
        ("validSlices", ctypes.c_uint16),
        ("reserved", ctypes.c_uint8 * 4068),
    ]


class RuntimeInfo(ctypes.Structure):
    _fields_ = [
        ("headIndex", ctypes.c_uint64),
        ("lastWriteTime", ctypes.c_uint64),
        ("lastReadTime", ctypes.c_uint64),
        ("reserved", ctypes.c_uint8 * 40),
    ]


class BufferSlice(ctypes.Structure):
    _fields_ = [("pointer", ctypes.c_void_p), ("size", ctypes.c_size_t)]


class WrappedMultiBufferSlice(ctypes.Structure):
    _fields_ = [
        ("fragments", BufferSlice * 2),
        ("stride", ctypes.c_size_t),
        ("count", ctypes.c_size_t),
    ]


class Rational(ctypes.Structure):
    _fields_ = [("numerator", ctypes.c_int64), ("denominator", ctypes.c_int64)]


class CommonConfig(ctypes.Structure):
    _fields_ = [
        ("id", ctypes.c_uint8 * 16),
        ("format", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("grainRate", Rational),
        ("maxCommitBatchSizeHint", ctypes.c_uint32),
        ("maxSyncBatchSizeHint", ctypes.c_uint32),
        ("payloadLocation", ctypes.c_uint32),
        ("deviceIndex", ctypes.c_int32),
        ("reserved", ctypes.c_uint8 * 72),
    ]


class DiscreteConfig(ctypes.Structure):
    _fields_ = [("sliceSizes", ctypes.c_uint32 * 4), ("grainCount", ctypes.c_uint32), ("reserved", ctypes.c_uint8 * 44)]


class ContinuousConfig(ctypes.Structure):
    _fields_ = [("channelCount", ctypes.c_uint32), ("bufferLength", ctypes.c_uint32), ("reserved", ctypes.c_uint8 * 56)]


class _ConfigUnion(ctypes.Union):
    _fields_ = [("discrete", DiscreteConfig), ("continuous", ContinuousConfig)]


class FlowConfigInfo(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("common", CommonConfig), ("u", _ConfigUnion)]


assert ctypes.sizeof(GrainInfo) == 4096
assert ctypes.sizeof(FlowConfigInfo) == 192
assert ctypes.sizeof(RuntimeInfo) == 64


def load_library(name: str = "libmxl.so"):
    lib = ctypes.CDLL(name)
    lib.mxlCreateInstance.restype = ctypes.c_void_p
    lib.mxlCreateInstance.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    lib.mxlDestroyInstance.argtypes = [ctypes.c_void_p]
    lib.mxlCreateFlowReader.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
    lib.mxlReleaseFlowReader.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.mxlFlowReaderGetRuntimeInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(RuntimeInfo)]
    lib.mxlFlowReaderGetConfigInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(FlowConfigInfo)]
    lib.mxlFlowReaderGetGrain.argtypes = [
        ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint64,
        ctypes.POINTER(GrainInfo), ctypes.POINTER(ctypes.c_void_p),
    ]
    lib.mxlFlowReaderGetGrainNonBlocking.argtypes = [
        ctypes.c_void_p, ctypes.c_uint64, ctypes.POINTER(GrainInfo), ctypes.POINTER(ctypes.c_void_p),
    ]
    lib.mxlFlowReaderGetSamples.argtypes = [
        ctypes.c_void_p, ctypes.c_uint64, ctypes.c_size_t, ctypes.c_uint64,
        ctypes.POINTER(WrappedMultiBufferSlice),
    ]
    for function in (
        lib.mxlDestroyInstance, lib.mxlCreateFlowReader, lib.mxlReleaseFlowReader,
        lib.mxlFlowReaderGetRuntimeInfo, lib.mxlFlowReaderGetConfigInfo, lib.mxlFlowReaderGetGrain,
        lib.mxlFlowReaderGetGrainNonBlocking, lib.mxlFlowReaderGetSamples,
    ):
        function.restype = ctypes.c_int
    return lib


class Reader:
    """One flow reader. ``api`` is the loaded library (or a test double)."""

    def __init__(self, api, instance, flow_id: str) -> None:
        self.api = api
        self.instance = instance
        self.handle = ctypes.c_void_p()
        status = api.mxlCreateFlowReader(instance, flow_id.encode(), b"", ctypes.byref(self.handle))
        if status != MXL_STATUS_OK:
            raise RuntimeError(f"mxlCreateFlowReader({flow_id}) failed with status {status}")

    def close(self) -> None:
        if self.handle:
            self.api.mxlReleaseFlowReader(self.instance, self.handle)
            self.handle = ctypes.c_void_p()

    def config(self) -> FlowConfigInfo:
        info = FlowConfigInfo()
        status = self.api.mxlFlowReaderGetConfigInfo(self.handle, ctypes.byref(info))
        if status != MXL_STATUS_OK:
            raise RuntimeError(f"mxlFlowReaderGetConfigInfo failed with status {status}")
        return info

    def head(self) -> int | None:
        info = RuntimeInfo()
        status = self.api.mxlFlowReaderGetRuntimeInfo(self.handle, ctypes.byref(info))
        return info.headIndex if status == MXL_STATUS_OK else None


@dataclass
class GrainRead:
    status: int
    index: int
    value: object = None       # caller's result, only when accepted
    torn: bool = False         # the slot changed while it was being read


class GrainReader(Reader):
    def __init__(self, api, instance, flow_id: str, grain_count: int) -> None:
        super().__init__(api, instance, flow_id)
        self.grain_count = grain_count

    def read(self, index: int, timeout_ns: int, sample) -> GrainRead:
        """Read grain ``index`` and run ``sample(payload_address, grain_size)`` on it in place.

        The result is accepted only if, after sampling, the same grain still
        occupies the slot *and* the committed head leaves the writer no room to
        have opened that slot for a newer grain (the margin Core uses:
        ``head + 2 > index + grain_count`` means possibly overwritten). The
        second check covers a writer that has opened but not yet committed the
        slot. Otherwise ``torn`` is set and ``value`` is None.
        """

        info = GrainInfo()
        payload = ctypes.c_void_p()
        status = self.api.mxlFlowReaderGetGrain(self.handle, index, timeout_ns, ctypes.byref(info), ctypes.byref(payload))
        if status != MXL_STATUS_OK:
            return GrainRead(status, index)
        if info.index != index or info.flags & MXL_GRAIN_FLAG_INVALID or not payload.value:
            return GrainRead(status, index, torn=info.index != index)
        value = sample(payload.value, info.grainSize)
        check = GrainInfo()
        check_payload = ctypes.c_void_p()
        check_status = self.api.mxlFlowReaderGetGrainNonBlocking(
            self.handle, index, ctypes.byref(check), ctypes.byref(check_payload))
        if check_status != MXL_STATUS_OK or check.index != index or check_payload.value != payload.value:
            return GrainRead(status, index, torn=True)
        head = self.head()
        if head is None or head + 2 > index + self.grain_count:
            return GrainRead(status, index, torn=True)
        return GrainRead(status, index, value)


class SampleReader(Reader):
    def __init__(self, api, instance, flow_id: str, ring_length: int) -> None:
        super().__init__(api, instance, flow_id)
        self.ring_length = ring_length

    def read(self, end_index: int, count: int, timeout_ns: int, channels: int):
        """Return ``(status, [bytes per channel] | None, torn)`` for samples ``[end-count, end)``.

        Planar Float32 bytes are copied out of the ring, then the head is
        re-read; if the writer could have overwritten the range during the copy
        the read is reported torn.
        """

        slices = WrappedMultiBufferSlice()
        status = self.api.mxlFlowReaderGetSamples(self.handle, end_index, count, timeout_ns, ctypes.byref(slices))
        if status != MXL_STATUS_OK:
            return status, None, False
        data = []
        for channel in range(min(channels, slices.count)):
            parts = []
            for fragment in slices.fragments:
                if fragment.size:
                    parts.append(ctypes.string_at(fragment.pointer + channel * slices.stride, fragment.size))
            data.append(b"".join(parts))
        head = self.head()
        torn = head is None or head - (end_index - count) >= self.ring_length
        return status, (None if torn else data), torn


def v210_code_sampler(decode):
    """Adapt a v210 frame-code decoder to sample straight from a payload address."""

    def sample(address: int, size: int):
        return decode(_AddressView(address, size))

    return sample


class _AddressView:
    """Byte-slice view over raw memory, reading only the slices requested."""

    def __init__(self, address: int, size: int) -> None:
        self.address = address
        self.size = size

    def __len__(self) -> int:
        return self.size

    def __getitem__(self, key):
        if isinstance(key, slice):
            start, stop, _ = key.indices(self.size)
            return ctypes.string_at(self.address + start, max(0, stop - start))
        return ctypes.string_at(self.address + key, 1)[0]
