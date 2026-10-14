# MXL Switcher Core

[![CI](https://github.com/guycochran/mxl-switcher-core/actions/workflows/ci.yml/badge.svg)](https://github.com/guycochran/mxl-switcher-core/actions/workflows/ci.yml)

The first executable slice of a headless, skin-independent MXL production
switcher. It keeps MXL's native operating point—progressive v210 video—and gives
Program and Preview stable flow identities that do not disappear on a cut.

## Choose your path

Core is shared infrastructure, not a preselected production workflow. Start
with the path that matches the system you are building:

| I want to… | Start here | Typical optional bolt-ons |
|---|---|---|
| Build a switcher, automation system, or custom UI | [Core/API](docs/ARCHITECTURE.md) | Your own Skin or controller |
| Produce corporate events on premises | [Corporate/on-prem workflow](docs/workflows/CORPORATE_ON_PREM.md) | SDI/HDMI, NDI, SRT, PowerPoint, Companion |
| Produce meetings and streams online | [Online-production workflow](docs/workflows/ONLINE_PRODUCTION.md) | ZoomISO, meeting return, WebRTC, SRT, RTMPS |
| Add recording, clipping, and replay | [Archive/replay workflow](docs/workflows/ARCHIVE_REPLAY.md) | File recorder or TAMS |

The optional implementations and deployment profiles live in
[`mxl-switcher-extensions`](https://github.com/guycochran/mxl-switcher-extensions).
You do not need TAMS, ZoomISO, NDI, a cloud account, or SDI hardware to build or
use Core.

This is an executable vertical slice. The switching engine, lifecycle rules,
control sequencing, mock domain, demo, and invariant tests run on any C++20
machine. The production backend and bounded live harness have now been compiled
and exercised against the official MXL Flow API on a Linux MXL domain. A
versioned HTTP/SSE control service and dependency-free reference operator Skin
are included.

> **Core is the product boundary.** It owns frame-accurate routing, stable
> Program/Preview identities, health telemetry, and the versioned control API.
> The included operator surfaces are replaceable reference implementations.
> They demonstrate what can be built on Core; they are not required by Core and
> never sit in its media hot path.

## Repository map

```text
MXL Switcher Core
├── include/, src/, apps/        headless switching engine and live service
├── docs/                        architecture, control, deployment, acceptance
├── tools/mxl-atem-skin/         optional ATEM-style reference operator Skin
├── companion-module-mxl-core/   optional Companion / Stream Deck integration
└── config/                      extension contracts and example manifests
```

Choose the entry point that matches what you want to build:

- **Build a new switcher or automation system:** start with
  [Core architecture](docs/ARCHITECTURE.md) and the
  [control contract](docs/CONTROL.md).
- **Build a custom operator interface:** use the versioned HTTP/SSE API, then
  inspect the [ATEM-style reference Skin](tools/mxl-atem-skin/README.md) as one
  example—not as a required frontend.
- **Add a physical control surface:** use the
  [Companion / Stream Deck module](companion-module-mxl-core/README.md).
- **Add a destination:** follow the independent
  [output-node architecture](docs/OUTPUT_ARCHITECTURE.md) for Meeting, NDI,
  SRT, SDI, WebRTC, recording, or RTMP/RTMPS delivery.
- **Understand the repository boundary:** read
  [Core and optional extensions](docs/EXTENSIONS.md).
- **Run the complete demonstrated workflow:** use the
  [operator demo profile](docs/OPERATOR_DEMO.md) and the
  [community presentation](docs/decks/mxl-switcher-community-demo-v5-3.pptx).

## Architecture at a glance

```text
MXL sources ──> MXL Switcher Core ──> stable PGM/PVW MXL flows
                       │                         │
                       │ HTTP/SSE               ├─> compositor / multiview
                       │ control + state         ├─> Meeting return
                       v                         ├─> NDI / SRT / SDI / RTMP
              any operator surface              └─> WebRTC confidence monitor
              ├─ reference web Skin
              ├─ Companion / Stream Deck
              └─ your own UI or automation
```

Media remains in MXL between modular services. Operator surfaces exchange only
bounded control and telemetry with Core.

## What works now

- two or more registered `video/v210` sources;
- stable PGM and PVW output UUIDs, created exactly once;
- Preview, direct Cut, and broadcast-style Take (bus swap);
- commands committed only on an output grain boundary;
- source capture timestamps and opaque payload bytes preserved;
- strict format/source/index validation;
- dependency-free mock backend with bounded history and a fast soak test.
- official `mxl/flow.h` backend with direct SDK-buffer-to-SDK-buffer routing;
- bounded `mxl-core-live` harness with automatic TAKE cadence and metrics.
- aggregate and per-source health counters that distinguish upstream invalid or
  stale media from failures in the stable PGM/PVW outputs.
- automatic reader replacement detection and a measured hold-last-output policy
  that preserves Program/Preview cadence through a transient source restart.
- independent per-output CUT timelines with forward-only recovery floors, so
  unrelated source-index drift cannot collapse Program and Preview routing.
- one-shot sustained-unavailable alarms that clear on media recovery without
  fault-spamming every held output frame.
- dependency-free versioned control adapter for state, sources, Preview, Cut,
  and Take, with contract tests preserving frame-boundary command semantics.
- bounded HTTP/1.1 control transport, loopback-only by default, with real-socket
  tests and no access to MXL handles or media buffers.
- reconnectable Server-Sent Events for frame-applied state/tally revisions and a
  thread-safe live health endpoint with per-input routing counters.
- a responsive reference Skin at `/` (also `/skin`) with Program and Preview
  source buses, Cut, Preview, Take, tally, timing, and error telemetry.
- an optional, richer ATEM-style demonstration Skin with two persistent ME
  banks, safe off-air 2-up through 8-up preparation, WebRTC confidence
  monitoring, audio controls, and live tally.
- an optional Bitfocus Companion module with Stream Deck presets and live
  Program, Preview, and ME-bank feedback.

## Build and verify

Requires a C++20 compiler and Make.

```sh
make test
make demo
make soak
```

With the official MXL SDK installed on Linux:

```sh
make live
./build/mxl-core-live --domain /mxl-domain \
  --source-a SOURCE_UUID --source-b SOURCE_UUID \
  --program-def program.json --program-id PROGRAM_UUID \
  --preview-def preview.json --preview-id PREVIEW_UUID \
  --frames 300 --take-every 90 \
  --rate-numerator 30000 --rate-denominator 1001 \
  --control-host 127.0.0.1 --control-port 8080
```

The rate flags describe the facility operating point. For a 1080p30 domain use
`--rate-numerator 30 --rate-denominator 1`.

The one-million-frame soak is accelerated: it tests state and lifecycle pressure,
not eight hours of wall-clock scheduling. The real acceptance run will use the MXL
adapter on Linux for eight continuous hours.

## Current acceptance result

The local engine must prove these invariants before live SDK work:

1. PGM/PVW flow identity is stable through repeated cuts.
2. A control command cannot take effect between frames.
3. No colorspace conversion or payload mutation occurs.
4. Capture timestamps survive the route.
5. Output indexes are strictly monotonic.
6. A long accelerated cut loop neither drops mock grains, recreates outputs, nor
   grows the mock output history without a bound.

See [the architecture](docs/ARCHITECTURE.md) and
[control semantics](docs/CONTROL.md). See [Linux deployment](docs/DEPLOYMENT.md)
for the validated configuration format and non-root systemd service.
The clock-correct Program Master and independent Meeting, NDI, SRT, SDI,
WebRTC, recording, and RTMP/RTMPS output roadmap is documented in the
[output-node plan](docs/OUTPUT_ARCHITECTURE.md).
For the complete ZoomISO/Core/SuperSource operator demonstration, including the
confidence-monitor performance profile, Stream Deck controls, secure tunnels,
and NMOS discovery view, see the [operator demo profile](docs/OPERATOR_DEMO.md).
The matching [community-demo slide deck](docs/decks/mxl-switcher-community-demo-v5-3.pptx)
is included as a downloadable PowerPoint file.

The independent-CUT timing change has also passed live ZoomISO MXL validation
with four simultaneous 1920x1080p30 v210 inputs plus stable Program and Preview.
Clean 10-minute and 30-minute confirmations held every monitored stream at an
average 30.00 fps with zero probe drops, zero Core timing/read/synchronization
errors, and no active sustained-unavailable alarms while executing CUT,
Preview, and TAKE commands. See the
[ZoomISO MXL acceptance record](docs/ZOOMISO_MXL_ACCEPTANCE_2026-10-12.md).

A separately supervised Program Audio candidate now publishes one stable 48
kHz stereo flow, follows four applied Program selections without replacing its
identity, and appears in the engineering-health surface. A live timing probe
measured zero discontinuities and A/V timestamp proximity within one 30 fps
frame. This is not yet an accepted audio profile; content-level lip-sync and
staged duration gates remain. See the
[Program Audio candidate record](docs/PROGRAM_AUDIO_CANDIDATE_2026-10-14.md).

The isolated clock-correct Program Master has now also passed a 10-minute
concurrent audio/video qualification: 579,995 audio buffers with exact 1 ms
timestamp steps and zero discontinuities or hard corrections, while Program
video held 30.00 fps with zero drops. Audience-meeting mapping and measured
content-level lip sync remain separate gates.

The NDI pilot now has a documented loaded full-path result. The original
30-minute run with two continuous NDI receivers exposed a fixed-index scheduling
limitation under sustained CPU load. The adaptive common-window scheduler now
passes the loaded five-minute, 30-minute, and eight-hour gates with zero media,
synchronization, monitoring, or restart failures. The eight-hour gate made 479
TAKEs while routing 1,727,460 source grains and producing 863,730 synchronized
output decisions. A separate systemd-logind IPC cleanup issue was identified and
corrected on the dedicated test VM before the final gates. See the
[acceptance record](docs/ACCEPTANCE_2026-10-12.md) and
[NDI full-path findings](docs/NDI_FULL_PATH_FINDINGS.md) for exact measurements,
remediation, VM sizing, evidence hashes, and shared-memory lifecycle findings.

The first hardware gate completed on a 1080p30 v210 domain: the core created and
reused isolated stable PGM/PVW UUIDs, switched two live flows at frame
boundaries, and both outputs were consumed concurrently through the GStreamer
MXL source. A 900-frame run completed in 30.10 seconds at 29% of one CPU and
about 131 MiB maximum RSS. Invalid output grains observed during that run were
traced to an intermittent upstream guest flow; a continuously healthy clip flow
ran clean in isolated source tests.

The loopback HTTP transport has also been exercised against the live core. A
TAKE returned a command receipt at revision 0 and appeared in state at revision
1 on a later grain boundary, with PGM/PVW flow identities unchanged. A following
Preview command advanced revision 2. The 600-frame controlled run emitted all
1,200 expected output grains; its invalid grains were attributed to the same
intermittent guest input while the clip input remained clean.

## Planned milestones

1. **Program Master clock recovery:** candidate stable 48 kHz Program Audio and
   AFV are live; complete content-level lip-sync, recovered-alarm analysis, and
   staged acceptance before any distribution adapter claims the paired master.
2. **Meeting-to-meeting proof:** complete an audience-side receive gate with
   switched/composited video and clock-correct audio.
3. **Independent output nodes:** add NDI, SRT, WebRTC, recording, YouTube
   RTMPS, and scheduled DeckLink SDI/HDMI adapters without adding codecs or
   hardware dependencies to Core's hot path.
4. **Accepted NDI/MXL profile:** complete. One real NDI ingress plus one local
   synthetic v210 source passed eight loaded hours with two active NDI outputs.
5. **Lifecycle resilience:** complete for Core, ingress, and egress replacement.
   A controlled NDI-ingress interruption preserved both decoded NDI outputs,
   reopened the recreated MXL input, and recovered without process restarts.
6. **Two-source hardware acceptance:** repeat with two independent real sources
   into stable PGM/PVW on the Supermicro.
7. **Deployment profiles:** package Core-only, Program-only NDI, and full
   Program/Preview NDI profiles for fresh Linux hosts.

License: MIT.
