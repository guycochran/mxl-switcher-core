#!/usr/bin/env python3
"""Publish a clock-correct candidate Program Audio flow from an MXL source."""

from __future__ import annotations

import argparse
import json
import pathlib
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from program_clock import AdaptiveResampleController, ClockRateEstimator, telemetry


CAPS = (
    "audio/x-raw,format=F32LE,layout=interleaved,rate=48000,"
    "channels=2,channel-mask=(bitmask)0x3"
)
BYTES_PER_SECOND = 48_000 * 2 * 4
HEAD_INDEX_OFFSET = 0xC8
OUTPUT_STALL_SECONDS = 3.0
STARTUP_GRACE_SECONDS = 8.0


def advance_output_epoch(epoch_ns: int, emitted_bytes: int) -> int:
    return epoch_ns + emitted_bytes * 1_000_000_000 // BYTES_PER_SECOND


def pipeline_description(
    domain: str,
    source_flow: str,
    output_flow: str,
    epoch_ns: int,
    group_hint: str = "MXLProgramMasterCandidate",
) -> str:
    """Return the candidate pipeline without starting or modifying live state."""

    return (
        f"mxlsrc name=source domain={domain} audio-flow-id={source_flow} "
        "automatic-eos=false ! "
        f"{CAPS} ! "
        "queue name=input_queue max-size-time=500000000 "
        "max-size-buffers=0 max-size-bytes=0 ! "
        "pitch name=pll pitch=1.0 tempo=1.0 rate=1.0 ! "
        "audioresample quality=10 ! "
        f"{CAPS} ! "
        f"identity name=retime datarate={BYTES_PER_SECOND} "
        f"single-segment=true ts-offset={epoch_ns} ! "
        "queue name=output_queue max-size-time=500000000 "
        "max-size-buffers=0 max-size-bytes=0 ! "
        f"mxlsink name=output domain={domain} flow-id={output_flow} "
        'label="Program Master Audio Candidate" '
        'description="Clock-correct 48 kHz Program master candidate" '
        f'group-hint="{group_hint}:Audio" sync=true'
    )


def load_gstreamer():
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    return Gst


