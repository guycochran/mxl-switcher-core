# Preview 1 Program-return runbook

This runbook closes the highest-risk Preview 1 gap: a synthetic Program travels
through a destination meeting and is measured at the audience side. It uses no
participant picture, voice, name, or meeting detail as evidence.

## Roles

- **Contribution ZoomISO** joins the source meeting and publishes MXL.
- **Core plus Program Audio** produces the paired Program.
- **Return ZoomISO** joins the destination meeting and selects the paired
  Program input.
- **Audience observer** joins the destination meeting as a separate participant
  and records the returned participant locally. This is the measurement point;
  recording on the sending instance is not audience-side proof.

Avoid an audio feedback loop: the audience observer does not send microphone
audio, and the return participant is not mixed back into its own Program bus.

## Candidate under test

The first required candidate is deliberately small:

- frame-coded 1080p30 reference video;
- synchronized 48 kHz meeting-safe reference audio;
- index-domain audio mixing at the original MXL sample indices;
- an explicit 3,200-sample Program presentation alignment; and
- no clock reclocker between the index mixer and the return input.

The zero-displacement case is retained as a failed diagnostic: it measured a
constant -66.667 ms offset before meeting transport. The 3,200-sample case
passed the same 70-second paired MXL capture at -0.021 ms median, 0 ms range,
and 0 ppm drift. The bounded-PLL reclocker remains an optional comparison case,
not the assumed winner. The legacy clock-driven mixer and its 4,800-sample
displacement are diagnostic history, not Preview 1 candidates.

## Before changing anything

Complete the [real ZoomISO recovery gate](REAL_ZOOMISO_RECOVERY_GATE.md) first.
Do not use the audience meeting to discover an unresolved source-lifecycle
problem. Pin the exact passing Core and Program Audio revisions for this run;
the audience gate must not introduce a new media candidate.

Record, read-only:

- exact Core, Skin, Program Audio, ZoomISO, and reference-source revisions;
- container state and restart counts;
- Core hard counters and current Program/Preview routing;
- current return-input selection; and
- the exact temporary container names, flow identities, ports, and cleanup
  commands planned for this run.

Use isolated flow identities and an unused loopback control port. Refuse the
run if any candidate output already has a writer. Do not replace the operator
Core or the existing Program Audio service during the smoke.

Give the candidate Program video definition and index mixer one shared NMOS
group-hint prefix that the operator pair does not use. Confirm the return input
by its candidate flow identities, not by display name; this prevents pairing
candidate video with operator audio or vice versa.

## Smoke sequence

1. Join both ZoomISO instances to their intended meetings and join the audience
   observer to the destination meeting.
2. Start the paired reference as a temporary source and register it only with
   the isolated candidate Core.
3. Start the index-domain audio candidate on a new output flow. Pass the
   isolated candidate Core URL explicitly; never rely on the operator-port
   default. Set the mixer to Manual, turn on only the reference channel, and
   confirm `/state` shows only that channel active, with `on_time` advancing,
   zero skipped blocks, and zero reader-release errors.
4. Preview the reference, CUT it to Program, and select the candidate paired
   Program input on the return instance.
5. Confirm picture and audio at the audience observer. Immediately before
   recording, snapshot candidate Core hard counters, mixer per-channel skipped
   reasons, `blocks_degraded`, `write_errors`, container restart counts, and
   the first CPU/RSS sample. Then record at least 70 seconds; the smoke stage
   needs 60 seconds of decoded reference time, not merely a 60-second file.
6. Stop the recording without changing the candidate. Take the matching final
   counter/restart snapshot and retain the original media file unchanged.
7. Run the matrix plan, inspect it, then apply it to a new result directory.
8. Save final Core/audio state, resource ranges, restart counts, and cleanup
   results beside the private operational evidence.
9. Remove only the exact temporary source and candidate containers and
   unregister only the temporary reference source. Restore the recorded
   Program/Preview and return-input selections.

