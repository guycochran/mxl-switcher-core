# Paired-clock meeting-return adapter (offline foundation)

> **Status: offline foundation only. This is not an accepted Zoom meeting
> return.** Nothing here has run against Zoom, ZoomISO, or a live meeting.
> The only accepted gate for meeting return remains the independent
> audience-side A/V reference capture (`docs/AV_REFERENCE.md`).

## Why

The audience-side gate localized the remaining Program-return failure:

- the Core and index-mixer pair is exact (0 ms offset, 0 ppm drift)
  immediately before the meeting adapter;
- at the audience it shows growing audio lag (+5,000 to +9,400 ppm) and
  reduced, repeat-heavy video cadence;
- CPU and memory are not saturated.

The virtual camera and the virtual microphone pull media through independent
callback clocks. An audio-only PLL stays internally healthy but cannot pair
the two. The adapter must own **one output clock for both**.

## Contract

`tools/mxl-atem-skin/meeting_return_clock.py` (`PairedScheduler`) is pure and
deterministic. It uses integer nanoseconds and sample indices, and has no
Zoom or MXL dependency.

| Item | Contract |
| --- | --- |
| Input | The accepted Program Master pair on one media timeline: frame `k` at `k/30` s, sample `s` at `s/48000` s. `push_video(k)` and `push_audio(start, count)`. |
| Queues | Bounded. Video holds `video_queue_frames` (8) and audio holds `audio_queue_samples` (500 ms). Overflow drops the oldest data and counts `video_overruns` or `audio_overrun_samples`. Duplicate or regressed frames are ignored. Input gaps and overlaps are counted. |
| Output clock | One monotonic epoch `(t0, m0)` and target media `m(t) = m0 + (t − t0)`. The clock starts only once both queues hold `target_latency_ms` (80) of media. Until then, output is explicit pre-start silence or no frame, and `output_allowed` is false. |
| Video policy | At each camera callback, present the newest frame with media ≤ `m(t)`. Older unpresented frames are `video_drops`. If no new frame is due, the last frame is repeated (`video_repeats`). A queue with nothing to show is `video_underruns`. |
| Audio policy | At each microphone callback, error = read pointer − `m(t)`. Outside `audio_deadband_ms` (5), corrections are drawn from a budget that accrues at `max_correction_ppm` (2%) of samples delivered: samples are dropped when audio is behind (`audio_dropped`) or the last sample is repeated when it is ahead (`audio_inserted`). Missing data becomes silence (`audio_underrun_samples`). |
| Discontinuity and re-anchor | An error beyond `discontinuity_ms` (100) re-anchors the read pointer to `m(t)`, counting `discontinuities` and `reanchors`. More than `max_reanchors` (5) within `reanchor_window_s` (60) fails. |
| A/V skew | Audio error minus video error, both measured against the shared clock: `av_skew.current/min/max_ms`. |
| Telemetry | For each stream: callback interval (last, min, max, mean, p99, over a bounded window). Queue depth and maximum depth. All counters. |

### Fail-closed health

| `health` | `output_allowed` | When |
| --- | --- | --- |
| `starting` | false | The clock has not started; the latency reservoir is still filling. |
| `ok` | true | Skew is within `skew_soft_ms` (50) and corrections are not saturated. |
| `degraded` | true | Skew exceeds the soft bound, or corrections were saturated in at least half of the last `saturation_callbacks` (300). |
| `failed` (latched) | false | Skew beyond `skew_hard_ms` (100) for `hard_grace_callbacks` (50) → `sustained_av_skew`; or corrections saturated in ≥ 90% of the window while losing ground (the newer half's mean residual exceeds the older half's by more than the deadband), or a re-anchor while saturated and not converging → `uncorrectable_rate_mismatch`; or the re-anchor budget is exceeded → `reanchor_budget_exceeded`. |

A future integration must stop sending, or raise an EIC alarm, whenever
`output_allowed` is false. A latched failure clears only on restart.

### Boundary

The input side consumes MXL Program Master. Video frames are indexed, and
audio arrives as index segments. The output side is two callbacks:

- `on_video_callback(t)` returns the frame to present;
- `on_audio_callback(t, n)` returns segments: `("source", start, count)`,
  `("hold", count)` (repeat the last sample), or `("silence", count)`.

Every time `t` is host monotonic time. A ZoomISO virtual-device integration
would wrap these two calls and move payloads. That integration does not
exist yet.

## Device boundary: what is verified

This was established from the ZoomISO Cloud setup guide and the native log
lines recorded by the audience gate. No ZoomISO source code is available.
Anything not listed as verified is unknown; it is not designed around as if
it were known.

| Question | Status | Evidence |
| --- | --- | --- |
| Where are the virtual camera and microphone? | **Verified: inside ZoomISO.** Its footer input feeds both from one selected source. | Setup guide: "One source feeds both the virtual camera and the virtual microphone." |
| What can we control? | **Verified:** only the MXL pair offered to ZoomISO's input (or an NDI source). We cannot hook Zoom's device callbacks. | Setup guide: the footer input is an MXL video flow, a test card or tone, or an NDI source. |
| Input payload formats | **Verified:** MXL `video/v210` (BT.709, progressive) and `audio/float32` at 48 kHz, 2 channels. | Setup guide, MXL format section. |
| Audio path into Zoom | **Logged, not documented:** ZoomISO converts F32LE stereo to S16LE mono at 48 kHz and pushes it (`send_audio_samples`, `swap_mic`). | Audience-gate native log. |
| Audio device stack | **Verified present:** PulseAudio runs in the container. Whether the virtual microphone is clocked through it is **unknown**. | Setup guide, `--shm-size` note. |
| Push or pull at the Zoom SDK | **Unknown.** The log suggests ZoomISO pushes audio; the pacing clock and block size are not documented. | — |
| Callback or send timestamps | **Unknown.** | — |
| Requested audio block size | **Unknown.** | — |
| Video send cadence | **Unknown.** The audience capture showed reduced unique cadence. | Audience gate. |
| One process or thread for both devices | **Unknown**, though one container serves both. | — |
| Capture output format | **Verified configurable**, and the default is **`1080p29.97fps`**. An observer row left at 29.97 against a 30 fps reference adds about 1,000 ppm of its own. That is too small to explain +9,400 ppm, but it should be pinned to 1080p30 for measurements. | Setup guide, "Default output format". |

**Consequence.** The paired scheduler cannot sit at Zoom's callbacks in the
current product, because those belong to ZoomISO. It can run upstream, on
the MXL pair we offer, only if a device clock can be observed. Today the only
observable is the audience-side measurement. The safe options are:

- a ZoomISO change that applies this contract at its own device boundary
  (a vendor request);
- an integration that uses an observable send clock, if ZoomISO exposes
  one; or
- a measurement-driven loop on the audience observer, which is slow, coarse,
  and needs the reference content.

The exact non-proprietary questions and a minimal read-only telemetry shape are
collected in [ZoomISO meeting-return integration questions](ZOOMISO_DEVICE_BOUNDARY_REQUEST.md).

## Rate controller (ratio policy)

`Limits(correction="ratio")` replaces whole-sample drop and hold for
production use. The default stays `"sample"`, so the 19 original regression
tests are unchanged. `RateController` is bounded PI control of the
source:output sample ratio, driven by the shared-clock error:

- **Sign:** audio ahead gives a ratio below 1 (consume the source more
  slowly); audio behind gives a ratio above 1.
- **Cap:** `max_correction_ppm` (2%). `saturated` is reported when the request
  reaches the cap. The integral stops winding into the cap (anti-windup).
- **Slew:** at most `max_slew_ppm_per_s` per second of audio. Time advances by
  audio quantity, not callback wall time, so a catch-up burst still slews.
- **Jitter:** the proportional term ignores error inside the deadband (5 ms).
  The integral tracks a steady rate offset.
- **Telemetry:** requested and applied ratio and ppm, `saturated`,
  `slew_limited`, plus `resampled_source_samples` and
  `resampled_output_samples`.
- **Output:** `("resampled", start, source_n, output_n)` segments. Producing
  `output_n` samples from `source_n` is a resampler's job. This contract
  processes no payloads and makes **no DSP-quality claim**.