class ProgramReclock:
    def __init__(
        self,
        domain: str,
        source_flow: str,
        output_flow: str,
        target_buffer_ms: float,
        max_correction_ppm: float,
        group_hint: str,
    ) -> None:
        self.Gst = load_gstreamer()
        self.domain = domain
        self.source_flow = source_flow
        self.output_flow = output_flow
        self.group_hint = group_hint
        self.epoch_ns = time.monotonic_ns()
        self.lock = threading.Lock()
        self.estimator = ClockRateEstimator(
            window_seconds=60,
            minimum_span_seconds=10,
            maximum_rate_error_ppm=10_000_000,
        )
        self.output_estimator = ClockRateEstimator(
            window_seconds=60,
            minimum_span_seconds=10,
            maximum_rate_error_ppm=10_000_000,
        )
        self.controller = AdaptiveResampleController(
            target_buffer_ms=target_buffer_ms,
            max_correction_ppm=max_correction_ppm,
            # GStreamer queue levels are diagnostic here, not the explicit
            # sample FIFO required for trustworthy occupancy feedback.
            buffer_gain_ppm_per_ms=0.0,
            smoothing=0.05,
        )
        self.input_buffers = 0
        self.input_bytes = 0
        self.latest_input_pts = None
        self.last_observed_input_bytes = 0
        self.source_pts_regressions = 0
        self.output_buffers = 0
        self.output_bytes = 0
        self.last_observed_output_bytes = 0
        self.last_output_pts = None
        self.last_output_duration = None
        self.output_pts_discontinuities = 0
        self.output_pts_discontinuity_min_ns = 0
        self.output_pts_discontinuity_max_ns = 0
        self.tempo = 1.0
        self.last_error = None
        self.last_restart_reason = None
        self.pipeline_generation = 0
        self.pipeline_restarts = 0
        self.started_at = time.monotonic()
        self.pipeline_started_at = self.started_at
        self.output_head_index = None
        self.output_progress_at = self.started_at
        self._make_pipeline(self.epoch_ns)

    def _make_pipeline(self, output_epoch_ns: int) -> None:
        self.pipeline = self.Gst.parse_launch(
            pipeline_description(
                self.domain,
                self.source_flow,
                self.output_flow,
                output_epoch_ns,
                self.group_hint,
            )
        )
        self.source = self.pipeline.get_by_name("source")
        self.input_queue = self.pipeline.get_by_name("input_queue")
        self.output_queue = self.pipeline.get_by_name("output_queue")
        self.pll = self.pipeline.get_by_name("pll")
        self.retime = self.pipeline.get_by_name("retime")
        self.output_sink = self.pipeline.get_by_name("output")
        self.bus = self.pipeline.get_bus()
        self.source.get_static_pad("src").add_probe(
            self.Gst.PadProbeType.BUFFER, self._observe_input
        )
        self.output_sink.get_static_pad("sink").add_probe(
            self.Gst.PadProbeType.BUFFER, self._observe_output
        )
        self.pll.set_property("tempo", self.tempo)
        self.pipeline_generation += 1
        self.pipeline_started_at = time.monotonic()

    @property
    def output_data_path(self) -> pathlib.Path:
        return pathlib.Path(self.domain) / f"{self.output_flow}.mxl-flow" / "data"

    def _observe_input(self, _pad, info):
        buffer = info.get_buffer()
        if buffer is not None:
            with self.lock:
                self.input_buffers += 1
                self.input_bytes += buffer.get_size()
                if buffer.pts != self.Gst.CLOCK_TIME_NONE:
                    if (
                        self.latest_input_pts is not None
                        and int(buffer.pts) < self.latest_input_pts
                    ):
                        self.source_pts_regressions += 1
                    self.latest_input_pts = int(buffer.pts)
        return self.Gst.PadProbeReturn.OK

    def _observe_output(self, _pad, info):
        buffer = info.get_buffer()
        if buffer is not None:
            with self.lock:
                self.output_buffers += 1
                self.output_bytes += buffer.get_size()
                if buffer.pts != self.Gst.CLOCK_TIME_NONE:
                    pts = int(buffer.pts)
                    duration = (
                        0
                        if buffer.duration == self.Gst.CLOCK_TIME_NONE
                        else int(buffer.duration)
                    )
                    if duration <= 0:
                        duration = (
                            buffer.get_size() * 1_000_000_000 // BYTES_PER_SECOND
                        )
                    if self.last_output_pts is not None:
                        expected = self.last_output_pts + self.last_output_duration
                        discontinuity = pts - expected
                        if abs(discontinuity) > 1_000:
                            self.output_pts_discontinuities += 1
                            self.output_pts_discontinuity_min_ns = min(
                                self.output_pts_discontinuity_min_ns, discontinuity
                            )
                            self.output_pts_discontinuity_max_ns = max(
                                self.output_pts_discontinuity_max_ns, discontinuity
                            )
                    self.last_output_pts = pts
                    self.last_output_duration = duration
        return self.Gst.PadProbeReturn.OK

    def _read_output_head(self):
        payload = self.output_data_path.read_bytes()
        return struct.unpack_from("<Q", payload, HEAD_INDEX_OFFSET)[0]

    def _buffer_ms(self) -> float:
        queued_ns = self.input_queue.get_property("current-level-time")
        queued_ns += self.output_queue.get_property("current-level-time")
        return queued_ns / 1_000_000.0

    def _restart_pipeline(self, reason: str) -> None:
        old_pipeline = self.pipeline
        old_pipeline.set_state(self.Gst.State.NULL)
        with self.lock:
            output_epoch_ns = advance_output_epoch(self.epoch_ns, self.output_bytes)
            self.estimator.reset()
            self.output_estimator.reset()
            self.latest_input_pts = None
            self.last_observed_input_bytes = self.input_bytes
            self.last_observed_output_bytes = self.output_bytes
            self.output_head_index = None
            self.output_progress_at = time.monotonic()
            self.pipeline_restarts += 1
            self.last_restart_reason = reason
            self._make_pipeline(output_epoch_ns)
        result = self.pipeline.set_state(self.Gst.State.PLAYING)
        if result == self.Gst.StateChangeReturn.FAILURE:
            with self.lock:
                self.last_error = f"pipeline restart failed: {reason}"
            return

    def monitor(self) -> None:
        while True:
            time.sleep(0.25)
            message = self.bus.pop_filtered(
                self.Gst.MessageType.ERROR | self.Gst.MessageType.EOS
            )
            if message is not None:
                if message.type == self.Gst.MessageType.ERROR:
                    error, debug = message.parse_error()
                    reason = f"gstreamer error: {error}; {debug or ''}".strip()
                else:
                    reason = "unexpected end of stream"
                self._restart_pipeline(reason)
                continue

            now = time.monotonic()
            now_ns = time.monotonic_ns()
            try:
                head = self._read_output_head()
            except (OSError, struct.error):
                head = None
            with self.lock:
                if self.input_bytes != self.last_observed_input_bytes:
                    received_ns = self.input_bytes * 1_000_000_000 // BYTES_PER_SECOND
                    self.estimator.observe(received_ns, now_ns)
                    self.last_observed_input_bytes = self.input_bytes
                if self.output_bytes != self.last_observed_output_bytes:
                    emitted_ns = self.output_bytes * 1_000_000_000 // BYTES_PER_SECOND
                    self.output_estimator.observe(emitted_ns, now_ns)
                    self.last_observed_output_bytes = self.output_bytes
                if head is not None and head != self.output_head_index:
                    self.output_head_index = head
                    self.output_progress_at = now
                if self.estimator.ready:
                    ratio = self.controller.update(self.estimator.rate, self._buffer_ms())
                    self.tempo = 1.0 / ratio
                    self.pll.set_property("tempo", self.tempo)
                if (
                    now - self.pipeline_started_at > STARTUP_GRACE_SECONDS
                    and now - self.output_progress_at > OUTPUT_STALL_SECONDS
                ):
                    reason = (
                        f"output ring stalled for {now - self.output_progress_at:.1f}s"
                    )
                else:
                    reason = None
            if reason is not None:
                self._restart_pipeline(reason)

    def state(self) -> dict[str, object]:
        with self.lock:
            buffer_ms = self._buffer_ms()
            result = telemetry(self.estimator, self.controller, buffer_ms, 0)
            result.update(
                {
                    "state": "error" if self.last_error else "ok",
                    "source_flow": self.source_flow,
                    "output_flow": self.output_flow,
                    "group_hint": self.group_hint,
                    "epoch_ns": self.epoch_ns,
                    "tempo": self.tempo,
                    "pipeline_generation": self.pipeline_generation,
                    "pipeline_restarts": self.pipeline_restarts,
                    "last_restart_reason": self.last_restart_reason,
                    "input_buffers": self.input_buffers,
                    "input_bytes": self.input_bytes,
                    "latest_input_pts": self.latest_input_pts,
                    "source_pts_regressions": self.source_pts_regressions,
                    "output_buffers": self.output_buffers,
                    "output_bytes": self.output_bytes,
                    "output_rate": self.output_estimator.rate,
                    "output_drift_ppm": self.output_estimator.drift_ppm,
                    "output_pts_discontinuities": self.output_pts_discontinuities,
                    "output_pts_discontinuity_ns_range": [
                        self.output_pts_discontinuity_min_ns,
                        self.output_pts_discontinuity_max_ns,
                    ],
                    "output_head_index": self.output_head_index,
                    "output_progress_age_seconds": round(
                        time.monotonic() - self.output_progress_at, 3
                    ),
                    "hard_sample_corrections": 0,
                    "buffer_feedback_enabled": False,
                    "last_error": self.last_error,
                }
            )
            return result

    def start(self) -> None:
        result = self.pipeline.set_state(self.Gst.State.PLAYING)
        if result == self.Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Program reclock pipeline failed to start")
        threading.Thread(target=self.monitor, daemon=True).start()


BRIDGE: ProgramReclock


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path != "/state":
            self.send_error(404)
            return
        payload = json.dumps(BRIDGE.state(), separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format, *_args):
        return


def main() -> None:
    global BRIDGE
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", default="/mxl-domain")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--target-buffer-ms", type=float, default=120.0)
    parser.add_argument("--max-correction-ppm", type=float, default=2500.0)
    parser.add_argument("--group-hint", default="MXLProgramMasterCandidate")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9750)
    args = parser.parse_args()
    BRIDGE = ProgramReclock(
        args.domain,
        args.source,
        args.output,
        args.target_buffer_ms,
        args.max_correction_ppm,
        args.group_hint,
    )
    BRIDGE.start()
    print(
        json.dumps(
            {
                "event": "program_reclock_ready",
                "source": args.source,
                "output": args.output,
                "port": args.port,
            }
        ),
        flush=True,
    )
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
