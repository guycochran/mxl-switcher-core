# Preview 1: broadcast-ready vertical slice

Preview 1 is not a claim that every destination or workflow is finished. It is
one complete, repeatable production path that a broadcast engineer can operate
and independently verify:

> Join a contribution meeting, map six participants, prepare Preview, CUT to
> Program, mix clean audio, and return synchronized Program video and audio to
> an audience meeting on CPU-only infrastructure.

Core remains the deterministic MXL routing engine. The reference Skin,
Program Audio, meeting return, protocol adapters, and operator controls remain
replaceable bolt-ons.

## Release acceptance

Preview 1 is ready only when all of these are true:

- six contribution sources recover after restart without remapping by hand;
- the operator can build an off-air ME while the other ME remains on Program;
- Preview, CUT, Program tally, audio-follow-video, and manual audio are clear;
- the Program-return video is smooth and its audio is clean 48 kHz stereo;
- a destination-meeting capture passes the objective A/V analyzer for 30
  minutes, followed by an eight-hour soak;
- no Core hard counter, A/V discontinuity, process restart, or unbounded RSS
  growth occurs after stabilization;
- a fresh engineer can deploy the tested profile, recover it, and roll it back
  without editing source code; and
- the public repository contains the architecture, screenshots, limitations,
  acceptance evidence, and a short reproducible demo.

Listening and visual inspection remain useful operator checks, but do not
replace the captured A/V verdict.

## Work sequence

### P1 — Objective Program-return A/V

1. Put the existing frame-coded video and synchronized 48 kHz reference into
   the normal demo source catalog.
2. Treat a repeated moving reference longer than 250 ms as a failure, even on
   a deliberately reduced-rate meeting path.
3. Capture the audience side of the returned Program and measure offset,
   offset range, drift, freezes, undecodable frames, and audio events.
4. Remove the known fixed 4,800-sample audio displacement from the candidate
   path and qualify the index-domain mixer plus clock recovery as one pair.
5. Run a short parameter matrix. Promote only one named configuration after a
   clean 10-minute result, then repeat it for 30 minutes and eight hours.

The first live sequence is defined in the
[Program-return runbook](PREVIEW_1_PROGRAM_RETURN_RUNBOOK.md).

The result must record configuration, immutable revisions, duration, A/V
statistics, CPU/RSS, restart counts, and hard-counter deltas.

### P2 — Fast, predictable startup and recovery

- Restore persisted source identities and mappings without operator re-entry.
- Show `Starting`, `Waiting for source`, `Ready`, and `Fault` distinctly.
- Target a usable dashboard within 30 seconds and automatic source appearance
  as publishers join.
- Provide one scoped, reversible recovery action with a dry-run preview.

### P3 — Broadcast operator workflow

- Make ME 1 / ME 2 ownership and on-air protection unmistakable.
- Keep the on-air bank immutable while another 2-up through 8-up Look is built.
- Align browser and Companion labels, tally, CUT, audio controls, and health.
- Present Program, Preview, sources, audio meters, clock, and faults in one
  coherent multiview without requiring one encoder per source.

### P4 — Supported bolt-on profiles

Publish independently versioned examples in this order:

1. ZoomISO/MXL contribution input and meeting return;
2. NDI input and Program output;
3. SRT output;
4. RTMPS/YouTube output; and
5. DeckLink SDI/HDMI output.

Every adapter must expose identity, readiness, negotiated format, timing,
bounded buffering, restart count, and an actionable fault. An adapter failure
must not interrupt Core or another destination.

### P5 — Repeatable release and proof

- Ship immutable containers and pinned EasyMXL catalog entries.
- Run collision, active-writer, port, resource, and domain preflight checks.
- Document safe upgrade and rollback.
- Publish a tested six-input configuration and a first-picture guide.
- Add public-safe screenshots, a two-minute demo, supported-host profiles, and
  measured acceptance results.

## Explicitly outside the Preview 1 critical path

- TAMS trimming and timeline editing;
- partial-file or growing-file playback;
- putting codec, destination, or hardware dependencies into Core; and
- claiming eight simultaneous 1080p30 sources on the older lab host.

Simple full-file recording and replay may continue as an experimental
extension. The older host remains valuable for constrained CPU testing; the
CPU-only cloud profile is the performance baseline when additional headroom is
needed. Paid cloud machines must be deallocated after each test window.

## Current checkpoint

- Core routing, Preview/Program, ME banks, source lifecycle recovery, NMOS,
  Companion, the Skin, and health surfaces are implemented.
- The objective reference passed direct-MXL and isolated Core gates.
- Real-MXL Program Audio input lifecycle recovery passed a strict 10-minute
  CPU-only cloud gate.
- The previous meeting return exposed two known problems: a fixed +100 ms
  audio displacement and dropout-prone clock-driven mixing. The index-domain
  mixer addresses the latter, and a paired Core Program smoke measured and
  corrected its exact 66.667 ms presentation relationship without resampling.
  A longer repeat on the loaded older lab host kept audio perfect but exposed
  one-frame Core video-cadence shifts. Two 610-second repeats on the qualified
  CPU-only cloud profile kept audio and A/V alignment exact but each retained
  three isolated video repeats/skips. The head-sampling race was reproduced
  offline and corrected with a sequential input-timeline cursor. Its strict
  610-second cloud repeat passed 18,302 frames with zero repeats/skips,
  sample-perfect audio, and all 610 A/V events at exactly 0 ms. The follow-up
  prefers one fixed output-relative source target for deterministic A/V delay,
  while preserving a counted bounded-adaptive fallback for delayed or
  differently phased adapters. Read-only lab sampling found the active ZoomISO
  sources inside the fixed target window. An isolated two-flow writer
  stop/recreate gate then passed with both outputs near 30 fps, zero probe
  drops, zero selected skips/regressions, clean epoch resets, and deterministic
  recovery. A read-only two-source real-ZoomISO smoke subsequently held 4,262
  routes over 70 seconds with no post-baseline counter movement and zero probe
  drops at approximately 30 fps. Controlled real-ZoomISO interruption recovery
  remains open. A mixer-owned
  sample history also makes
  long presentation delays independent of an upstream writer's ring depth;
  that boundary has unit and real-MXL audio proof but is not yet a promoted
  cloud profile.
- Work has begun on P1 by making operator-visible video freeze an explicit A/V
  analyzer threshold rather than an informal observation.
