# A/V reference source and analyzer

Status: Gate A passed. A 10-minute direct-MXL baseline is zero-loss and
sample-exact, with A/V offset 0.000 ms and no drift, using the direct capture
reader. The Program-return audio stages have been diagnosed in isolation (Gate C
below). Nothing has yet been run through Core switching or into a meeting.

This replaces listening as the acceptance measurement for audio cadence,
sample continuity, and lip sync through Core, Program Audio, and meeting
returns.

## Timeline contract

One 30 fps video grain `g` corresponds to 48 kHz audio sample
`s = g * 1600`. The publisher starts `g` at the host TAI clock's current grain,
so the content identifies wall time as well as order.

| Element | Content |
| --- | --- |
| Frame code | Two rows of 22 blocks at the bottom of the picture: white/black reference blocks, then `g mod 4,294,967,280` (32 bits) and CRC-8. The modulus is a multiple of 120, so event identity survives the wrap. |
| Flash patch | White on frames where `g % 30 == 0`, black otherwise. |
| Audio event | A 20 ms raised-cosine tone burst starting on the first sample of every flash frame. Its frequency (600, 900, 1200, or 1500 Hz) is `(g // 30) % 4`, so pairing is unambiguous for offsets up to +/-2 s. |
| Counter (lossless mode only) | The right channel carries `(s mod 2^20 - 2^19) / 2^22`, exact in Float32. Use it to find sample gaps, duplicates, and regressions on gain-only MXL paths. |
| Human-readable | TAI time-of-day timecode, a moving bar, and the current event code. |

Use `--audio-mode meeting` (the default) for any path that may downmix,
encode, or apply AGC. **Never route lossless mode into a meeting**: the
counter channel is a full-scale sawtooth.

The frame code decodes after downscaling to 480x270 and JPEG quality 20, and
CRC rejects blended or corrupted frames instead of misreading them.

## Publishing

```sh
python3 tools/mxl-atem-skin/mxl_av_reference_source.py \
  --domain /mxl-domain \
  --video-flow <new video flow id> --audio-flow <new audio flow id> \
  --group MXLAVReference --audio-mode meeting \
  [--core-url http://127.0.0.1:<port> --source-id av-reference]
```

One GStreamer pipeline writes both flows from one frame counter, with group
hints `<group>:Video` and `<group>:Audio`. Buffer timestamps are anchored to
MXL time, using the pipeline base time and the gst-mxl-rs clock offset
`D = mxl_now - pipeline_clock_now`, taken from the tightest of 50 bracketed
clock reads. As a result the sink writes grain `g` at MXL index `g` and sample
`g * 1600` at index `g * 1600`: **the frame code equals the MXL index**.
Without the anchor, the sink quantizes an arbitrary per-run video phase to a
grain, which produced constant but run-dependent offsets of -1.25 ms and
-10.83 ms in early live runs. The greyscale picture is packed to
v210 in numpy, and only the bar, timecode, flash, and code regions are repacked
each frame. There is no scaler, colour converter, or leaky queue that could drop
grains silently. The publisher logs `stats` lines with frames, render time, and true lateness:
buffers handed to the sink after their running time, which are written late,
never dropped. With `--core-url`, it registers a
runtime source of kind `reference` and sets `audio_flow_id` to the paired
audio. Run it in its own container with its own flow identities, and remove
it by exact name. The publisher needs the same Python/GStreamer/OpenCV/MXL
plugin environment as `mxl_demo_sources.py`.

## Capturing

The analyzer needs the video and audio on one capture timeline:

- **Audience side**: a local recording of the destination meeting, or any
  capture ffmpeg can decode. Analyze it with `--media`.
- **Inside MXL**: `mxl_av_capture.py` runs both `mxlsrc` branches in one
  pipeline. It reads video as v210 with no conversion and decodes each grain's
  code from about 400 sampled pixels, writing `video.codes` and `audio.f32`.
  `meta.json` separates index steps (from timestamps: reader-side loss) from
  content steps (from codes: publisher-side loss). It also reconstructs each
  buffer's MXL index to check that the code equals the grain index and the
  lossless counter equals the sample index. Finally it lists the exact
  analyzer arguments. Two separately started pipelines do not share a timeline.
