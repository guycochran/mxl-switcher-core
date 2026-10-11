# MXL Switcher Core

[![CI](https://github.com/guycochran/mxl-switcher-core/actions/workflows/ci.yml/badge.svg)](https://github.com/guycochran/mxl-switcher-core/actions/workflows/ci.yml)

The first executable slice of a headless, skin-independent MXL production
switcher. It keeps MXL's native operating point—progressive v210 video—and gives
Program and Preview stable flow identities that do not disappear on a cut.

This is an executable vertical slice. The switching engine, lifecycle rules,
control sequencing, mock domain, demo, and invariant tests run on any C++20
machine. The production backend and bounded live harness have now been compiled
and exercised against the official MXL Flow API on a Linux MXL domain. The HTTP
control service is the next implementation slice.

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
- dependency-free versioned control adapter for state, sources, Preview, Cut,
  and Take, with contract tests preserving frame-boundary command semantics.
- bounded HTTP/1.1 control transport, loopback-only by default, with real-socket
  tests and no access to MXL handles or media buffers.
- reconnectable Server-Sent Events for frame-applied state/tally revisions and a
  thread-safe live health endpoint with per-input routing counters.

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

The NDI pilot now has a documented loaded full-path result. A five-minute
isolated gate passed with zero media errors, while a 30-minute run with two
continuous NDI receivers exposed a fixed-index scheduling limitation under
sustained CPU load. All containers stayed running, upstream inputs remained
valid, and memory stayed bounded. See the
[NDI full-path acceptance findings](docs/NDI_FULL_PATH_FINDINGS.md) for exact
measurements and the adaptive common-window remediation plan.

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

1. **Adaptive live scheduling:** select input grains from the common readable
   window of all active sources and pass the loaded five- and 30-minute gates.
2. **Hardware acceptance:** two real v210 sources into stable PGM/PVW for eight
   hours on the Supermicro, including cuts and two active NDI outputs under
   load.

License: MIT.
