# MXL Switcher Core: current project snapshot

Updated: 2026-10-15

MXL Switcher Core is a headless, CPU-only production engine that keeps media in
MXL and exposes stable Program and Preview flows. It is designed to be embedded
in a deployment platform or paired with any operator interface rather than
forcing a particular UI, cloud, I/O protocol, or show workflow.

## Ready to evaluate today

- Native progressive v210 switching with stable Program and Preview identities.
- Preview, direct Cut, and bus-swap Take applied on output grain boundaries.
- Eight registered 1920x1080p30 sources on the current live-lab profile.
- Runtime source registration, removal, and atomic rebinding while preserving a
  stable logical source ID.
- Real-MXL audio-input lifecycle recovery: absent inputs can join, disappear,
  and recreate the same flow ID without restarting the index mixer; a strict
  10-minute CPU-only cloud gate passed with zero skips on the surviving input.
- Versioned HTTP control, reconnectable SSE state/tally, and detailed health
  telemetry.
- Immutable container deployment with dry-run preflight, retained rollback,
  exact output-collision protection, port checks, and bounded verification.
- EasyMXL packaging plus deterministic discovery, writer attribution, and an
  optional fail-closed live-writer guard.
- Optional ATEM-style reference Skin, Companion/Stream Deck controls, NMOS
  integration, browser confidence views, and a loopback-only EIC extension
  readiness panel.

## Latest live result

The current immutable Core build was promoted in a live CPU-only eight-source
facility after two independent guards correctly refused to start a competing
Program/Preview writer. Once the previous operator was stopped and retained for
rollback, the replacement launched and completed a 602.8-second hold:

| Measurement | Result |
|---|---:|
| Samples | 61 |
| Output frames advanced | 17,999 |
| Core hard-counter errors | 0 |
| Core container restarts | 0 |
| CPU, minimum / average / maximum | 49.22% / 65.32% / 122.10% |
| Memory, first / last | 77.41 MiB / 77.45 MiB |

Program and Preview retained their selections. The reference Skin, Program
Audio, NMOS registration, and Program/Preview/Multiview confidence paths stayed
healthy, and EasyMXL attributed both stable outputs to the replacement.

Full public evidence:

- [Immutable Core live promotion](IMMUTABLE_CORE_LIVE_PROMOTION_2026-10-15.md)
- [Dynamic source acceptance](DYNAMIC_SOURCE_ACCEPTANCE_2026-10-14.md)
- [ZoomISO MXL acceptance](ZOOMISO_MXL_ACCEPTANCE_2026-10-12.md)
- [NDI full-path findings](NDI_FULL_PATH_FINDINGS.md)
- [Objective A/V reference](AV_REFERENCE.md)
- [Preview 1 release plan](PREVIEW_1_PLAN.md)
- [Preview 1 Program-return runbook](PREVIEW_1_PROGRAM_RETURN_RUNBOOK.md)
- [Paired-clock meeting-return foundation](MEETING_RETURN_ADAPTER.md)
- [ZoomISO device-boundary integration questions](ZOOMISO_DEVICE_BOUNDARY_REQUEST.md)
- [Real ZoomISO output identity rotation](REAL_ZOOMISO_FLOW_ROTATION_2026-10-15.md)

## What is demonstrated versus productized

The repository includes an operator-ready demonstration stack, but Core remains
the product boundary:

```text
MXL sources -> Core -> stable PGM/PVW MXL flows -> independent bolt-ons
                   \-> HTTP/SSE -> any Skin, controller, or automation
```

The web Skin, Companion pages, SuperSource layouts, confidence encoders,
Program Audio service, NMOS adapter, and deployment-platform integration prove
the extension model. They can be replaced independently and do not sit inside
Core's media hot path.

