#!/usr/bin/env python3
"""Payload resampler seam and fail-closed backend preflight for meeting return.

**Offline foundation. Nothing here is an accepted Zoom meeting return.**

``meeting_return_clock.PairedScheduler`` in ratio mode emits
``("resampled", start, source_n, output_n)`` segments and processes no
audio. This module is the seam that does: ``PayloadResampler`` takes the
scheduler's float32, 48 kHz, two-channel source frames per segment and
returns **exactly** ``output_n`` frames, using a pluggable streaming backend
with a time-varying ratio. It keeps a bounded output FIFO, so the backend's
own delay and variable output counts never become gaps or duplicates, and it
fails closed on underflow or overflow.

Backends implement ``reset()``, ``process(left, right, ratio)`` returning
``(left_out, right_out)``, and ``latency_frames`` (an estimate). ``ratio`` is
output samples per input sample, the libsamplerate convention, which is the
inverse of the scheduler's source:output ratio.

``select_backend`` is the preflight. It returns a usable **qualified**
backend or a machine-readable refusal; it never silently substitutes a weaker
algorithm. Backends here:

- ``samplerate-sinc-best``: libsamplerate through ``python-samplerate``
  (BSD-2-Clause for libsamplerate). This is the production candidate. It
  streams with a per-call ratio and smooths the change across each call. It
  is **unavailable in this offline environment**: the preflight probes for it
  and refuses when it is absent, and it is not claimed as qualified until the
  gate passes on the target runtime.
- ``oracle``, ``linear``, ``drop-hold``: test instruments only, never
  qualified. The gate must pass ``oracle``, which evaluates the known
  reference analytically, and must fail the other two.
"""

from __future__ import annotations

import collections
import dataclasses
import math

SAMPLE_RATE = 48_000
CHANNELS = 2


class ResamplerUnavailable(RuntimeError):
    """No qualified backend can be used; the adapter refuses to start."""


@dataclasses.dataclass(frozen=True)
class BackendInfo:
    name: str
    qualified_candidate: bool  # may be qualified by the gate for production use
    license: str
    notes: str


class SamplerateBackend:
    """libsamplerate (``sinc_best``) through ``python-samplerate``: the production candidate."""

    info = BackendInfo("samplerate-sinc-best", True, "libsamplerate BSD-2-Clause; python-samplerate MIT",
                       "streaming Resampler.process(data, ratio); ratio changes are smoothed within a call")

    def __init__(self) -> None:
        import numpy  # noqa: F401  (python-samplerate requires numpy arrays)
        import samplerate

        self._np = numpy
        self._samplerate = samplerate
        self.reset()

    @property
    def latency_frames(self) -> int:
        return 0  # reported by measurement in the gate, not assumed

    def reset(self) -> None:
        self._resampler = self._samplerate.Resampler("sinc_best", channels=CHANNELS)

    def process(self, left, right, ratio):
        np = self._np
        frames = np.column_stack([np.asarray(left, dtype=np.float32), np.asarray(right, dtype=np.float32)])
        out = self._resampler.process(frames, ratio, end_of_input=False)
        return list(map(float, out[:, 0])), list(map(float, out[:, 1]))


class LinearBackend:
    """Two-point linear interpolation. A **negative control** the gate must fail."""

    info = BackendInfo("linear", False, "n/a (test instrument)", "known aliasing and high-frequency loss")
    latency_frames = 1

    def reset(self) -> None:
        self.position = 0.0  # fractional read position in the buffered input
        self.buffer = [[], []]

    def __init__(self) -> None:
        self.reset()

    def process(self, left, right, ratio):
        self.buffer[0].extend(left)
        self.buffer[1].extend(right)
        step = 1.0 / ratio
        out = [[], []]
        while self.position + 1 < len(self.buffer[0]):
            i = int(self.position)
            frac = self.position - i
            for c in range(CHANNELS):
                a, b = self.buffer[c][i], self.buffer[c][i + 1]
                out[c].append(a + (b - a) * frac)
            self.position += step
        drop = max(0, int(self.position) - 1)
        if drop:
            for c in range(CHANNELS):
                del self.buffer[c][:drop]
            self.position -= drop
        return out[0], out[1]


class DropHoldBackend:
    """Whole-sample drop/hold, as in the scheduler's sample policy. A **negative control**."""

    info = BackendInfo("drop-hold", False, "n/a (test instrument)", "inserts or removes whole samples")
    latency_frames = 0

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.credit = 0.0

    def process(self, left, right, ratio):
        self.credit += len(left) * ratio
        target = int(self.credit)
        self.credit -= target
        out = [list(left), list(right)]
        for c in range(CHANNELS):
            while len(out[c]) < target:
                out[c].append(out[c][-1] if out[c] else 0.0)
            del out[c][target:]
        return out[0], out[1]


