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

## Read-only discovery planner

`tools/mxl_discovery_planner.py` turns EasyMXL discovery into a deterministic
reconciliation plan for Core. **It only reads.** It issues `GET` requests at
most, has no apply mode, and never sends the `PUT` requests it proposes.
Applying a plan is a separate phase that must be explicitly enabled after the
candidate Core passes live restart and rebinding acceptance.

Trust boundary:

- **EasyMXL** is the source of truth for discovery and lifecycle: which flows
  exist, whether a writer holds them (`active`/`inactive`/`stale`), and which
  container owns the writer.
- **Core** is the source of truth for switching state: which flow each stable
  source ID is bound to, and Program/Preview.
- **The planner** compares the two and reports. It never changes either.

### Inputs

1. **EasyMXL flow inventory.** Either a saved
   `GET /api/domains/{domain}/flows` response (`--inventory`), or a live
   read-only request (`--easy-mxl-url` with `--domain`). The token is read from
   an environment variable (`--token-env`, default `EASY_MXL_TOKEN`) or given
   explicitly with `--token`. It is sent only to EasyMXL as a bearer header and
   is never printed, logged, or written to the plan.
2. **Core source snapshot.** Either a saved `GET /api/mxl/v1/sources` response
   (`--core-snapshot`), or a live read-only request (`--core-url`).
3. **Declarative stable-source mapping** (see
   [the example](../packaging/easy-mxl/discovery-mapping.example.json)):

```json
{
  "version": 1,
  "domain": "example-domain",
  "defaults": {
    "video": {"width": 1920, "height": 1080, "rate": "30/1", "media_type": "video/v210"},
    "audio": {"sample_rate": 48000, "channels": 2, "media_type": "audio/float32"}
  },
  "sources": [
    {
      "id": "cam-1",
      "match": {
        "writer_container": "contrib-a",
        "group": "Camera Output 1",
        "video_role": "Video 1",
        "audio_role": "Audio 1"
      }
    },
    {
      "id": "playout",
      "match": {"writer_container": "playout-a", "group": "Playout", "video_role": "Video 1", "audio_role": "Audio 1"},
      "audio": {"required": false}
    }
  ]
}
```

### Matching rules

- A flow matches a stable source only when **all** of the following hold:
  - its writer container name, NMOS group and role equal the rule;
  - its media format, frame size, rate and media type equal the expected
    operating point.

  Labels are never used. Two writers can publish identical labels.
- Only `active` flows are bound by default. Inactive and stale versions of the
  same output are listed in the plan as diagnostics.
- Audio is paired only from the same writer container and group as the video.
  Missing audio blocks the source unless the mapping sets
  `"audio": {"required": false}`, in which case the source binds with
  `audio_flow_id: null`.
- Two mapping entries with the same writer, group and video role are rejected
  as a configuration error.

### Statuses

| Status | Meaning |
| --- | --- |
| `noop` | Core already holds the desired binding. |
| `rebind` | The writer regenerated its flow UUIDs. The stable source ID stays the same, and a `PUT /api/mxl/v1/sources/{id}` is proposed. |
| `create_required` | The stable source is not registered in Core. |
| `missing` | No active compatible flow, or required audio is absent. |
| `ambiguous` | More than one valid candidate; a duplicate video assignment; a flow already bound to another Core source; or a rebinding cycle such as a swap. The operator decides. |
| `incompatible` | Matching flows exist, but the format, resolution or cadence differ, or the flow differs from Core's operating point. |

Proposed requests are ordered so that a flow is released before another source
claims it. Every collection is sorted, and the JSON is rendered with sorted
keys, so identical inputs give byte-identical plans. The exit status is `0`
when every source is `noop` or `rebind`, `1` when any source needs operator
action, and `2` for an input or configuration error.

```sh
export EASY_MXL_TOKEN=...   # never committed
python3 tools/mxl_discovery_planner.py \
  --mapping packaging/easy-mxl/discovery-mapping.example.json \
  --easy-mxl-url http://127.0.0.1:9700 --domain example-domain \
  --core-url http://127.0.0.1:18085 \
  --output plan.json
```

EasyMXL listens on port 9700 by default. That is also the reference operator
Skin's default port, so run the Skin on another port (for example 9710) on a
shared host.

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
5. Add the read-only discovery planner (`tools/mxl_discovery_planner.py`).
6. Qualify atomic rebinding on a candidate Core.
7. Add the Skin, confidence encoders, and output adapters as separate catalog
   entries rather than combining the entire production stack into one image.
