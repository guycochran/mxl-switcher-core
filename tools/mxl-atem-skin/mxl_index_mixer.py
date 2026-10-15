#!/usr/bin/env python3
"""Index-domain Program Audio mixer: mix MXL audio by absolute sample index.

The GStreamer ``audiomixer`` used by ``mxl_audio_mixer.py`` is clock-driven:
an input that has not arrived within its latency budget is mixed as silence
(see docs/AV_REFERENCE.md, Gate C). This mixer has no media clock of its own.
For each 480-sample output block ending at index ``end`` it reads every active
input at ``end`` through the MXL SDK, waits for late input up to a bounded
budget measured in MXL time, sums with per-input gain, and writes the result at
the same ``end``. Output sample ``s`` is therefore the gain-weighted sum of
input samples ``s``, whatever the arrival jitter, and the wait only delays when
the block is written, never where.

Input that cannot be had is never mixed in as uncounted silence. Each block an
active input contributes is classified per input as ``on_time``, ``late``
(arrived during the wait; the output was held for it, and ``held_ms`` records
how long), or ``skipped`` (with a reason: ``missing`` after the full budget,
``overrun`` when the ring has already moved past the index, ``torn`` when the
writer lapped the range during the copy, ``invalid`` when the writer
recreated the flow, ``offline`` when there is no open reader, or ``error``).
A skipped input contributes nothing to that block and the block is counted as
degraded. An input that runs out of budget is marked stalled and is polled
without waiting until it catches up, so a dead source cannot hold Program Audio
back.

Inputs come and go: ZoomISO recreates its flows on restart. An input whose flow
is absent (or not 48 kHz) at startup is **offline**, not fatal; the lab SDK
crashes on ``mxlCreateFlowReader`` for a missing flow, so a flow is opened only
after ``mxl_direct.flow_exists``. Offline inputs, and stalled inputs whose flow
has disappeared, are re-probed once per reopen interval of MXL time. A read
that reports ``MXL_ERR_FLOW_INVALID`` closes the reader and reopens it at once.

The control API (``GET /state``, ``POST /control`` with ``mode``,
``master_gain``, ``channel``/``gain``/``on``/``mute``) and AFV polling of Core's
``/api/mxl/v1/state`` match ``mxl_audio_mixer.py``. Standard library only; the
MXL SDK shared library is loaded at runtime.
"""

from __future__ import annotations

import argparse
import json
import operator
import sys
import threading
import time
import urllib.request
import uuid
from array import array
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import av_reference as ref
import mxl_direct as mxl

SAMPLE_RATE = 48_000
BLOCK = 480
DEFAULT_MAX_WAIT_MS = 500.0
DEFAULT_REOPEN_INTERVAL_MS = 1000.0
GAIN_LIMIT = 1.5
TAI = getattr(time, "CLOCK_TAI", time.CLOCK_REALTIME)
GROUPHINT_TAG = "urn:x-nmos:tag:grouphint/v1.0"


def mxl_now_index() -> int:
    return ref.ns_to_index(time.clock_gettime_ns(TAI), SAMPLE_RATE)


def flow_definition(flow_id: str, group_hint: str, label: str, description: str) -> str:
    """NMOS audio flow definition for a stable 48 kHz Float32 stereo output."""

    def derived(role: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"mxl-index-mixer:{role}:{flow_id}"))

    return json.dumps({
        "id": flow_id,
        "source_id": derived("source"),
        "device_id": derived("device"),
        "format": "urn:x-nmos:format:audio",
        "media_type": "audio/float32",
        "label": label,
        "description": description,
        "tags": {GROUPHINT_TAG: [f"{group_hint}:Audio"]},
        "sample_rate": {"numerator": SAMPLE_RATE},
        "channel_count": 2,
        "bit_depth": 32,
        "parents": [],
    })