- **Latency and discontinuities:** a ratio corrects drift, not transients.
  An error beyond `ratio_reanchor_ms` (40) is an explicit re-anchor, which
  bounds latency deviation to about 40 ms. Re-anchors within
  `reanchor_episode_s` (1) form one episode, so a stall's catch-up burst
  counts once. The controller keeps its learned rate across a position
  re-anchor.
- **Fail-closed:**
  - two consecutive **capacity** re-anchors in one direction mean
    `uncorrectable_rate_mismatch`. A capacity re-anchor is reached by a
    gradual ramp while saturated; a stall arrives as an abrupt jump and
    never counts;
  - the window and trend rules still apply;
  - the episode budget gives `reanchor_budget_exceeded`.

| Scenario (ratio policy) | Drift / range | Whole-sample edits | Health |
| --- | ---: | ---: | --- |
| Nominal | 20 ppm / 7 ms | 0 | ok |
| Measured shape, 120 s | −11 ppm / 49 ms (ratio about +9,400 ppm) | 0 | ok |
| Microphone −5,000 ppm | 155 ppm / 17 ms | 0 | ok |
| Microphone +19,000 ppm | −248 ppm / 25 ms | 0 | ok |
| Microphone ±30,000 or +25,000 ppm | — | 0 | failed (`uncorrectable_rate_mismatch`, about 4–6 s) |
| 300 ms microphone stall | 4 ppm / 42 ms (1 episode) | 0 | ok |
| Eight stalls in 40 s | — | 0 | failed (`reanchor_budget_exceeded`) |

## Payload resampler seam and quality gate

The real libsamplerate `sinc_best` candidate has passed this isolated objective
quality gate. That qualifies the payload algorithm and adapter seam; it does
not qualify a live Zoom meeting return or the target Linux runtime.

### Seam

`tools/mxl-atem-skin/meeting_return_resampler.py` turns the scheduler's
`("resampled", start, source_n, output_n, source_per_output)` segments into
audio.

`PayloadResampler.process(left, right, source_n, output_n,
source_per_output=...)` takes float32, 48 kHz stereo frames and returns
**exactly** `output_n` frames per channel. It passes the inverse of the
controller's continuous source:output ratio to libsamplerate. The quantised
`output_n / source_n` ratio exists only as a documented compatibility fallback.

| Property | Behaviour |
| --- | --- |
| Latency | A bounded output FIFO is primed with `prime_frames` (256, 5.33 ms) of silence, so the backend's group delay and its call-to-call output variation never become gaps or duplicates. The gate reports the measured latency. |
| Bounds | Underflow or overflow past `max_fifo_frames` (4,800, 100 ms) latches `failed`, and later calls refuse. |
| Ratio updates | The ratio is stepped with `src_set_ratio` before each segment. A wrapper that cannot provide step changes is refused because `src_process` smoothing produces measurable callback-rate phase modulation. |
| State reset | `backend.reset()` starts a new stream. The scheduler's re-anchor policy decides when a discontinuity is declared. |

### Backends and preflight

`select_backend()` returns a qualified candidate or a machine-readable
refusal. It **never substitutes a weaker algorithm**.

| Backend | Role | Licence | Availability |
| --- | --- | --- | --- |
| `samplerate-sinc-best` | Production candidate: libsamplerate `SRC_SINC_BEST_QUALITY`, streaming, explicit step-ratio updates | libsamplerate BSD-2-Clause; python-samplerate MIT | Passed the isolated real-backend gate with NumPy 2.5.4 and python-samplerate 0.2.4. Deployment still requires these dependencies and a repeat on the target Linux runtime. |
| `oracle` | Test instrument: evaluates the analytic reference exactly. Validates that the gate passes perfect output. | — | Always available; never production. |
| `linear`, `drop-hold` | Negative controls the gate must fail | — | Always available; never production. |

The lab runtime already uses GStreamer `pitch` (SoundTouch) plus
`audioresample quality=10` in `mxl_program_reclock.py`. They are alternative
candidates, but neither has been measured by this gate. Varying their ratio
per callback would need either `pitch rate` property updates or
`GstAudio.AudioResampler` with the variable-rate flag.

### Gate