The separate
[`mxl-switcher-extensions`](https://github.com/guycochran/mxl-switcher-extensions)
repository now publishes machine-readable concept contracts for ZoomISO, NDI,
SRT, RTSP, DeckLink, WebRTC, RTMPS, meeting return, and recording. It also has a
shared lifecycle/health runtime and fail-closed one-shot readiness check. Those
contracts make the intended boundary concrete; `concept` remains an explicit
non-claim of implementation or acceptance.

## Deliberately not claimed yet

- The Program Audio candidate and objective A/V reference have passed isolated
  timing gates. A paired Core Program plus index-audio smoke also passed at
  -0.021 ms median offset with zero range and drift after explicit presentation
  alignment. A 610-second repeat on the loaded older lab host retained perfect
  audio but failed the strict offset-range limit because Core video shifted by
  one frame under shared CPU load. Two further 610-second runs on the 32-vCPU
  CPU-only cloud profile held audio and A/V alignment exactly but each recorded
  three isolated video repeats/skips. The resulting source-head sampling race
  was reproduced offline and corrected with a sequential input timeline. A
  strict 610-second cloud repeat then passed 18,302 sequential frames, zero
  video faults, sample-perfect audio, and 610/610 A/V events at exactly 0 ms.
  Bounded recovery for genuinely fast/re-anchored sources is implemented. A
  deterministic output-relative target is preferred for clock-anchored inputs,
  with an explicit counted fallback to the bounded adaptive cursor for delayed
  or differently phased adapters. Read-only lab sampling placed active ZoomISO
  heads within the deterministic window. An isolated two-flow writer
  stop/recreate gate passed at 30 fps with zero output-probe drops, selected
  skips, or regressions after adding explicit writer-epoch reset handling.
  A subsequent read-only two-source real-ZoomISO smoke held 4,262 routes over
  70 seconds with no post-baseline counter movement; Program and Preview stayed
  near 30 fps with zero drops and one-frame final source lag. A controlled
  protocol-change test then proved that ZoomISO rotates both video and audio
  flow UUIDs when an output returns to MXL. A subsequent writer-specific gate
  discovered the replacement pair, atomically rebound the existing stable
  logical source, and recovered moving Program at 29.81 changes/s with zero
  probe drops, stalls, hard-counter movement, or permanent-service restarts.
  The bounded always-on lifecycle service is now complete. A subsequent
  600-second meeting-connected sender gate on the 32-vCPU CPU-only profile and
  RAM-backed MXL storage advanced 35,974 routed grains and 59,957 audio blocks
  with zero video holds/repeats/skips, zero Core hard-counter movement, and
  zero degraded or skipped audio blocks. The independent observer discovered
  the return participant. After the host granted co-host/capture permission,
  objective audience-side recording completed and exposed the next boundary.
  The paired Core/index output immediately before Zoom was exact, but the
  meeting return accumulated hundreds of milliseconds of audio lag and
  reduced unique-video cadence. An audio-only bounded-PLL comparison stayed
  internally clean yet ended a 20-second audience capture at 1,009.083 ms
  median lag, 130.729 ms range, and +9,365 ppm fitted drift. Core hard counters
  stayed at zero and the host had ample CPU/RAM headroom. The 30-minute and
  eight-hour holds were withheld. End-to-end acceptance now requires a paired
  meeting adapter that owns one audio/video output clock and exposes callback
  and queue telemetry; the clean sender result is not presented as end-to-end
  acceptance.
- Video-only NDI has completed loaded qualification. The independent paired
  NDI extension has also passed a 10-minute receiver gate: 18,000 video frames
  at 29.996 fps and 28,804,800 audio sample-frames at 48,002.7 Hz, with no
  receiver error or sender restart. Its readiness gate measures payload rate,
  not mere essence presence. Content-level timing and flow-replacement
  recovery are still required. Packaged SRT, WebRTC distribution, RTMP/RTMPS,
  SDI/HDMI, and recording adapters are not all production-qualified
  deliverables yet.
- The meeting-return libsamplerate `sinc_best` payload candidate has passed its
  isolated full-scale dynamic-ratio gate: 95.02 dB worst-window SNR, exact
  frame accounting, zero underflow, and no measurable steady pitch error. This
  qualifies the algorithm and adapter seam, not the target Linux package or an
  audience-side Zoom return.
- The included Skin is a reference implementation, not a requirement for using
  Core.
- Archive/replay and TAMS work is exploratory and is not part of the accepted
  switching baseline.

## Best entry points

- Build a platform or custom UI: [Architecture](ARCHITECTURE.md) and
  [Control API](CONTROL.md)
- Deploy a versioned service: [Deployment bundle](DEPLOYMENT_BUNDLE.md)
- Integrate discovery/lifecycle: [EasyMXL integration](EASY_MXL_INTEGRATION.md)
- Add destinations: [Output architecture](OUTPUT_ARCHITECTURE.md)
- Reproduce the operator workflow: [Operator demo](OPERATOR_DEMO.md)
- Choose an audience-specific path: [Corporate/on-prem](workflows/CORPORATE_ON_PREM.md)
  or [online production](workflows/ONLINE_PRODUCTION.md)

## Near-term direction

1. Package the isolated-qualified audio payload resampler behind the
   paired-clock contract on the target Linux runtime, obtain ZoomISO
   device-clock telemetry or a supported integration boundary, then repeat the
   content-level audience-side A/V gate.
2. Finish the paired NDI content-timing/recovery gates, then package independent
   SRT, RTMP/RTMPS, SDI/HDMI, recording, and WebRTC destination services
   without adding codecs to Core.
3. Turn the accepted immutable promotion sequence into repeatable fresh-host
   deployment profiles.
4. Continue proving useful capacity on ordinary CPU infrastructure, with larger
   CPU-only cloud instances as the high-density reference baseline.
