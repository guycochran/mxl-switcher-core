# MXL Switcher Core architecture

## Boundary

The Core is a small deterministic media function, not an application UI. It owns:

- stable Program and Preview MXL flow identities;
- the source registry and bus state;
- frame-boundary application of control commands;
- native `video/v210` grain routing and capture-timestamp preservation;
- state revisions suitable for tally and event consumers.

It does not own contribution decoding, graphics, layouts, multiview, replay,
encoding, monitoring, or operator presentation. Those are media functions or
skins connected through MXL and the control contract.

The repository ships a small reference Skin to prove that contract end to end.
It is static HTML/CSS/JavaScript served by the control listener and has no MXL
handles, media buffers, or video rendering. A production Skin can replace it
without changing the switching engine.

## Hot path

```text
MXL source reader ── SDK-owned v210 grain ──┐
                                            ├─ one write into stable PGM flow
frame boundary ── apply queued commands ────┤
                                            └─ one write into stable PVW flow
```

For ordinary cuts, each stable output reads the newest safe grain from its own
selected source ring. Program and Preview therefore do not depend on an overlap
between unrelated source-index timelines. The backend still observes the
cross-source common window for timing telemetry; a common index is reserved for
a future transition path that actually samples two pictures into one output.

The engine never interprets pixels and never performs a colorspace conversion.
The mock backend shares immutable payload ownership. The production adapter will
open an input grain and an output grain through the MXL Flow API, copy the opaque
payload once, preserve capture metadata, and commit it. PGM/PVW flows are created
once at startup and remain present across every cut.

## Timing contract

The production adapter supplies domain-aligned grain indexes using the MXL SDK
timing model. The engine refuses duplicate or decreasing indexes. Commands may be
accepted at any time, but become visible only at the start of `process(index)`.
That gives control clients a clean invariant: a state revision is never applied
halfway through an output frame.

## Failure contract

After an output has routed its first valid grain, transient missing source media
must preserve output cadence by repeating that output's last valid picture. The
adapter measures every held grain and must not recreate output flows. Before a
valid picture exists, unavailable media is committed invalid. SDK timing errors
and reader replacement remain visible in health telemetry. Five consecutive
unavailable frames raise one active alarm rather than one fault per frame; a
valid recovery clears the active alarm while retaining its cumulative event
counter.

## Live adapter

`MxlBackend` implements `MediaBackend` against `mxl/flow.h`:

1. creates two discrete v210 writers at startup;
2. keeps one reader handle per registered source;
3. selects a safe input index independently for each `(output, source)` CUT
   timeline and resets that timeline on a source change;
4. clamps recovery to a forward-only per-timeline floor, holding the last valid
   picture if that floor is no longer readable;
5. copies directly from the SDK-owned input payload into the SDK-owned output
   payload, avoiding an intermediate allocation or second copy;
6. seeds a hold-last cache immediately, refreshes it on a source change and
   about once per second, and uses that bounded-age picture for unavailable or
   repeated input so output timing advances without doubling steady-state v210
   memory traffic;
7. detects a missing or replaced selected shared-memory object and retires the
   selected reader cohort together, preventing one frame from mixing old and
   new bank generations while preserving PGM/PVW identities;
8. exposes independent selection, observed common-window gap, timeline-reset,
   recovery-clamp, and sustained-unavailable telemetry;
9. closes handles at shutdown without deleting/recreating flows during operation.

This is deliberately isolated from the engine so development and contract tests
do not require `/dev/shm`, Docker, or an installed SDK. `mxl-core-live` is the
bounded hardware harness; `--frames 0` runs until SIGINT/SIGTERM.
