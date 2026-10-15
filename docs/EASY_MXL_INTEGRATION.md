# EasyMXL integration

EasyMXL and MXL Switcher Core solve different parts of an MXL production
system and fit together naturally:

- EasyMXL owns MXL domains, flow discovery, writer attribution, container
  lifecycle, dependencies, logs, terminals, and launch-time parameters.
- Core owns the authoritative Program/Preview state and deterministic routing
  between MXL flows.
- Separate media functions provide compositing, audio, graphics, monitoring,
  and destination-specific encoding.

The intended deployment is:

```text
EasyMXL
  +-- MXL Switcher Core
  +-- reference operator Skin
  +-- Program / Preview / multiview confidence encoders
  +-- Program Audio
  +-- NMOS and Companion control adapters
  `-- NDI / SRT / RTMP / recording output adapters
```

## Why discovery belongs outside Core

An MXL writer may create new flow UUIDs when it restarts or its outputs are
reconfigured. Labels alone cannot identify a source: two writers can publish
the same labels into one domain. EasyMXL already reports whether a flow is
active and, when host process information is available, which container owns
its writer.

A binding controller can consume EasyMXL's
`GET /api/domains/:name/flows` response and use all of the following fields:

- active status;
- writer container;
- NMOS group hint and role;
- media format and operating point;
- paired video and audio roles.

The controller then maps those ephemeral flow UUIDs to stable Core source IDs.
It must reject ambiguous matches. It must never guess which identically named
writer is on air.

## Atomic Core binding

Runtime source registration rejects an existing source ID whose descriptor
changes. The candidate Core therefore adds
`PUT /api/mxl/v1/sources/{id}` for atomic media rebinding with these properties:

1. validate the new video and optional audio descriptors first;
2. replace one stable source ID in one committed state transition;
3. preserve Program and Preview selection when that stable ID is selected;
4. increment the state/catalog revision once;
5. expose the old and new binding in the event and audit result;
6. remain idempotent when the binding is unchanged;
7. reject format or operating-point mismatches without changing state.

The operation is unit- and control-contract tested, but discovery integration
must remain read-only until the candidate passes live restart/rebinding
acceptance with Program and Preview active.

## Catalog entry

[`packaging/easy-mxl/mxl-switcher-core.catalog.json`](../packaging/easy-mxl/mxl-switcher-core.catalog.json)
is a schema-valid contract for a locally built runtime image. It deliberately
uses EasyMXL's `local` image policy because this repository does not yet publish
a production runtime image.

The operator supplies a Core configuration file as a read-only host mount. The
configuration must bind the control listener to `0.0.0.0` inside the container
when the port is published by Docker. Do not expose that port beyond a trusted
production network without authentication and TLS at a reverse proxy.

The catalog entry becoming valid JSON does **not** make the deployment
production-ready. Promotion also requires:

- a versioned runtime image;
- confirmed redistribution terms for the MXL SDK/runtime;
- container health checking;
- immutable image tags or digests;
- restart and rollback acceptance;
- an atomic source-binding API;
- clean Program audio and destination A/V measurements.

## Suggested packaging sequence

1. Build and load a versioned local image on an EasyMXL host.
2. Add this catalog file through `EASY_MXL_CATALOG`.
3. Launch Core against a test domain and configuration.
4. Verify state, health, routing, and restart recovery.
5. Add the read-only discovery planner.
6. Qualify atomic rebinding on a candidate Core.
7. Add the Skin, confidence encoders, and output adapters as separate catalog
   entries rather than combining the entire production stack into one image.
