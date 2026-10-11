# MXL Switcher Core

[![CI](https://github.com/guycochran/mxl-switcher-core/actions/workflows/ci.yml/badge.svg)](https://github.com/guycochran/mxl-switcher-core/actions/workflows/ci.yml)

The first executable slice of a headless, skin-independent MXL production
switcher. It keeps MXL's native operating point—progressive v210 video—and gives
Program and Preview stable flow identities that do not disappear on a cut.

This is an executable vertical slice. The switching engine, lifecycle rules,
control sequencing, mock domain, demo, and invariant tests run on any C++20
machine. A production backend and bounded live harness now target the official
MXL Flow API; hardware compilation and verification remain to be completed on a
Linux MXL domain. The HTTP control service follows that verification.

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
  --rate-numerator 30000 --rate-denominator 1001
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
[control semantics](docs/CONTROL.md).

## Planned milestones

1. **Hardware verification:** compile the included `mxl/flow.h` backend against
   the installed SDK and run two live sources through isolated PGM/PVW flows.
2. **Control service:** the versioned JSON API plus an event stream; no media in
   the web process.
3. **Linux package:** one service, one config, health endpoint, graceful restart.
4. **Hardware acceptance:** two real v210 sources into stable PGM/PVW for eight
   hours on the Supermicro, including cuts under load.

License: MIT.
