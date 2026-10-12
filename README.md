# MXL Switcher Core

[![CI](https://github.com/guycochran/mxl-switcher-core/actions/workflows/ci.yml/badge.svg)](https://github.com/guycochran/mxl-switcher-core/actions/workflows/ci.yml)

The first executable slice of a headless, skin-independent MXL production
switcher. It keeps MXL's native operating point—progressive v210 video—and gives
Program and Preview stable flow identities that do not disappear on a cut.

This is an executable vertical slice. The switching engine, lifecycle rules,
control sequencing, mock domain, demo, and invariant tests run on any C++20
machine. The production backend and bounded live harness have now been compiled
and exercised against the official MXL Flow API on a Linux MXL domain. A
versioned HTTP/SSE control service and dependency-free reference operator Skin
are included.

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
- dependency-free versioned control adapter for state, sources, Preview, Cut,
  and Take, with contract tests preserving frame-boundary command semantics.
- bounded HTTP/1.1 control transport, loopback-only by default, with real-socket
  tests and no access to MXL handles or media buffers.
- reconnectable Server-Sent Events for frame-applied state/tally revisions and a
  thread-safe live health endpoint with per-input routing counters.
- a responsive reference Skin at `/` (also `/skin`) with Program and Preview
  source buses, Cut, Preview, Take, tally, timing, and error telemetry.

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

1. **Accepted NDI/MXL profile:** complete. One real NDI ingress plus one local
   synthetic v210 source passed eight loaded hours with two active NDI outputs.
2. **Lifecycle resilience:** complete for Core, ingress, and egress replacement.
   A controlled NDI-ingress interruption preserved both decoded NDI outputs,
   reopened the recreated MXL input, and recovered without process restarts.
3. **Two-source hardware acceptance:** repeat with two independent real sources
   into stable PGM/PVW on the Supermicro.
4. **Deployment profiles:** package Core-only, Program-only NDI, and full
   Program/Preview NDI profiles for fresh Linux hosts.

License: MIT.