If the operator cannot verify the original return-input selection after the
test, the run is incomplete even when the media analysis passes. Restore the
return input before any further gate or cleanup of the evidence record.

The public-safe manifest starts at
[`config/program-return-matrix.example.json`](../config/program-return-matrix.example.json).
Copy it beside a `captures/` directory and adjust only the capture filenames
and declared configuration.

```sh
python3 tools/mxl-atem-skin/mxl_av_matrix.py matrix.json
python3 tools/mxl-atem-skin/mxl_av_matrix.py matrix.json \
  --apply --result-dir results/preview1-smoke-1
```

## Smoke pass criteria

- required matrix case: PASS;
- at least 60 seconds of decoded reference coverage;
- every paired content A/V offset from -40 to +60 ms;
- offset range no more than 20 ms and absolute drift no more than 100 ppm;
- no moving-reference freeze longer than 250 ms;
- no video regression;
- no unexpected process restart or Core hard-counter movement;
- no audio discontinuity or mixer skipped block after stabilization; and
- bounded CPU and RSS.

Reduced meeting frame rate may produce skipped reference grains, so total
skips are measured rather than rejected. Freezes, regression, unstable offset,
and audio-event loss remain failures. The matrix evaluates media; prove Core,
mixer, restart, CPU, and RSS criteria from recording-window snapshots and
deltas saved beside its report.

## CPU-only cloud sender qualification

The paired sender side completed a 600-second meeting-connected qualification
on the 32-vCPU CPU-only profile with a RAM-backed MXL domain. Eighty samples
covered 35,974 routed output grains and 59,957 audio blocks. Core recorded zero
holds, invalid/early/late reads, timeouts, reader reopens, sync faults,
no-common-window events, repeats, skips, regressions, presentation fallbacks,
or availability alarms. The audio mixer recorded zero degraded or skipped
blocks, history misses, release errors, or write errors. One audio block arrived
0.5 ms late and was held successfully without a discontinuity.

Both reference publishers reported zero late frames. CPU and memory remained
bounded:

| Service | CPU min / avg / max | Memory range |
| --- | ---: | ---: |
| Primary reference | 14.18% / 15.72% / 19.02% | 109.9-110.9 MiB |
| Standby reference | 13.88% / 15.87% / 19.67% | 109.6 MiB |
| Core | 11.39% / 14.50% / 24.46% | 75.53-81.90 MiB |
| Index audio | 0.78% / 1.17% / 1.66% | 16.85-18.33 MiB |
| Meeting return | 37.93% / 41.65% / 48.55% | 238.4-260.3 MiB |

An initial run using a disk-backed shared MXL domain failed before transport
qualification despite ample CPU. Moving the same isolated stack to RAM-backed
media storage removed the cadence and audio faults. RAM-backed MXL storage is
therefore part of the qualified same-host sender profile, not an optional
tuning suggestion.

The return participant joined and sent Program audio and video successfully.
An independent ZoomISO observer discovered that participant, but the meeting
did not grant the observer recording/capture permission, so Zoom's SDK refused
to attach its raw audio/video readers. The temporary observer mapping was
removed. This result qualifies the meeting-connected sender; it does **not**
replace the audience-side recording and matrix analysis required below.

## Promotion ladder

Do not tune several variables at once after a failure. Classify it first as
source, Core video cadence, index mixer, return sender, meeting transport, or
audience capture.

1. Passing smoke: repeat the same immutable configuration for slightly more
   than 10 minutes.
2. Passing 10-minute gate: repeat for slightly more than 30 minutes.
3. Passing 30-minute gate: run the unchanged configuration for eight hours.
4. Only then name the paired path the Preview 1 Program Master profile.

If the direct index candidate fails solely because the destination requires
clock recovery, test the bounded-PLL case as a separate capture. Never hide a
failed direct result by overwriting its media or report.
