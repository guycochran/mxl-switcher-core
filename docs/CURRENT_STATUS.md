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
  integration, and browser confidence views.

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
  Bounded recovery for genuinely fast/re-anchored sources and input-lag
  telemetry are implemented; real-ZoomISO recovery and meeting-to-meeting
  audience acceptance remain open.
- NDI has completed loaded qualification. Packaged SRT, WebRTC distribution,
  RTMP/RTMPS, SDI/HDMI, and recording adapters are not all production-qualified
  deliverables yet.
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

1. Complete content-level A/V and audience-meeting return qualification.
2. Package independent NDI, SRT, RTMP/RTMPS, SDI/HDMI, recording, and WebRTC
   destination services without adding codecs to Core.
3. Turn the accepted immutable promotion sequence into repeatable fresh-host
   deployment profiles.
4. Continue proving useful capacity on ordinary CPU infrastructure, with larger
   CPU-only cloud instances as the high-density reference baseline.