`tools/mxl-atem-skin/meeting_return_resampler_gate.py --backend <name>` runs
a schedule that obeys the 20,000 ppm/s slew contract: unity, a ramp to
+9,400 ppm, a hold, a ramp to −5,000 ppm, a hold, and a ramp back. Callback
sizes rotate through 480, 441, 1,024 and 160 frames. The reference is
analytic: 997 Hz on the left and 3,001 Hz on the right, plus Hann bursts of
7 kHz (left) and 5 kHz (right), offset in time. So the ideal output is known
exactly at every fractional source time.

| Check | Threshold |
| --- | --- |
| Worst 50 ms window SNR against the ideal (after latency and fractional-delay alignment) | ≥ 90 dB |
| Worst sample error (click) | ≤ 0.001 (−60 dBFS) |
| Fitted-delay jump between windows (gap or duplicate is about 1 sample) | ≤ 0.25 sample |
| Delay slope in steady segments (unexpected pitch) | ≤ 5 ppm |
| Left/right crosstalk | ≤ −80 dB |
| DC offset | ≤ 1e−4 |
| Peak (clipping) | ≤ 0.99 |
| Frame accounting (requested = produced, source fully consumed) | exact |

The output is JSON, with the verdict, failures, metrics and the worst
windows. Exit codes: 0 is PASS, 1 is FAIL, 2 is REFUSED (backend
unavailable).

### Dynamic-ratio root cause (the isolated libsamplerate FAIL at `225b5b9`)

Codex ran libsamplerate 0.2.2 (python-samplerate 0.2.4) in a throwaway
environment. It passed steady state, but failed on ramps: 9 dB SNR, 0.35
spikes, and 2.3-sample delay jumps. All three causes were reproduced and
fixed offline:

1. **The adapter quantised the ratio.** It passed `output_n / source_n`. With
   whole-sample `source_n` per callback, that alternated, for example 480/484
   and 480/485 at +9,400 ppm, so the backend's pitch jittered by about ±0.2%
   every callback. Scheduler segments now carry the controller's continuous
   ratio as a fifth field, and `PayloadResampler.process(...,
   source_per_output=)` passes `1 / ratio` to the backend.
2. **The gate's ideal was wrong for real resamplers.** It changed the ratio
   at the scheduler's callback boundaries. Any correct streaming resampler
   changes it when it reaches that input, minus its filter look-ahead. The
   ideal is now an exact, causal, step-mode resampler with one fitted
   **switch latency**, reported as `switch_latency_source_frames`. The
   per-window delay is fitted as a warm-started, curvature-penalised spline.
   Continuity is checked where windows meet. A new bound,
   `max_timing_deviation_samples`, sits beside the existing thresholds, which
   are unchanged.
3. **libsamplerate smooths ratio changes.** `src_process` interpolates each
   change across the call. That is phase modulation at the callback rate, and
   it measures about 60 dB, so it fails honestly. `SamplerateBackend` now
   steps the ratio (`set_ratio`, which is `src_set_ratio`) before every
   `process`, and refuses a wrapper that cannot.

An exact step-mode instrument with a sinc-like look-ahead of 143 or 230
frames now passes exactly, and the fit recovers its switch latency. The
smoothing instrument, a single dropped sample, linear interpolation,
drop-hold, channel swap, a +20 ppm pitch error, and a backend that ignores
ratio changes all still fail.

**Remaining risk.** The isolated real-backend rerun measured the residual from
any ratio-dependent look-ahead well inside the gate, but the target Linux
runtime and the live Zoom device boundary still require separate qualification.

Measured offline (short schedule):

| Backend | Verdict | Worst SNR | Worst error | Delay jump |
| --- | --- | ---: | ---: | ---: |
| `oracle` | PASS | exact | 0.0 | 0.0 |
| `linear` | FAIL | 40 dB | 0.020 | 0.001 |
| `drop-hold` | FAIL | 3 dB | 0.598 | 0.82 |
| `samplerate-sinc-best` | PASS | 95.02 dB | 0.0000308 | 0.00043 sample |

Injected faults are also caught: channel swap, DC offset, clipping, and a
20 ppm pitch error.