def mix_planar(contributions, count: int) -> list[bytes]:
    """Sum ``(channels, gain_from, gain_to)`` contributions into stereo planar Float32.

    ``channels`` is a list of per-channel Float32 bytes; mono is used for both
    sides. A gain change ramps linearly across the block, reaching the new gain
    on the last sample, so fader moves and AFV cuts do not click. A single
    input at unity is passed through bit-exact.
    """

    if not contributions:
        silent = bytes(4 * count)
        return [silent, silent]
    if len(contributions) == 1:
        channels, gain_from, gain_to = contributions[0]
        if gain_from == gain_to == 1.0:
            return [channels[0], channels[min(1, len(channels) - 1)]]
    out = [None, None]
    for channels, gain_from, gain_to in contributions:
        if gain_from == gain_to:
            gains = None
        else:
            step = (gain_to - gain_from) / count
            gains = [gain_from + step * (i + 1) for i in range(count)]
        for side in range(2):
            samples = array("f", channels[min(side, len(channels) - 1)])
            if gains is None:
                scaled = [gain_to * value for value in samples]
            else:
                scaled = list(map(operator.mul, gains, samples))
            out[side] = scaled if out[side] is None else list(map(operator.add, out[side], scaled))
    return [array("f", side).tobytes() for side in out]


@dataclass
class InputStats:
    on_time: int = 0
    late: int = 0
    skipped: int = 0
    skipped_reasons: dict = field(default_factory=lambda: {
        "missing": 0, "overrun": 0, "torn": 0, "invalid": 0, "offline": 0, "error": 0})
    held_ms: float = 0.0
    max_held_ms: float = 0.0
    stalled: bool = False
    recoveries: int = 0
    last_status: int | None = None
    flow_id: str | None = None
    online: bool = True
    opens: int = 0
    closes: int = 0
    last_open_error: str | None = None

    def skip(self, reason: str) -> None:
        self.skipped += 1
        self.skipped_reasons[reason] += 1


class InputUnavailable(RuntimeError):
    """The input flow cannot be opened now (absent, or not a 48 kHz audio flow)."""


class DomainInputs:
    """Open MXL audio inputs safely: never ask the SDK for a flow that is not there."""

    def __init__(self, api, instance, domain: str) -> None:
        self.api, self.instance, self.domain = api, instance, domain

    def exists(self, flow_id: str) -> bool:
        return mxl.flow_exists(self.domain, flow_id)

    def open(self, flow_id: str) -> mxl.SampleReader:
        if not self.exists(flow_id):
            raise InputUnavailable(f"flow {flow_id} is not in the domain")
        return open_input(self.api, self.instance, flow_id)