- **Raw**: 8-bit luma frames (`--video-raw`, `--width`, `--height`), one capture
  time per frame (`--video-pts`, or `--fps`), and interleaved Float32 48 kHz
  audio (`--audio-raw`, `--audio-start-s` for its first sample's capture time).

## Analyzing

```sh
python3 tools/mxl-atem-skin/mxl_av_analyze.py --media capture.mkv --series
python3 tools/mxl-atem-skin/mxl_av_analyze.py --video-raw v.y8 --video-pts v.pts \
  --audio-raw a.f32 --audio-start-s 0.0 --counter-channel right
```

The JSON report contains:

- **video**: decoded and undecodable frames, step histogram, repeats, skips,
  regressions, and the longest freeze in frames and seconds;
- **audio/sync**: detected events, unmatched events, the minimum/median/maximum
  A/V offset and its range, and drift in ppm. The offset is
  `audio_time - video_time`, so a positive value means audio is late;
- **counter** (when requested): estimated gain, continuous steps, duplicate
  samples, gap events and samples, and regressions.

The exit status is 0 when every threshold passes and 1 when any fails, with
reasons listed. Default limits:

- audio lead <= 40 ms and audio lag <= 60 ms;
- offset range <= 20 ms and drift <= 100 ppm;
- undecodable frames <= 1%;
- **zero missing video grains**;
- at most two unmatched audio events (capture edges);
- at least three pairs; and
- no counter discontinuities.

Capture completeness is part of the verdict. A path that legitimately reduces
frame rate, such as a meeting, must opt in with `--max-skipped-grains -1` (and
may bound repeats with `--max-repeats`). Frames that were captured but could not
be decoded count against the undecodable limit, not as missing grains. Every limit is a CLI flag; record the
limits used with each result.

Repeats and skips are expected when the capture rate differs from 30 fps or a
meeting reduces frame rate. Interpret them against the capture rate; a long
freeze or any regression is a finding.

## Tests

```sh
python3 -m unittest tools/mxl-atem-skin/test_av_reference.py
```

The tests run on the standard library only. They cover:

- frame-code round trips at several capture sizes, with low contrast and noise;
- CRC rejection of corrupted frames;
- exact burst placement and Float32 counter exactness;
- onset detection through gain and noise;
- counter gaps, duplicates, regressions, and wrap;
- video repeats, skips, freezes, and regressions;
- drift measurement and pairing beyond half a second; and
- full CLI runs on synthetic captures, including one with 100 ms of audio lag
  and a frozen video segment.

## Live zero-offset baseline — 2026-10-15

An isolated container published the reference in lossless mode on new flow
identities. Nothing was routed to Core or a meeting. `mxl_av_capture.py`
captured both flows directly from MXL for 60 seconds on the shared lab host,
which was CPU-saturated (load average about 23-25 on 20 vCPUs).

| Measure | Result |
| --- | ---: |
| Publisher heads | video +71 grains and audio +115,200 samples over 2.4 s; both advancing |
| Audio captured | 2,874,240 samples, 0 buffer timestamp gaps |
| Sample counter | 2,874,239 continuous steps; gain 1.0; 0 gaps, 0 duplicates, 0 regressions |
| Audio events / paired | 60 / 44 (16 bursts fell on frames missing from the video capture) |
| A/V offset | -1.25 ms on every pair; range 0.00 ms; drift 0 ppm |
| Video frames decoded | 1,437 of 1,437; 0 repeats, 0 regressions |
| Video grains missing | 349 (about 19%) in 193 short skips; longest 24 grains |
| Pass/fail | passed with default thresholds |

The content-level audio/video relationship is locked: the offset is
constant to the measurement resolution and does not drift. The audio path
is sample-exact.

The video skips are a capture-environment finding, not an A/V-sync finding.
A 15-second reader without colour conversion missed about 6% of grains,
against 19% with conversion. So most of the loss is the capture's 1080p
conversion on a saturated host, and a remainder is on the publisher or reader
side. Before a long gate, give the publisher and capture dedicated CPU (or
crop before converting), then repeat the baseline until video skips are zero.

During the run, both Cores' hard counters were unchanged, no container
restarted, and only the exact test container was removed.

## Gate A: zero-loss direct-MXL baseline (2026-10-15)

After the first baseline, three changes removed avoidable loss and ambiguity
from the measurement path:

- the publisher packs v210 directly and has no scaler, converter, or leaky
  queue;
- the capture decodes codes from raw v210 instead of converting frames; and
- buffer timestamps are anchored to MXL time.

| Run | Duration | Grains | Missing / regressions | Code = MXL index | Audio | A/V offset | Verdict |
| --- | ---: | ---: | ---: | ---: | --- | ---: | --- |
| Before changes | 60 s | 1,437 | 349 / 0 | not checked | exact | -1.25 ms | fail (loss) |
| Direct v210, unanchored | 30 s | 894 | 0 / 0 | not checked | exact | -10.83 ms | pass |
| Anchored | 30 s | 906 | 0 / 0 | 906 / 906 | exact | -0.042 ms | pass |
| Anchored, reps 1-3 | 3 x 60 s | 5,421 | 0 / 0 | all | exact | -0.021 ms | pass x3 |
| Tightest clock sample | 60 s | 1,807 | 0 / 0 | all | exact | 0.000 ms | pass |
| 10-minute run A | 600 s | 18,004 | 6 / 1 | 18,003 | exact (28.79 M samples) | 0.000 ms, 0 ppm | **fail** |
| 10-minute run B | stopped at 398 s | - | - | - | - | - | **stopped** |

Audio was sample-exact in every run: no gaps, duplicates, or regressions, and
zero offset range and drift once anchored.

**Run A.** The single video failure came during a host stall of about 0.5 s:
the publisher logged buffers about 530 ms late at the same moment. The capture
skipped two indices and then received one grain labelled with index `i` but
carrying the picture of grain `i + 6`. The ring holds six grains. This matches
a check-then-copy race in the upstream `mxlsrc` discrete reader. It validates
the ring slot's grain index before copying the payload, but not after. If the
reader is preempted mid-copy long enough for the writer to lap the ring, the
copied picture belongs to a newer grain. Core's reader had the same exposure: it copied from the ring with no
post-copy check, protected only by its read window's margin from the oldest
slot. Core now re-reads the flow head after each copy. If the writer could
have lapped the slot, Core publishes its hold/unavailable grain instead and
counts the event as `copy_overwrite_suspects` in health. This change is
unit-tested and syntax-checked against the MXL SDK headers, but has not yet
been qualified live. The same post-copy re-check is recommended upstream for
`mxlsrc`. Without the content-code check this failure would have been
invisible.

**Run B.** The default operator Core recorded one `too_late` event on a
software-rendered diagnostic source while host load reached about 35 on 20
vCPUs. The safety monitor stopped the test, as designed. During the 10-minute
runs the test container used about 150-210% CPU: the publisher plus a reader
that copies every 5.5 MB grain.

**Runs C-E, with CPU headroom (owner-approved window).** The software
diagnostic sources were stopped and four stale probe readers were removed.
The run was then repeated on the shared disk-backed MXL domain, and on a
temporary tmpfs domain to test whether disk-backed flow storage causes the
laps:

| Run | Domain | Grains | Lapped (`index - code = -6`) | Audio | A/V offset | Operator Core |
| --- | --- | ---: | ---: | --- | ---: | --- |
| C sanity, 60 s | disk | 1,782 | 6 | exact | constant, 0 ppm (0.000 ms on unaffected events) | unchanged |
| D sanity, 60 s | tmpfs | 1,819 | 0 | exact | -0.021 ms | unchanged |
| E 10 min | tmpfs | 17,983 | 14 | exact (28.8 M samples) | -0.021 ms, range 0, 0 ppm | unchanged |

The laps also occur on tmpfs, so **disk-backed storage is not their cause**.
Disk-backed MXL flow storage is still not recommended for 1080p rings. The
consistent pattern is the upstream `mxlsrc` check-then-copy race, which
appears whenever the reader thread is preempted on a CPU-contended host (CPU
pressure about 20-25%). The publisher was also frequently late against the
pipeline clock, by up to about 1.6 s, while still writing every grain at its
correct index. With the diagnostic sources stopped and the probes removed, the
operator Core's hard counters stayed flat for the full 10 minutes.

**Run F: direct reader.** `mxl_av_direct_capture.py` reads the rings through
the MXL SDK. It samples only the code pixels in place and validates every
read afterwards, so the race cannot mislabel a grain. It ran on the shared
disk-backed domain under normal load (about 21-31 on 20 vCPUs), with every
co-located source running.

| Measure | Result |
| --- | ---: |
| Duration | 600 s |
| Video grains captured / code = MXL index | 18,002 / 18,002 |
| Torn reads / reader skips / missing / regressions | 0 / 0 / 0 / 0 |
| Audio samples / gaps / duplicates / regressions | 28,802,400 / 0 / 0 / 0 |
| A/V pairs | 600 / 600 |
| A/V offset / range / drift | 0.000 ms / 0.000 ms / 0 ppm |
| Operator and candidate Core hard counters | unchanged |
| Test container CPU (publisher + capture) | 84-148% |

**Gate A status: passed.** The reference publisher writes grain `g` and sample
`g * 1600` at exactly the same MXL time, and the direct capture is complete
and race-free. Captures through the generic `mxlsrc` element remain
susceptible to the check-then-copy race on contended hosts, so acceptance
captures should use the direct reader.

## Gate C: Program-return stage diagnosis (2026-10-15)

The deployed Program-return pair has three stages:

- a live GStreamer `audiomixer` reading meeting audio over `mxlsrc`, with its
  aggregation latency tuned to 20 ms;
- a reindexer that republishes the mix 4,800 samples later; and
- a video passthrough of Core Program.

Each stage was replicated in isolation on the lossless reference: new flows,
no Core, no meeting, and byte-identical copies of the deployed mixer and
reindexer. Captures used the direct reader, so every sample and grain is
accounted for. In this signal, silence runs are dropped audio.

| Variant | Duration | Silence (dropouts) | Counter gaps | A/V offset |
| --- | ---: | --- | ---: | ---: |
| C1 mixer, 250 ms latency | 60 s | 6 runs, 154 ms | 6 | 0.000 ms (max 7.0 on affected events) |
| C2 mixer, **20 ms** (as deployed) | 60 s | **175 runs, 20.4 s (34%)**, longest 3.4 s | 174 | ~0 (15 of 60 bursts lost) |
| C3 C2 + reindex 4,800 | 60 s | 129 runs, 24.9 s | 129 | **+99.98 ms** |
| C4 C3 + video passthrough | 60 s | similar to C3 | 90 | **+99.98 ms** |
| C5 mixer, 1,000 ms | 60 s | 0 | 0 | 0.000 ms |
| C5 mixer, 1,000 ms | 600 s | 41 runs, 2.4 s (0.4%), median 18.6 ms, longest 629 ms | 41 | 0.000 ms median, 0 ppm |
| C6 reindex only, no mixer | 60 s | 0 | 0 | **+100.000 ms**, range 0 |

Across every variant: no pipeline restarts, and no Core hard-counter movement.

**Findings**

1. **Lag is the reindexer, exactly.** It places audio 4,800 samples (+100 ms)
   later in MXL time. Otherwise it is sample-exact. A receiver that aligns
   audio and video by MXL time presents audio 100 ms behind the picture. The
   video passthrough and the mixer do not move the timeline.
2. **Artifacting is the live mixer dropping late input.** A clock-driven
   `audiomixer` emits silence for any input that has not arrived within its
   latency budget. On this CPU-contended host, input arrival stalls of several
   hundred milliseconds occur. At 20 ms a third of the audio is lost; at the
   250 ms default about six dropouts a minute remain; even 1,000 ms leaves
   about four a minute in clustered episodes. Raising latency reduces dropouts
   but cannot eliminate them, and it makes the output arrive later in wall time.

**Recommended direction (not yet implemented or qualified)**

- Remove the fixed 4,800-sample lead, so audio stays at its true MXL time,
  after confirming how the destination consumes the pair. That needs an
  audience-side measurement in the meeting.
- Replace clock-driven mixing with **index-domain mixing**: read every input by
  sample index with a bounded wait, sum, and write at the same index. Missing
  input then becomes an explicit, counted, late-or-held condition instead of
  silent silence, and placement stays exact.
- Reduce co-located CPU contention, which drives the arrival stalls.

## Index-domain Program Audio mixer (design, 2026-10-15)

`tools/mxl-atem-skin/mxl_index_mixer.py` implements the index-domain
recommendation above as a drop-in alternative to `mxl_audio_mixer.py`. It has
not been qualified live; the Gate C rerun against the lossless reference is the
next step.

- **No media clock.** For each 480-sample block ending at index `end`, it reads
  every active input at `end` through the MXL SDK (`mxl_direct.SampleReader`,
  with its post-copy overwrite check), sums with per-input gain, and writes at
  the same `end` with `mxl_direct.SampleWriter`. Reader and writer pass the
  same head index and count, so output sample `s` is the gain-weighted sum of
  input samples `s`. A single input at unity is copied bit-exactly. The MXL
  clock only stops the mixer from writing ahead of real time. When the mixer is
  behind, it catches up without waiting, so a CPU stall delays output but does
  not lose it while the input rings still hold the samples.
- **Bounded wait, in MXL time.** An input that has not reached `end` is waited
  for until MXL time reaches `end + max_wait`. The default is 500 ms, capped at
  half the shortest input ring so waiting cannot let another input's samples be
  overwritten.
- **Gaps are counted, never silently mixed as silence.** Per input, each block
  is `on_time`, `late` (it arrived during the wait, and `held_ms` records how
  long the output was held), or `skipped`, with a reason: `missing` after the
  full budget, `overrun` when the ring has moved past the index, `torn`, or
  `error`. A skipped input contributes nothing to that block, and the block is
  counted in `blocks_degraded`. An input that ran out of budget is marked
  `stalled` and polled without waiting until it catches up, so a dead source
  cannot hold Program Audio back. Silence is written only when no input is on
  air.
- **Same control surface.** `/state` and `/control` follow
  `mxl_audio_mixer.py`: `mode` afv/manual, `channels`, `master_gain`, and AFV
  from Core's `/api/mxl/v1/state`. Gain changes ramp linearly across one block.
  The output is a stable 48 kHz Float32 stereo flow with the `--group-hint`
  prefix. Inputs must already be 48 kHz, because index mixing cannot resample.
- **Unit coverage.** `test_index_mixer.py` uses fake SDK rings to cover exact
  placement, gain and sum, ramps, late, missing and stalled inputs with
  recovery, overrun, torn copies, the wait cap, run-loop pacing, and the
  control contract. The writer binding follows the public SDK header
  (`mxlCreateFlowWriter`, `mxlFlowWriterOpenSamples`, `Commit`/`Cancel`).

## Next live gate

1. Register it with the isolated Core candidate, take it to Program, and capture
   Program video with its paired audio.
2. Feed the meeting return and analyze an audience-side recording.
3. Record offsets, drift, cadence, and counters for 10 minutes before changing
   any latency or presentation-offset setting.
