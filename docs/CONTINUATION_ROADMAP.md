# Contributor continuation roadmap

This document is the public-safe checkpoint for continuing development after
the October 2026 live demonstrations. It deliberately excludes private
addresses, meeting details, flow UUIDs, participant information, and raw
operational evidence.

## Proven foundation

The current feature branch contains a tested vertical slice:

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

## Recommended order of work

### 1. Finish the operator-visible bolt-on proof

- Import Companion 0.5.0 into a test Companion instance.
- Place the generated Playout and Keyed Program/Preview presets on a test page.
- Demonstrate that source registration updates Skin, Companion, and NMOS
  without restarting Core.
- Keep the existing default Core untouched until a separate promotion decision.

### 2. Build an objective A/V reference source

Extend the diagnostic source into a paired reference generator with:

- moving frame-count/timecode video;
- a 48 kHz audio click or tone event tied to a visible frame event;
- stable paired MXL identities and discoverable grouping; and
- machine-readable event timing.

Use a receive-side capture and measurement tool to calculate content-level
audio/video offset, jitter, dropouts, duplicated audio, and discontinuities.
Listening alone is useful for triage but is not an acceptance measurement.

### 3. Close the meeting-to-meeting Program return

The reclocker has passed an isolated 10-minute cadence test, but the audience
return still exhibited audible artifacts and apparent lag. The mixer latency,
presentation offset, and unsynchronized-sink CLI controls are experimental
test hooks—not accepted settings.

Run a small matrix against the objective reference source. For each setting,
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
