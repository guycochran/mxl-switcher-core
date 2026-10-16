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

1. **Sample correction is a drop or repeat of whole samples.** It is
   deterministic and accountable, but audible at a 1% rate. A production
   adapter should keep these counters and the error signal, and replace the
   correction with a bounded resampler that is driven by them.
2. **Video only drops or repeats.** There is no frame interpolation.
3. **The ZoomISO callback model is assumed.** The adapter assumes the
   virtual devices expose pull callbacks timed in host monotonic time. Check
   the real callback model and its timestamps before integrating.
4. **Defaults are first choices.** Latency 80 ms, correction 2%, skew bounds
   50/100 ms, and re-anchor 100 ms come from simulation, not from Zoom.
5. **Acceptance is unchanged.** An integration passes only through the same
   independent audience-side A/V gate. Simulator results are not acceptance.
