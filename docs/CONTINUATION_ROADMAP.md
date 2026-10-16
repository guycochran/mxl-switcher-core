# Contributor continuation roadmap

This document is the public-safe checkpoint for continuing development after
the October 2026 live demonstrations. It deliberately excludes private
addresses, meeting details, flow UUIDs, participant information, and raw
operational evidence.

The active product milestone is [Preview 1: broadcast-ready vertical
slice](PREVIEW_1_PLAN.md). The sequence below provides contributor history and
implementation detail; when priorities differ, the Preview 1 plan wins.

## Proven foundation

The current `main` branch contains a tested vertical slice:

- native MXL Program and Preview with grain-boundary Preview, CUT, and TAKE;
- independent read indices and cadence-hold correction;
- runtime registration and safe off-air removal of bolt-on sources;
- moving-timecode Playout and Keyed/graphics diagnostic sources;
- dynamic browser Skin, Companion choices/presets, NMOS IS-04 inventory, and
  immediate IS-05 Program/Preview routing;
- ME 1 / ME 2 composition with 2-up through 8-up Looks;
- EIC health and NMOS intelligence surfaces;
- native 48 kHz Program Audio mixing and an isolated clock-correct reclocking
  candidate; and
- public extension contracts and early output/recording prototypes in the
  separate `mxl-switcher-extensions` repository.

Read these records before changing behavior:

1. [Architecture](ARCHITECTURE.md)
2. [Control semantics](CONTROL.md)
3. [Core and optional extensions](EXTENSIONS.md)
4. [Runtime bolt-on acceptance](DYNAMIC_SOURCE_ACCEPTANCE_2026-10-14.md)
5. [ZoomISO MXL acceptance](ZOOMISO_MXL_ACCEPTANCE_2026-10-12.md)
6. [Program Audio candidate](PROGRAM_AUDIO_CANDIDATE_2026-10-14.md)
7. [Output architecture](OUTPUT_ARCHITECTURE.md)
8. [A/V reference source and analyzer](AV_REFERENCE.md)

## Recommended order of work

### 1. Finish the operator-visible bolt-on proof

- Import Companion 0.5.0 into a test Companion instance.
- Place the generated Playout and Keyed Program/Preview presets on a test page.
- Demonstrate that source registration updates Skin, Companion, and NMOS
  without restarting Core.
- Keep the existing default Core untouched until a separate promotion decision.

### 2. Build an objective A/V reference source — complete

Extend the diagnostic source into a paired reference generator with:

- moving frame-count/timecode video;
- a 48 kHz audio click or tone event tied to a visible frame event;
- stable paired MXL identities and discoverable grouping; and
- machine-readable event timing.

Use a receive-side capture and measurement tool to calculate content-level
audio/video offset, jitter, dropouts, duplicated audio, and discontinuities.
Listening alone is useful for triage but is not an acceptance measurement.

Status: complete for the direct-MXL baseline and isolated CPU-only cloud Core
gate. The 10-minute Core run delivered 18,002 consecutive 1080p30 frames and
28,800,960 audio samples with zero skips, repeats, regressions, torn reads,
sample gaps, or duplicates. All 600 A/V events held a constant measured offset
with zero range and drift. See [A/V reference](AV_REFERENCE.md).

### 3. Close the meeting-to-meeting Program return

The reclocker has passed an isolated 10-minute cadence test. A subsequent
host-authorized audience capture proved the upstream Core/index pair exact but
measured growing lag after the Zoom meeting boundary. The audio-only reclocker
remained internally healthy and did not solve the return: its final 20-second
sample measured 1,009.083 ms median lag, 130.729 ms range, and +9,365 ppm fitted
drift while Zoom delivered reduced unique-video cadence. The mixer latency,
presentation offset, and unsynchronized-sink CLI controls remain experimental
test hooks—not accepted settings. A fixed offset is specifically not a remedy
for measured drift.

The index-domain Program Audio mixer now also survives inputs that are absent
at startup or recreated later: unavailable channels remain offline and are
re-probed, while invalidated or stalled readers are reopened without restarting
the mixer. This lifecycle behavior is covered by offline tests and a strict
10-minute real-MXL lifecycle gate on the CPU-only cloud baseline.

Build the next candidate as a paired meeting-return adapter, not another
audio-only tuning variant. It should use one monotonic output clock for virtual
camera and microphone delivery, bound both queues, report callback cadence and
queue depth, and count every audio correction and video drop/repeat. Then run a
small matrix against the objective reference source. For each setting,
capture the audience-side result and retain:

- exact video cadence and repeats;
- exact audio sample cadence, gaps, overlaps, and PTS continuity;
- content-level A/V offset and range;
- process CPU/RSS and restart counts; and
- Core hard-counter deltas.

Only promote a paired Program return after a clean 10-minute test, then repeat
for 30 minutes and eight hours.

### 4. Turn examples into independently versioned extensions

Keep protocol and destination logic outside Core. Prioritize:

1. Program return / meeting output;
2. NDI output;
3. SRT output;
4. RTMPS output;
5. DeckLink SDI/HDMI output;
6. recording; and
7. archive/TAMS integration after recording and full-file playback are solid.

Each adapter must preserve stable MXL identities, expose readiness and bounded
buffer telemetry, and fail without interrupting Core or another adapter.

### 5. Promotion and releases

- Review the feature branch as a sequence of independently understandable
  commits.
- Do not replace an installed/default Core binary as a side effect of testing.
- Merge only after the relevant acceptance record is complete.
- Tag Core, Companion, and extensions independently and pin tested versions in
  deployment profiles.

## Verification commands

From the Core repository:

```sh
make test
make live-syntax MXL_INCLUDE=/path/to/mxl/lib/include
python3 -m unittest discover -s tools/mxl-atem-skin -p 'test_*.py'
npm test --prefix companion-module-mxl-core
npm run package --prefix companion-module-mxl-core
```

Do not claim a live result from unit tests alone. A live gate also needs
advancing outputs, zero probe drops, bounded resources, zero unexpected
restarts, and unchanged hard counters after stabilization.

## Guardrails

- Preserve user changes and unrelated dirty files.
- Never commit credentials, participant identities, private addresses, raw
  operational logs, hostnames, or flow UUIDs to the public repository.
- Treat common-window width, source skew, repeats, held frames, skips, and
  timeline resets as measurements unless an output stalls or an active alarm
  accompanies them.
- Keep TAMS playback and trimming out of the critical path until simple record
  and complete-file replay are dependable.
- Deallocate paid cloud machines when no active test is running.
