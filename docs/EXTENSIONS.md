# Core and optional extensions

## Product boundary

MXL Switcher Core owns only the timing-critical functions that every workflow
needs:

- stable Program and Preview MXL flow identities;
- frame-boundary routing and source lifecycle handling;
- health, tally, state, and a versioned control API;
- contracts that let other processes consume or publish MXL flows.

An extension is a separate process. It may ingest a transport into MXL, consume
an MXL output, provide an operator surface, or store and replay media. An
extension failure must not change Core's flow identities, block its routing
loop, or interrupt another extension.

```text
transport-specific input                 destination-specific output
NDI / SRT / SDI / ZoomISO               NDI / SRT / SDI / RTMPS / TAMS
           |                                         ^
           v                                         |
      input adapter -> MXL -> Switcher Core -> MXL -> output adapter
                                |
                                +---- HTTP/SSE ----> any Skin/controller
```

## Repository boundary

[`mxl-switcher-core`](https://github.com/guycochran/mxl-switcher-core) contains
the engine, API, schemas, acceptance evidence, and small reference operator
surfaces. It must build and test without proprietary transport SDKs, cloud
accounts, storage services, streaming credentials, or video hardware.

[`mxl-switcher-extensions`](https://github.com/guycochran/mxl-switcher-extensions)
contains optional input, output, recording, and deployment integrations. Its
packages declare the Core API they require and release independently.

Long-lived Git branches are not product editions. `main` is the integrated,
reviewed line in each repository; short feature branches merge back into it.
Corporate, online, archive, and full-demo editions are deployment profiles that
pin released components.

## Extension contract

Every extension declares:

- name, version, kind, and compatible Core API range;
- MXL flow roles it reads or publishes;
- required and optional capabilities;
- readiness separately from process liveness;
- start, stop, and restart behavior;
- bounded buffering and resource policy;
- telemetry and destination-specific alarms;
- external secret references, never returned by an API or committed to Git.

The common lifecycle is:

```text
disabled -> starting -> ready -> running
                              -> degraded -> running
                              -> failed
```

### Source-producing extensions

A source-producing extension creates its stable MXL flow first, then registers
the source with Core using `POST /api/mxl/v1/sources`. The descriptor separates
identity from implementation:

- `id`: stable control identity used by Program, Preview, Skins, and controllers;
- `flow_id`: MXL video flow routed by Core without decoding;
- `audio_flow_id`: optional paired audio flow for an audio service;
- `label`: operator-facing name;
- `kind`: camera, guest, playout, graphics, generator, layout, or another
  deployment-defined class;
- `capabilities`: discoverable controls such as video, audio, playback,
  graphics, timecode, or health.

The call is idempotent, so an extension may safely register after every process
start. Core rejects conflicting identities and prevents removal while a source
is on Program or Preview. Skins build their buses from the live catalog instead
of assuming fixed camera slots. The NMOS bridge mirrors source kind and
capabilities into IS-04 tags and withdraws stale Sender, Flow, and Source
resources from a configured Registry when an off-air extension is removed.
Companion likewise rebuilds action choices, feedback choices, and presets from
the live catalog. See [Control semantics](CONTROL.md) for the wire contract.

Stopping or restarting an extension must not recreate its upstream Program
Master flow. An input extension should publish a stable MXL identity through a
transport reconnect whenever the underlying MXL implementation permits it.

## Compatibility and releases

- Core releases use semantic versions and expose a versioned API.
- Extension packages version independently and declare `requires_core_api`.
- Deployment profiles pin tested versions or immutable container digests.
- Adding a destination never requires a Core feature branch.
- Proprietary SDKs and redistributable binaries remain outside Core.

See [Program master and output-node plan](OUTPUT_ARCHITECTURE.md) for media
timing and acceptance requirements.