class OracleBackend:
    """Evaluates the known reference exactly at the ideal times. **Test-only**, validates the gate."""

    info = BackendInfo("oracle", False, "n/a (test instrument)", "requires the analytic reference; not a DSP")
    latency_frames = 0

    def __init__(self, reference=None) -> None:
        self.reference = reference
        self.reset()

    def reset(self) -> None:
        self.source_time = 0.0  # in source samples
        self.carry = 0.0

    def process(self, left, right, ratio):
        n_in = len(left)
        self.carry += n_in * ratio
        n_out = int(self.carry + 1e-9)  # ratio is out/in of whole frames: never lose one to float error
        self.carry -= n_out
        out = [[], []]
        step = n_in / n_out if n_out else 0.0
        for k in range(n_out):
            t = self.source_time + k * step
            l_value, r_value = self.reference(t)
            out[0].append(l_value)
            out[1].append(r_value)
        self.source_time += n_in
        return out[0], out[1]


BACKENDS = {"samplerate-sinc-best": SamplerateBackend, "linear": LinearBackend, "drop-hold": DropHoldBackend}


def select_backend(name: str = "samplerate-sinc-best", allow_unqualified: bool = False):
    """Preflight. Returns ``(backend, report)``, or raises ``ResamplerUnavailable`` with the reasons."""

    factory = BACKENDS.get(name)
    if factory is None:
        raise ResamplerUnavailable(f"unknown backend {name!r}; known: {', '.join(sorted(BACKENDS))}")
    info = factory.info
    if not info.qualified_candidate and not allow_unqualified:
        raise ResamplerUnavailable(f"{name} is a test instrument, never a production resampler")
    try:
        backend = factory()
    except ImportError as error:
        raise ResamplerUnavailable(f"{name} is not installed in this runtime ({error.name or error}); "
                                   "refusing rather than substituting a weaker algorithm") from None
    return backend, {"backend": info.name, "license": info.license, "qualified_candidate": info.qualified_candidate,
                     "notes": info.notes}


class PayloadResampler:
    """Serve exactly ``output_n`` frames per scheduler segment from a streaming backend.

    A bounded FIFO absorbs the backend's delay and its call-to-call output
    variation. ``prime_frames`` of silence are produced first, so the backend's
    group delay is a fixed, reported latency rather than a start-up underflow.
    Underflow or overflow past the bounds latches ``failed``.
    """

    def __init__(self, backend, prime_frames: int = 256, max_fifo_frames: int = 4_800) -> None:
        self.backend = backend
        self.prime = prime_frames
        self.max_fifo = max_fifo_frames
        self.fifo = [collections.deque(), collections.deque()]
        self.failed: str | None = None
        self.counters = {"segments": 0, "source_frames": 0, "output_frames": 0, "underflow_frames": 0,
                         "max_fifo_frames": 0}
        for c in range(CHANNELS):
            self.fifo[c].extend([0.0] * prime_frames)

    def process(self, left, right, source_n: int, output_n: int):
        """Resample ``source_n`` frames (``left``/``right``) into exactly ``output_n`` frames."""

        if self.failed:
            raise ResamplerUnavailable(f"resampler failed closed: {self.failed}")
        if len(left) != source_n or len(right) != source_n:
            raise ValueError("left/right must each hold source_n frames")
        self.counters["segments"] += 1
        self.counters["source_frames"] += source_n
        if source_n:
            out_l, out_r = self.backend.process(left, right, output_n / source_n)
            self.fifo[0].extend(out_l)
            self.fifo[1].extend(out_r)
        depth = len(self.fifo[0])
        self.counters["max_fifo_frames"] = max(self.counters["max_fifo_frames"], depth)
        if depth > self.max_fifo:
            self.failed = "fifo_overflow"
            raise ResamplerUnavailable("resampler failed closed: fifo_overflow")
        if depth < output_n:
            self.counters["underflow_frames"] += output_n - depth
            self.failed = "fifo_underflow"
            raise ResamplerUnavailable("resampler failed closed: fifo_underflow")
        out = ([self.fifo[0].popleft() for _ in range(output_n)], [self.fifo[1].popleft() for _ in range(output_n)])
        self.counters["output_frames"] += output_n
        return out

    def status(self) -> dict:
        return {"failed": self.failed, "fifo_frames": len(self.fifo[0]),
                "fifo_latency_ms": round(len(self.fifo[0]) * 1000 / SAMPLE_RATE, 3),
                "prime_frames": self.prime, "counters": dict(self.counters)}