class IndexMixer:
    """Mix ``readers`` (``{"1": SampleReader | None, ...}``) into ``writer`` by sample index.

    ``clock`` returns the current MXL sample index; it is the only time base,
    so tests drive the mixer with a fake clock and fake rings. ``None`` is an
    offline input. With ``inputs`` (a ``DomainInputs``-like object) and
    ``flows`` (``{"1": flow_id, ...}``), offline and invalidated inputs are
    reopened; without them, a reader is used as given for the mixer's life.
    """

    def __init__(self, readers: dict, writer, output_flow: str = "", clock=mxl_now_index,
                 max_wait_ms: float = DEFAULT_MAX_WAIT_MS, sleep=time.sleep, inputs=None,
                 flows: dict | None = None, reopen_interval_ms: float = DEFAULT_REOPEN_INTERVAL_MS) -> None:
        self.readers = dict(readers)
        self.writer = writer
        self.output_flow = output_flow
        self.clock = clock
        self.sleep = sleep
        self.inputs = inputs
        self.flows = dict(flows or {})
        self.reopen_interval_samples = max(BLOCK, round(reopen_interval_ms * SAMPLE_RATE / 1000))
        self.next_probe = {number: None for number in self.readers}
        self.wait_budget_samples = round(max_wait_ms * SAMPLE_RATE / 1000)
        self._update_wait_cap()
        self.lock = threading.Lock()
        self.mode = "afv"
        self.program = None
        self.master_gain = 1.0
        self.channels = {number: {"gain": 1.0, "on": False, "mute": False} for number in readers}
        self.applied = {number: 0.0 for number in readers}
        self.stats = {number: InputStats(flow_id=self.flows.get(number), online=readers[number] is not None)
                      for number in readers}
        self.blocks_written = 0
        self.blocks_degraded = 0
        self.write_errors = 0
        self.last_write_status = None
        self.output_end_index = None
        self.output_lag_samples = None
        self.output_progress_at = time.monotonic()
        self._retire_lock = threading.Lock()
        self._retired_readers: list = []
        self.reader_release_errors = 0

    # --- control (same contract as mxl_audio_mixer.py) ---------------------
    def _enabled(self, number: str) -> bool:
        channel = self.channels[number]
        if channel["mute"]:
            return False
        if self.mode == "afv" and self.program and self.program.startswith("zoomiso-"):
            return number == self.program.rsplit("-", 1)[-1]
        return bool(channel["on"])

    def _target_gain(self, number: str) -> float:
        return self.channels[number]["gain"] * self.master_gain if self._enabled(number) else 0.0

    def set_program(self, program) -> None:
        with self.lock:
            self.program = program

    def control(self, command: dict) -> dict:
        with self.lock:
            if "mode" in command:
                if command["mode"] not in ("afv", "manual"):
                    raise ValueError("mode must be afv or manual")
                self.mode = command["mode"]
            if "master_gain" in command:
                self.master_gain = max(0.0, min(GAIN_LIMIT, float(command["master_gain"])))
            if "channel" in command:
                number = str(int(command["channel"]))
                if number not in self.channels:
                    raise ValueError(f"channel must be 1 through {len(self.channels)}")
                channel = self.channels[number]
                if "gain" in command:
                    channel["gain"] = max(0.0, min(GAIN_LIMIT, float(command["gain"])))
                if "on" in command:
                    channel["on"] = bool(command["on"])
                if "mute" in command:
                    channel["mute"] = bool(command["mute"])
            return self._state_unlocked()

    def _state_unlocked(self) -> dict:
        return {
            "state": "ok",
            "engine": "index",
            "mode": self.mode,
            "program": self.program,
            "master_gain": self.master_gain,
            "output_flow": self.output_flow,
            "block_samples": BLOCK,
            "max_wait_ms": self.max_wait_samples * 1000 / SAMPLE_RATE,
            "reopen_interval_ms": self.reopen_interval_samples * 1000 / SAMPLE_RATE,
            "output_head_index": self.output_end_index,
            "output_lag_ms": None if self.output_lag_samples is None else self.output_lag_samples * 1000 / SAMPLE_RATE,
            "output_progress_age_seconds": round(time.monotonic() - self.output_progress_at, 3),
            "blocks_written": self.blocks_written,
            "blocks_degraded": self.blocks_degraded,
            "write_errors": self.write_errors,
            "last_write_status": self.last_write_status,
            "reader_releases_pending": len(self._retired_readers),
            "reader_release_errors": self.reader_release_errors,
            "channels": {
                number: {**channel, "active": self._enabled(number), **asdict(self.stats[number])}
                for number, channel in self.channels.items()
            },
        }

    def state(self) -> dict:
        with self.lock:
            return self._state_unlocked()

    # --- input lifecycle -----------------------------------------------------
    def _update_wait_cap(self) -> None:
        # Waiting longer than half the shortest open input ring would let
        # another input's data at the same index be overwritten while we wait.
        shortest = min((reader.ring_length for reader in self.readers.values() if reader is not None), default=0)
        budget = self.wait_budget_samples
        self.max_wait_samples = min(budget, shortest // 2) if shortest else budget

    def _close_input(self, number: str) -> None:
        reader = self.readers[number]
        if reader is not None:
            # Detach first. Releasing a dead SDK reader can take longer than
            # the audio history window on a busy host; doing that release on
            # the mix thread makes healthy inputs overrun while an unrelated
            # flow is being recreated.
            self.readers[number] = None
            stats = self.stats[number]
            stats.online = False
            stats.closes += 1
            self._update_wait_cap()
            self._retire_input(reader)

    def _retire_input(self, reader) -> None:
        # The SDK serializes reader teardown with live reader operations even
        # when release is called from another Python thread. Keep the invalid
        # handle until orderly shutdown rather than letting unrelated Program
        # inputs miss their short real-time history window.
        with self._retire_lock:
            self._retired_readers.append(reader)

    def wait_for_retired(self, timeout: float | None = None) -> bool:
        """Wait for detached reader releases during tests or orderly shutdown."""

        deadline = None if timeout is None else time.monotonic() + timeout
        with self._retire_lock:
            readers, self._retired_readers = self._retired_readers, []
        for index, reader in enumerate(readers):
            if deadline is not None and time.monotonic() > deadline:
                with self._retire_lock:
                    self._retired_readers.extend(readers[index:])
                return False
            try:
                reader.close()
            except Exception:
                with self._retire_lock:
                    self.reader_release_errors += 1
        return True

    def _open_input(self, number: str, stalled: bool = False) -> bool:
        """Try to open an offline input now; on failure, wait one reopen interval.

        ``stalled`` carries a stall across a reopen, so a still-silent input is
        not waited for again.
        """

        stats = self.stats[number]
        self.next_probe[number] = self.clock() + self.reopen_interval_samples
        flow_id = self.flows.get(number)
        if self.inputs is None or flow_id is None:
            return False
        try:
            reader = self.inputs.open(flow_id)
        except (InputUnavailable, RuntimeError) as error:
            stats.last_open_error = str(error)
            return False
        self.readers[number] = reader
        stats.online, stats.stalled, stats.last_open_error = True, stalled, None
        stats.opens += 1
        self._update_wait_cap()
        return True

    def _probe_due(self, number: str) -> bool:
        due = self.next_probe[number]
        return due is None or self.clock() >= due

    def maintain_inputs(self) -> None:
        """Once per reopen interval, open offline inputs and reopen stalled ones.

        A stalled reader may hold a flow its writer has since recreated under
        the same ID; the old mapping then never advances. Reopening finds the
        new flow, or marks the input offline if the flow is gone.
        """

        if self.inputs is None:
            return
        for number, reader in self.readers.items():
            if not self._probe_due(number):
                continue
            if reader is None:
                self._open_input(number)
            elif self.stats[number].stalled and number in self.flows:
                self._close_input(number)
                self._open_input(number, stalled=True)

    # --- media ---------------------------------------------------------------
    def _read_input(self, number: str, end: int):
        reader = self.readers[number]
        stats = self.stats[number]
        if reader is None:
            stats.skip("offline")
            return None
        status, channels, torn = reader.read(end, BLOCK, 0, 2)
        waited = 0
        if status in (mxl.MXL_ERR_OUT_OF_RANGE_TOO_EARLY, mxl.MXL_ERR_TIMEOUT) and not stats.stalled:
            started = self.clock()
            remaining = end + self.max_wait_samples - started
            if remaining > 0:
                status, channels, torn = reader.read(end, BLOCK, remaining * 1_000_000_000 // SAMPLE_RATE, 2)
            waited = max(0, self.clock() - started)
        held_ms = waited * 1000 / SAMPLE_RATE
        stats.held_ms += held_ms
        stats.max_held_ms = max(stats.max_held_ms, held_ms)
        stats.last_status = status
        if status == mxl.MXL_STATUS_OK and not torn and channels:
            if stats.stalled:
                stats.stalled = False
                stats.recoveries += 1
            if waited:
                stats.late += 1
            else:
                stats.on_time += 1
            return channels
        if status == mxl.MXL_STATUS_OK:
            stats.skip("torn")
        elif status == mxl.MXL_ERR_OUT_OF_RANGE_TOO_LATE:
            stats.skip("overrun")
        elif status in (mxl.MXL_ERR_OUT_OF_RANGE_TOO_EARLY, mxl.MXL_ERR_TIMEOUT):
            stats.skip("missing")
            stats.stalled = True
        elif status == mxl.MXL_ERR_FLOW_INVALID:
            # The writer recreated the flow: this reader will never see it again.
            stats.skip("invalid")
            if self.inputs is not None:
                self._close_input(number)
                self._open_input(number)
        else:
            stats.skip("error")
        return None

    def mix_block(self, end: int) -> int:
        """Mix and write the block of samples ending at ``end``; return the write status."""

        self.maintain_inputs()
        with self.lock:
            targets = {number: self._target_gain(number) for number in self.readers}
        contributions = []
        degraded = False
        for number in self.readers:
            gain_from, gain_to = self.applied[number], targets[number]
            self.applied[number] = gain_to
            if gain_from == 0.0 and gain_to == 0.0:
                continue
            channels = self._read_input(number, end)
            if channels is None:
                degraded = True
            else:
                contributions.append((channels, gain_from, gain_to))
        status = self.writer.write(end, mix_planar(contributions, BLOCK))
        with self.lock:
            self.last_write_status = status
            self.blocks_degraded += degraded
            if status == mxl.MXL_STATUS_OK:
                self.blocks_written += 1
                self.output_end_index = end
                self.output_progress_at = time.monotonic()
            else:
                self.write_errors += 1
            self.output_lag_samples = self.clock() - end
        return status

    def run(self, stop: threading.Event, first_end: int | None = None) -> None:
        """Write consecutive blocks, never ahead of MXL time; catch up without waiting when behind."""

        end = first_end if first_end is not None else self.clock() // BLOCK * BLOCK
        while not stop.is_set():
            ahead = end - self.clock()
            if ahead > 0:
                self.sleep(min(ahead / SAMPLE_RATE, 0.05))
                continue
            self.mix_block(end)
            end += BLOCK

    def poll_program(self, core: str, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                with urllib.request.urlopen(core.rstrip("/") + "/api/mxl/v1/state", timeout=2) as response:
                    self.set_program(json.load(response).get("program"))
            except (OSError, TimeoutError, json.JSONDecodeError):
                pass
            stop.wait(0.25)


MIXER: IndexMixer


class Handler(BaseHTTPRequestHandler):
    def _json(self, status, value):
        payload = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        self._json(200, MIXER.state()) if self.path == "/state" else self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/control":
            return self._json(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            command = json.loads(self.rfile.read(length) or b"{}")
            self._json(200, MIXER.control(command))
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self._json(400, {"error": str(error)})

    def log_message(self, _format, *_args):
        return


def open_input(api, instance, flow_id: str) -> mxl.SampleReader:
    """Open a 48 kHz audio reader. Call only for a flow known to exist (see ``DomainInputs``)."""

    probe = mxl.Reader(api, instance, flow_id)
    try:
        config = probe.config()
    finally:
        probe.close()
    rate = config.common.grainRate
    if (rate.numerator, rate.denominator) != (SAMPLE_RATE, 1) or config.continuous.channelCount < 1:
        raise InputUnavailable(f"input {flow_id} is not a 48 kHz audio flow; index mixing needs one sample rate")
    return mxl.SampleReader(api, instance, flow_id, config.continuous.bufferLength)


def main() -> int:
    global MIXER
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--domain", default="/mxl-domain")
    parser.add_argument("--output", required=True)
    parser.add_argument("--input", action="append", required=True)
    parser.add_argument("--core", default="http://host.docker.internal:18085")
    parser.add_argument("--group-hint", default="ZoomISOMXLProgram")
    parser.add_argument("--label", default="Program Audio Mix")
    parser.add_argument("--max-wait-ms", type=float, default=DEFAULT_MAX_WAIT_MS)
    parser.add_argument("--reopen-interval-ms", type=float, default=DEFAULT_REOPEN_INTERVAL_MS)
    parser.add_argument("--library", default="libmxl.so")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9740)
    args = parser.parse_args()

    api = mxl.load_library(args.library)
    instance = api.mxlCreateInstance(args.domain.encode(), b"")
    if not instance:
        raise RuntimeError("mxlCreateInstance failed")
    inputs = DomainInputs(api, instance, args.domain)
    flows = {str(number): flow for number, flow in enumerate(args.input, 1)}
    readers = {}
    for number, flow in flows.items():
        try:
            readers[number] = inputs.open(flow)
        except (InputUnavailable, RuntimeError) as error:
            readers[number] = None
            print(f"index_mixer=input_offline channel={number} reason={error}", file=sys.stderr, flush=True)
    writer = mxl.SampleWriter(api, instance, flow_definition(
        args.output, args.group_hint, args.label, "Index-domain AFV Program Audio Master"))
    rate = writer.info.common.grainRate
    if (rate.numerator, rate.denominator) != (SAMPLE_RATE, 1) or writer.info.continuous.channelCount != 2:
        writer.close()
        raise RuntimeError("output flow exists with a different format; expected 48 kHz stereo")
    MIXER = IndexMixer(readers, writer, args.output, max_wait_ms=args.max_wait_ms, inputs=inputs,
                       flows=flows, reopen_interval_ms=args.reopen_interval_ms)
    stop = threading.Event()
    threading.Thread(target=MIXER.poll_program, args=(args.core, stop), daemon=True).start()
    mix_thread = threading.Thread(target=MIXER.run, args=(stop,), daemon=True)
    mix_thread.start()
    online = sum(reader is not None for reader in readers.values())
    print(f"index_mixer=ready inputs={len(readers)} online={online} created={writer.created} "
          f"max_wait_ms={MIXER.max_wait_samples * 1000 / SAMPLE_RATE:.1f}", file=sys.stderr, flush=True)
    try:
        ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    finally:
        stop.set()
        mix_thread.join(2.0)
        for reader in MIXER.readers.values():
            if reader is not None:
                reader.close()
        MIXER.wait_for_retired()
        writer.close()
        api.mxlDestroyInstance(instance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
