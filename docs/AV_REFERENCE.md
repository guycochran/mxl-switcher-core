# A/V reference source and analyzer

Status: Gates A and B passed. A 10-minute direct-MXL baseline is zero-loss and
sample-exact, with A/V offset 0.000 ms and no drift, using the direct capture
reader. A separate 10-minute isolated Core run on a CPU-only cloud VM was also
zero-loss and sample-exact. The Program-return audio stages have been diagnosed
in isolation (Gate C below). The meeting-return path has not yet passed an
audience-side measurement.

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
- no repeated moving-reference freeze longer than 250 ms;
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

## Qualification matrix

`tools/mxl-atem-skin/mxl_av_matrix.py` runs the analyzer over a set of
existing audience-side captures, one per named Program-return configuration,
and writes one qualification report. It works offline: it never records,
joins a meeting, or touches a container or the network.

```json
{
  "schema": "mxl-av-matrix/1",
  "matrix": "preview1-program-return",
  "stage": "10min",
  "defaults": {
    "limits": {"max_skipped_grains": -1, "max_freeze_ms": 250},
    "event_channel": "left"
  },
  "cases": [
    {"name": "index-mixer", "capture": "captures/index-mixer.mkv",
     "configuration": {"name": "index-mixer+clock-recovery", "max_wait_ms": 500}},
    {"name": "clock-mixer", "capture": "captures/clock-mixer.mkv",
     "configuration": {"name": "clock-driven-mixer"}, "required": false}
  ]
}
```

```sh
python3 tools/mxl-atem-skin/mxl_av_matrix.py matrix.json            # plan only
python3 tools/mxl-atem-skin/mxl_av_matrix.py matrix.json \
  --apply --result-dir results/preview1-10min-1                     # run
```

**Manifest.** `limits` keys are the analyzer's threshold flags in snake
case. `-1` disables `max_freeze_ms`, `max_skipped_grains` and `max_repeats`.
Case limits override `defaults`. Capture paths are relative to the manifest.
`configuration` is free-form but must have a `name`. A case is required
unless it says `"required": false`.

**Refusals.** These are refused with exit 2, before anything is written:

- unknown keys;
- malformed or out-of-range limits;
- duplicate or unsafe case names;
- a missing capture;
- an empty matrix;
- an unknown stage;
- an existing result directory, or one whose parent is absent.

**Plan.** Without `--apply`, the runner prints every case and its exact
analyzer command.

**Apply.** With `--apply`, the runner creates only the named result directory.
For each case it records:

- the capture's SHA-256 and size;
- the declared configuration and the effective limits;
- the analyzer command, exit status, and full report.

The runner also records the source revision: the Git commit, whether the tools
directory is dirty, and tool hashes. It keeps each case's raw analyzer output
next to `report.json`.

**Stages.** The stages are `smoke` (60 s), `10min` (600 s), `30min` (1,800 s)
and `8h` (28,800 s). A case covers the reference time it decoded: the sum of
its forward grain-code steps divided by 30. So a short or frozen capture
cannot satisfy a longer stage; capture a little longer than the stage. Use
`--stage` to re-check a manifest against another stage.

**Verdict.** The matrix passes only if at least one case is required and every
required case passes. An analyzer error counts as a failure. Exit codes are 0
for PASS, 1 for FAIL, and 2 for a refusal.

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

**Implementation status**

- The fixed 4,800-sample displacement remains excluded from the proposed
  audience-return profile so audio stays at its true MXL time. The destination
  meeting still needs to confirm the final paired presentation offset.
- **Index-domain mixing is implemented and its input lifecycle is live
  qualified**: it reads every input by
  sample index with a bounded wait, sum, and write at the same index. Missing
  input then becomes an explicit, counted, late-or-held condition instead of
  silent silence, and placement stays exact.
- Reduce co-located CPU contention, which drives the arrival stalls.

## Index-domain Program Audio mixer (design, 2026-10-15)

`tools/mxl-atem-skin/mxl_index_mixer.py` implements the index-domain
recommendation above as a drop-in alternative to `mxl_audio_mixer.py`. Its
real-MXL input lifecycle passed a strict 10-minute CPU-only cloud gate. The
remaining Gate C work is the paired content-level Program-return capture in
the destination meeting.

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
  full budget, `overrun` when the ring has moved past the index, `torn`,
  `invalid`, `offline`, or `error`. A skipped input contributes nothing to
  that block, and the block is counted in `blocks_degraded`. An input that ran out of budget is marked
  `stalled` and polled without waiting until it catches up, so a dead source
  cannot hold Program Audio back. Silence is written only when no input is on
  air.