The full-scale real-backend run produced exactly 429,120 requested frames from
429,800 consumed source frames with zero underflow, 0 ppm measured steady pitch
error, 0.00448 sample maximum timing deviation, and a fitted 143.5-frame switch
latency.

**Still unproven:**

- the same backend package and performance on the target Linux runtime;
- listening quality;
- performance at the real callback rate;
- behaviour against ZoomISO's actual device clock (see "Device boundary").

## Simulator

`tools/mxl-atem-skin/meeting_return_sim.py` is event-driven on integer host
time from a seeded RNG. It does not sleep. These can each be varied
independently:

- camera frame rate and clock error;
- microphone period and clock error;
- jitter in each domain;
- stalls of the camera, the microphone, or the producer;
- upstream audio input gaps.

It measures lip sync the way the analyzer does: for each shown frame, the
time its content is heard minus the time it is first seen (positive means
audio is late). It reports min, max, median, range, and fitted drift.
`NaiveScheduler` is the pass-through baseline: newest frame per callback,
sequential audio, no shared clock.

### Results (20 s unless stated; seed 1)

| Scenario | Naive drift / range | Paired drift / range | Paired health |
| --- | ---: | ---: | --- |
| Nominal 30 fps / 48 kHz | 95 ppm / 35 ms | 4 ppm / 6 ms | ok |
| **Measured shape**: camera 25 fps, microphone +9,400 ppm | **9,477 ppm / 208 ms** | 3–19 ppm / 34–35 ms (120 s) | ok |
| Microphone −5,000 ppm | 62 ppm / 42 ms | −19 ppm / 6 ms | ok |
| Microphone +19,000 ppm (near the 2% budget) | — | 0 ppm / 6 ms | degraded |
| Microphone ±30,000 or +25,000 ppm | 30,119 ppm / 581 ms | — | **failed**: `uncorrectable_rate_mismatch` within about 3 s |
| One 300 ms microphone stall | 300 ms range | 79 ms range, re-anchors counted | ok |
| Eight stalls in 40 s | — | — | **failed**: `reanchor_budget_exceeded` |
| Camera stall, producer stall, 50 ms upstream gap | — | < 10 ms range | ok |

The naive baseline reproduces the field result: +9,365 ppm measured against
+9,477 ppm simulated. Pairing on one clock removes the drift and bounds
the skew. Video at 25 fps keeps a constant audio lead of up to one camera
period (about −13 to −20 ms here), because a frame appears only on a camera
callback. That lead is inside the analyzer's lead allowance and can be
trimmed later with a deliberate video lead.

## Tests

`test_meeting_return_clock.py` has 19 tests and needs no sleeps, Zoom, or MXL.
They cover:

- bounded queues and overrun accounting;
- the clock waiting for the target latency;
- input gap and overlap accounting;
- validation of the limits;
- a stable shared clock with zero corrections;
- the **measured-shape regression**: the naive baseline drifts more than
  8,000 ppm, while the paired scheduler stays under 50 ppm with less than
  50 ms range;
- 120 s stability;
- correction accounting for slow and fast microphones (within 10% of the rate
  error);
- **fail-closed** behaviour on an uncorrectable mismatch in both directions,
  latched and within about 5 s;
- degraded health near capacity;
- stall recovery, and the re-anchor budget as a rolling window;
- camera stalls, upstream gaps, and producer stalls;
- determinism;
- the health contract;
- explicit pre-start output.

## Limits and next steps

1. **The scheduler's whole-sample correction is not the production payload
   path.** The qualified candidate processes payloads through the bounded
   ratio contract above, but it is not yet packaged into or accepted at the
   live Zoom meeting-return boundary.
2. **Video only drops or repeats.** There is no frame interpolation.
3. **The ZoomISO device boundary is closed.** See "Device boundary: what is
   verified". Its callbacks and send pacing are inside ZoomISO and are not
   documented.
4. **Defaults are first choices.** Latency 80 ms, correction 2%, skew bounds
   50/100 ms, and re-anchor 100 ms come from simulation, not from Zoom.
5. **Acceptance is unchanged.** An integration passes only through the same
   independent audience-side A/V gate. Simulator results are not acceptance.