- **Same control surface.** `/state` and `/control` follow
  `mxl_audio_mixer.py`: `mode` afv/manual, `channels`, `master_gain`, and AFV
  from Core's `/api/mxl/v1/state`. Gain changes ramp linearly across one block.
  The output is a stable 48 kHz Float32 stereo flow with the `--group-hint`
  prefix. Inputs must already be 48 kHz, because index mixing cannot resample.
- **Inputs come and go.** ZoomISO recreates its flows on restart, and the lab
  SDK crashes (SIGSEGV) on `mxlCreateFlowReader` for a flow that is not in the
  domain. The mixer therefore opens an input only after
  `mxl_direct.flow_exists`. An input that is absent, or not 48 kHz, is
  **offline** (`online: false`, `last_open_error`), never fatal; while it is on
  air its blocks are skipped as `offline`. Once per `--reopen-interval-ms`
  (default 1000 ms of MXL time), offline inputs are probed whether or not they
  are on air, so they are ready before a cut. A read that returns
  `MXL_ERR_FLOW_INVALID` (skipped as `invalid`) closes the reader and reopens
  it at once. A stalled input is also reopened once per interval, because a
  flow recreated under the same ID can leave the old reader on a mapping that
  never advances. Its stall is kept, so the reopen does not hold the output
  again. `opens` and `closes` count reader changes. One window remains: a flow
  removed between the existence check and the SDK open can still crash the
  process.
- **Unit coverage.** `test_index_mixer.py` uses fake SDK rings to cover exact
  placement, gain and sum, ramps, late, missing and stalled inputs with
  recovery, overrun, torn copies, the wait cap, run-loop pacing, the control
  contract, and the input lifecycle: absent at startup, wrong format, probed
  off air, recreated, removed, stalled and reopened, and a domain check that
  never calls the SDK for a missing flow. The writer binding follows the public SDK header
  (`mxlCreateFlowWriter`, `mxlFlowWriterOpenSamples`, `Commit`/`Cancel`).

## Real-MXL mixer lifecycle gate (harness, 2026-10-15)

`tools/mxl-atem-skin/mxl_index_mixer_gate.py` proves the input lifecycle
against a real MXL domain. Its corrected smoke and 10-minute confirmation have
now passed on the CPU-only cloud baseline.

**Isolation.** The gate creates a new tmpfs Docker volume as the domain and
never accepts a domain path. All gate containers run with `--network none` and
`--restart no`, publish no ports, and mount the tools directory read-only.
Mixer state is read with `docker exec`. Every container and the volume carry
`org.mxl.gate.run=<run id>`. Names and flow IDs (random UUIDv4) are new for each
run. The gate refuses, before any change, if any of these is true:

- a name or the volume already exists;
- the image is absent (it never pulls or builds);
- the tools directory lacks the mixer;
- the evidence directory exists;
- Docker is unusable.

The gate writes an explicit 1-second history setting into its private,
audio-only domain. This is deliberate test isolation: container lifecycle
commands can pause processes longer than MXL's default roughly 200 ms readable
history window, which turns a reader-lifecycle test into a host-scheduling
test. This setting does **not** change a production MXL domain and should not be
copied to a mixed audio/video domain without accounting for video memory.

**Sequence.** Two constant-tone writers drive the mixer: A at 0.125 and B at
0.25. The output sample therefore shows exactly who contributes (0.25 = B
only, 0.375 = A + B). The phases are:

| Phase | What happens | What must hold |
| --- | --- | --- |
| `a_absent` | The mixer starts with A absent and B writing, in manual mode with both channels on. | A is offline, its `offline` skips grow, the output is 0.25, and the mixer stays up. |
| `a_joined` | Writer A starts. | A goes online without a mixer restart, and the output is 0.375. |
| `a_gone` | Writer A stops and removes its own flow directory. | (Observed.) |
| `a_recreated` | A new writer creates the same flow ID; its `created=True` is required. | A's `closes` grew, `opens` ≥ 2, A is online, and the output is 0.375. |
| `hold` | `--hold-seconds`: 60 for the smoke, 600 to confirm. | Both inputs keep contributing. |

Throughout the run:

- the mixer container keeps one start time and zero restarts;
- the output head advances at every sample; and
- B never stalls and is never skipped.

Every wait is bounded, and so is the whole run (`max_total` in the plan). A
missing transition is a failure.

**Evidence and cleanup.** Before cleanup, the gate writes to the evidence
directory: container logs and inspect output, `samples.jsonl`, `events.json`,
`plan.json`, `verdict.json`, and `cleanup.json`. The verdict reports A's state
transitions, opens and closes, skipped reasons, block counts, and the mixer's
CPU and RSS range. Cleanup removes only the run's exact container names and
volume, each only after its run label matches. It never prunes or filters by
pattern.

**Known limit.** The one-syscall SDK race is not hidden. A flow removed between
`flow_exists()` and reader creation can still crash the mixer. The gate keeps
removal (writer A's exit) and re-creation apart by `--gap-seconds`, but a
crash fails the gate with the exit code recorded.

**Live result.** The first real runs found two distinct issues rather than
weakening the verdict:

1. Releasing an invalid reader through the shared MXL instance blocked or
   invalidated other real-time reader work. The mixer now gives each lifecycle
   reader its own MXL instance, detaches invalid readers immediately, and
   releases retired handles only during orderly shutdown. Pending releases and
   release errors are visible in state.
2. With the default history window, the Docker-driven transition produced one
   overrun on the always-present B input on both the loaded older host and a
   cloud smoke. Those runs are retained as scheduling evidence. The audio-only
   lifecycle gate now declares a 1-second private history window and leaves
   capacity/cadence qualification to the separate cadence gates.

The corrected 60-second smoke passed, followed by this CPU-only cloud result:

| Measure | 10-minute result |
| --- | ---: |
| Verdict / samples | PASS / 65 |
| Output blocks written | 62,366 |
| Output samples during hold | 0.375 throughout |
| Output-head stalls | 0 |
| Mixer restarts / non-running samples | 0 / 0 |
| A opens / closes / invalid detections | 2 / 1 / 1 |
| B stalls / skips | 0 / 0 |
| Write errors / reader-release errors | 0 / 0 |
| Mixer CPU, min / average / max | 1.10% / 2.77% / 3.22% |
| Mixer RSS, min / max | 17.55 / 19.39 MiB |
| Cleanup refusals | 0 |

The loaded older CPU host completed the same 10-minute default-history
sequence with stable output, zero restarts, 62 samples, and bounded
16.48-17.03 MiB RSS, but correctly failed the strict gate for one B overrun.
That is a useful capacity boundary, not a hidden pass.

```sh
# dry run (default): read-only collision checks and the exact plan
python3 tools/mxl-atem-skin/mxl_index_mixer_gate.py plan \
  --image <local MXL test image> --library <libmxl path in that image>
# smoke, then confirmation
python3 tools/mxl-atem-skin/mxl_index_mixer_gate.py plan \
  --image <local MXL test image> --library <libmxl path in that image> --apply
python3 tools/mxl-atem-skin/mxl_index_mixer_gate.py plan \
  --image <local MXL test image> --library <libmxl path in that image> \
  --hold-seconds 600 --apply
```

Use `--history-ms 200` only when intentionally combining lifecycle with a
default-window scheduling stress test.

Use `--docker "sudo -n docker"` where the daemon needs it. The exit code is 0
for PASS, 1 for FAIL, and 2 for a refusal. `test_index_mixer_gate.py` covers
the following with a simulated Docker daemon and clock:

- command construction and isolation;
- unique naming;
- collision refusals with no mutation;
- the passing sequence;
- failure paths: A never joins, a mixer crash on re-creation, re-creation
  not proven, B stalls, and a mixer restart;
- each verdict invariant;
- exact, label-checked cleanup;
- the tone writer removing only its own flow; and
- the probe.

## Gate B: through the isolated candidate Core (2026-10-15)

The reference was registered at runtime with the isolated candidate Core only
(never the operator Core). It was taken to Program through Preview + TAKE, and
exercised with CUT-aways and TAKE cycles. Core Program video and the
reference's own audio were captured with the direct reader. Every run
restored the candidate's exact starting buses and removed the registered
source.

| Run | Duration | Program frames | Repeats / skips | Regressions | `index - code` | A/V offset median (range) |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| Steady (smoke) | 60 s | 1,802 | 68 / 68 | 0 | +1 for 88%; -3 to +5 | -33.3 ms (167 ms) |
| CUT/TAKE (smoke) | 70 s | 2,102 (600 other sources) | 75 / 75 | 0 | +1 for 82% | -33.3 ms (133 ms) |
| 10 minutes, 6 CUT-aways + 3 TAKE cycles | 600 s | 18,001 (1,350 other sources) | 745 / 746 (4.1%) | 0 | +1 for 77%; -6 to +8 | -33.3 ms (333 ms) |

In every run: audio was sample-exact, there were zero torn reads, all 26
commands in the 10-minute run were accepted, and both Cores' hard counters
were unchanged. The source-side lateness trace rules out the reference: only
2.1% of its grains were written late, and only about 12% of Core repeats sit
next to one.

**Interpretation.** Core passes the reference through with a nominal one-grain
delay (-33.3 ms for audio taken from the source), but its cadence is not
frame-exact on this host. Two contributing causes were isolated using the
candidate's own binary in separate instances:

| Isolated Core | Repeats / skips (60 s) | `index - code` | Offset range |
| --- | ---: | --- | ---: |
| `input_delay_frames=0` (candidate setting) | 145 / 144 | -8 to +8 | 500 ms |
| `input_delay_frames=1` | 58 / 58 | +2 for 91% | 67 ms |

1. **Phase race.** With zero input delay, Core selects the source's newest
   grain on the same grain boundary at which an MXL-time-anchored source
   commits it. Scheduling jitter decides which grain wins. One grain of input
   delay removes most of this.
2. **Output timing under CPU contention.** The residual repeats, and negative
   `index - code` values, show Core writing some output grains after the
   source has moved on. Over the same window, the operator Core recorded
   similar repeat and skip counts on its own inputs.

**Gate B status on the shared older host: not passed** under the strict
zero-skip criterion. The behaviour is explained and bounded, with no
hard-counter movement. Recommended next steps:

- run Core with `input_delay_frames=1` (or measure 2) for MXL-anchored
  sources; and
- give Core dedicated CPU before repeating the 10-minute gate.

The +1-grain Program delay should be carried as a documented presentation
offset for audio paired with Core Program.

### CPU-only cloud qualification

Gate B was repeated on a 32-vCPU CPU-only cloud VM. Two independent synthetic
1080p30 sources, the isolated Core, and the direct capture each had a dedicated
CPU set. The MXL domain and capture directory were RAM-backed. Core used
`input_delay_frames=1`.

The first disk-backed smoke exposed storage stalls and did not qualify. Moving
only the isolated test domain and capture to RAM removed that variable. The
corrected 60-second smoke passed, followed by this 10-minute result:

| Measure | Result |
| --- | ---: |
| Duration | 600 s |
| Video frames | 18,002 |
| Video skips / repeats / regressions | 0 / 0 / 0 |
| Torn reads / reader skips | 0 / 0 |
| Audio samples | 28,800,960 |
| Audio gaps / duplicates / regressions | 0 / 0 / 0 |
| A/V event pairs | 600 / 600 |
| A/V offset | -66.667 ms, constant |
| Offset range / drift | 0.000 ms / 0 ppm |
| Core hard and cadence counters | all zero |

The fixed two-frame offset is the expected presentation relationship in this
fixture. It did not move during the run. Publisher lateness stayed at zero.
The two source processes each used 14.75-16.26% of one CPU and about 110-121
MiB RSS. Core used 9.71-10.24% of one CPU and 75-76 MiB RSS. Capture used
3.41-3.71% of one CPU; its RSS rose from 34 to 226 MiB while retaining the raw
audio capture, then the process exited normally.

**Gate B status: passed** for the isolated CPU-only profile. The result shows
that the strict cadence and A/V contract does not require a GPU. It also shows
that CPU allocation and RAM-backed MXL storage are part of the tested profile,
not optional tuning details. This does not qualify the audience-meeting return,
the operator stack, or eight simultaneous contribution feeds.

## Next live gate

1. Feed the meeting return and analyze an audience-side recording.
2. Record offsets, drift, cadence, and counters for 10 minutes before changing
   any latency or presentation-offset setting.
3. Repeat the isolated profile with real ZoomISO sources before using the result
   as an end-to-end contribution claim.
