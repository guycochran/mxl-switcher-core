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

### Flow ownership

Every flow, **video or audio**, has at most one owner. The planner fails closed
and never chooses a winner by ordering or label:

- Two stable sources claiming the same active audio flow both become
  `ambiguous` (`duplicate_audio_assignment`). This happens, for example, when
  two video roles in one group are both mapped to that group's single audio
  role.
- A source claiming an audio or video flow that Core already binds to another
  source becomes `ambiguous`
  (`audio_flow_bound_to_other_source:<id>` or `video_flow_bound_to_other_source:<id>`).
  The exception is when that other source is rebinding away in the same plan.
  Then the release is ordered before the claim.
- If the release a claim relies on is itself blocked, the claim is blocked too.
- Only active flows can be desired, so a stale or inactive copy of an audio
  output never reserves ownership. It appears only in diagnostics.
- `audio_flow_id: null` (optional audio absent) owns nothing, so any number of
  video-only sources may carry it.

For example, with synthetic identities, writer `contrib-a` publishes group
`Camera Output 1` with roles `Video 1`, `Video 2` and one `Audio 1`. A mapping
that gives `cam-wide` (`Video 1`) and `cam-tight` (`Video 2`) the same
`audio_role: "Audio 1"` yields two `ambiguous` sources and no requests. The
operator must assign the audio to one of them, or mark audio optional on the
other.

### Statuses

| Status | Meaning |
| --- | --- |
| `noop` | Core already holds the desired binding. |
| `rebind` | The writer regenerated its flow UUIDs. The stable source ID stays the same, and a `PUT /api/mxl/v1/sources/{id}` is proposed. |
| `create_required` | The stable source is not registered in Core. |
| `missing` | No active compatible flow, or required audio is absent. |
| `ambiguous` | More than one valid candidate; a duplicate video or audio assignment; a flow already bound to another Core source; or a rebinding cycle such as a swap. The operator decides. |
| `incompatible` | Matching flows exist, but the format, resolution or cadence differ, or the flow differs from Core's operating point. |

Proposed requests are ordered so that a flow, video or audio, is released
before another source claims it. Every collection is sorted, and the JSON is rendered with sorted
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

### Read-only six-source qualification (2026-10-15)

The planner was exercised against a live six-source ZoomISO bank without
changing Core or either writer. EasyMXL v1.0.0 ran as a temporary loopback-only
service with a token. The existing MXL domain was mounted read-only. EasyMXL
found 31 flows, classified 27 as active, and attributed the two ZoomISO writers
separately even though they published identical labels, groups, and roles.

The planner returned exit 0 with this result:

| Status | Count |
| --- | ---: |
| `rebind` | 6 |
| `noop` | 0 |
| `missing` | 0 |
| `ambiguous` | 0 |
| `incompatible` | 0 |
| `create_required` | 0 |

All six current Core video flow IDs already matched the desired active flows
from the selected writer. Each proposed request preserved that video flow and
added the audio flow from the same writer, group, and output number. Nothing
was applied. The temporary service reported zero restarts and was removed with
its credential and build directory after the plan was saved.

ZoomISO stores its MXL domain as the root of a Docker volume, while EasyMXL
normally scans child directories of a domain root. For read-only discovery,
mount the volume's domain directory as a named child such as
`/domains/zoomiso:ro`, then set `EASY_MXL_DOMAIN_ROOT=/domains`. Do not point
EasyMXL at the volume's parent and assume `_data` will be accepted as a domain
name; domain names must start with an alphanumeric character.

### Isolated atomic rebind qualification (2026-10-15)

The next gate was run against a freshly built, isolated candidate Core. Its
two stable source IDs intentionally started with stale flow UUIDs to reproduce
a publisher restart. Preview was rebound first, followed by Program, using the
planner-selected video/audio pairs. Both requests returned `changed: true` and
advanced the Core revision exactly once.

Across the recovery:

- stable source names and Program/Preview assignments did not change;
- Program and Preview output flow UUIDs did not change;
- both source bindings changed atomically to the new video/audio pairs;
- the output index resumed at approximately 30 fps;
- the container remained running with zero restarts;
- invalid, early, late, timeout, reader-reopen, sync, no-common-window,
  regression, and active sustained-unavailable counters did not move after
  stabilization;
- resident memory stayed near 77 MiB. CPU was approximately one older server
  core while producing independent 1080p30 Program and Preview outputs.

The deliberately stale startup accumulated unavailable/invalid diagnostics
before both sources were restored. Those values froze after recovery and the
active sustained-unavailable state cleared. Cadence repeats and skips continued
to be reported as measurements; neither output stalled or regressed.

An older, already-installed candidate returned HTTP 405 for the source update
endpoint. This is the expected compatibility boundary: discovery and planning
can be used with older builds, but atomic live rebinding requires a Core build
that includes `PUT /api/mxl/v1/sources/{id}`. The operator Core was not touched.

This qualifies the manual discovery-to-rebind path. An automatic apply mode
remains out of scope until it has explicit operator approval, authentication,
preflight state checks, and rollback semantics.

### Real ZoomISO identity rotation (2026-10-15)

A controlled live test confirmed why this reconciliation path is required.
Changing one unused ZoomISO output from MXL to NDI stopped its existing MXL
video/audio pair. Changing the same numbered output back to MXL preserved its
stable ZoomISO source identity but created new video and audio flow UUIDs.

The isolated Core remained healthy, its independent control source continued
at 30.02 fps, and the candidate Program output continued at 29.99 fps through
hold/fallback with zero post-baseline probe drops or stalls. Hard error and
regression counters had zero delta. The static old binding could not see the
replacement flow, so the run correctly did not claim recovery and was stopped
before the audience-return gate.

This result promoted real ZoomISO UUID rotation from an assumption to an
observed lifecycle contract. The follow-up live gate used writer-specific
discovery to plan and atomically apply the new pair to the existing stable Core
source ID. Program recovered moving content near 30 fps, completed its stable
hold, and recorded zero probe drops, stalls, hard-counter movement, or
permanent-service restarts. The remaining lifecycle step is a bounded,
authenticated always-on observer built from the accepted planner and executor,
not new matching logic. Full aggregate evidence is recorded in
[Real ZoomISO output identity rotation](REAL_ZOOMISO_FLOW_ROTATION_2026-10-15.md).

### Core audio-ownership qualification (2026-10-15)

Core now enforces the planner/executor single-owner rule independently at its
registration and rebind APIs. The exact committed candidate was built in an
isolated worktree and exercised against six active 1080p30 sources without
replacing the installed operator Core.

The live gate established one audio owner, then attempted to assign that audio
flow to a second source. Core returned HTTP 400 and preserved the second
source's null audio binding and the current revision. A separate runtime source
registration attempting the same collision also returned HTTP 400 without
changing state. Null audio remained valid for independent video-only sources.

After the negative tests, six distinct writer-specific video/audio pairs were
applied in Preview, off-air, then Program order. Every source was routed through
Program and the stable Program/Preview identities were preserved. During the
following observation:

- the candidate stayed healthy with zero restarts;
- 612 Program/Preview routes completed in a 10-second sample;
- invalid, early, late, timeout, reader-reopen, synchronization,
  no-common-window, regression, and sustained-unavailable counters had zero
  delta;
- current common-window width was five frames and current source skew was zero;
- sampled CPU was 63-67% of one older server core and working memory was about
  12 MiB.

The shared domain contained stale same-label definitions from another writer.
The gate therefore used writer-specific discovery results rather than labels
alone. The temporary candidate, image, outputs, worktree, and private config
were removed afterward. The operator Core state and source catalog were
byte-identical before and after the gate, and every permanent service remained
running with zero restarts.

### Guarded operator executor

`tools/mxl_rebind_apply.py` provides the next deliberately narrow step. It is
not a daemon and does not discover or decide anything. It consumes a saved,
unblocked planner document and runs as a dry preflight unless the operator
supplies all three write gates: `--apply`, the exact live Core revision, and
the literal confirmation `APPLY_REBINDS`.

The executor accepts only a loopback Core URL, rejects a stale source snapshot,
verifies every request against the plan, applies Preview and other off-air
sources before Program when there are no ownership dependencies, and verifies
the live binding after every receipt. If a request or verification fails, it
rolls completed requests back in reverse order. A dependent plan that would
require releasing Program before another source is rejected for manual
handling.

The executor enforces flow ownership independently of the planner, so a
malformed or hand-edited plan cannot bypass it. It rejects a plan whose
resulting bindings give any video or audio flow two owners. It then simulates
the ordered apply against live Core, including sources outside the plan, and
refuses if any intermediate step would. Audio transfers between sources
must release before they claim. The Skin's Source Recovery preflight and apply
both run through this check.

```sh
# Safe default: preflight only. It prints the required revision and order.
python3 tools/mxl_rebind_apply.py \
  --plan plan.json --core-url http://127.0.0.1:18085

# Explicit operator action using the revision printed by preflight.
python3 tools/mxl_rebind_apply.py \
  --plan plan.json --core-url http://127.0.0.1:18085 \
  --apply --expect-revision 42 --confirm APPLY_REBINDS \
  --output rebind-receipt.json
```

Keep the Core control listener on loopback or behind an authenticated control
proxy. A future UI may wrap this executor, but must preserve the preflight,
confirmation, ordering, verification, and rollback gates.

The reference Skin's `/health` page now provides that guarded wrapper. Its
Source Recovery panel scans and displays the plan without exposing the EasyMXL
token, requires a separate preflight, and keeps the reconnect control disabled
unless the server was explicitly started with `--enable-source-reconnect`.
The operator must type `APPLY_REBINDS`; the server then regenerates the plan
and requires both its hash and Core revision to match the reviewed values.

## Catalog entry

[`packaging/easy-mxl/mxl-switcher-core.catalog.json`](../packaging/easy-mxl/mxl-switcher-core.catalog.json)
is a schema-valid contract for a locally built runtime image. It deliberately
uses EasyMXL's `local` image policy because this repository does not yet publish
a production runtime image.

The operator supplies a Core configuration file as a read-only host mount. The
configuration must bind the control listener to `0.0.0.0` inside the container
when the port is published by Docker. Do not expose that port beyond a trusted
production network without authentication and TLS at a reverse proxy.

Build the local candidate from a host that already has the licensed MXL lab
base image and SDK runtime:

```sh
packaging/easy-mxl/build-local-image.sh 0.2.0-candidate          # dry run
packaging/easy-mxl/build-local-image.sh 0.2.0-candidate --apply  # build
```

The script now delegates to the immutable deployment bundle. It tags the image
`<semver>.g<12-hex revision>` from a clean commit only, verifies its OCI
labels, and writes a release manifest and a pinned catalog entry. See
[Immutable Core deployment bundle](DEPLOYMENT_BUNDLE.md) for preflight,
launch, exact cleanup, and rollback. The earlier qualified image
`0.2.0-candidate.a89fe17` predates this naming. The alias
`mxl-switcher-core:0.2.0-candidate` is a mutable tag, and production launches
refuse it.

The multi-stage `Dockerfile.runtime` compiles the current checkout, labels the
result with its Git revision, installs the acceptance definitions and guarded
discovery/rebind tools, and adds a Core HTTP health check. It deliberately uses
the local `mxl-lab:latest` base: MXL SDK redistribution terms must be resolved
before publishing any runtime image. A successful local build or health check
does not authorize registry publication.

### Versioned local image qualification (2026-10-15)

Candidate `0.2.0-candidate.a89fe17` was built from exact revision `a89fe17`
using the licensed local base image, then run as an isolated container against
two live 1080p30 sources. The first start failed closed because a test
configuration supplied output UUIDs that did not match the bundled output
definitions. The corrected gate used the definitions' declared acceptance
UUIDs and a loopback port mapping; the operator Core was never replaced.

The corrected candidate became Docker-healthy with zero restarts. During the
qualification window both stable outputs advanced at approximately 30 fps,
resident memory stayed near 77 MiB, and invalid, early, late, timeout,
reader-reopen, synchronization, no-common-window, regression, and sustained
alarm counters remained zero. Cadence holds/skips and a small number of
observed-window/copy-lap measurements were retained as load telemetry; neither
output stalled or regressed.

The isolated container and exact temporary output flows were removed after the
gate. Permanent ZoomISO, operator Core, Program Audio, and NMOS services stayed
running with zero restarts. The qualified image remains installed locally as
both `mxl-switcher-core:0.2.0-candidate.a89fe17` and the catalog alias
`mxl-switcher-core:0.2.0-candidate`. It has not been pushed to a registry and
has not replaced the operator Core.

### Six-source guarded recovery qualification (2026-10-15)

The versioned candidate, read-only planner, guarded executor, and EIC Source
Recovery panel were exercised together against a live six-source bank. The
candidate ran in parallel with the operator Core and used separate loopback
control and output identities. The operator Core and its Skin were not changed.

The unarmed EIC panel discovered six deterministic video/audio pairs, reported
six reconnectable stable sources with no missing, ambiguous, incompatible, or
registration-required results, and produced a Preview/off-air-first, Program-last
preflight. Reconnect remained unavailable on the unarmed panel. A second panel
bound only to the isolated candidate was explicitly armed for the gate; it
regenerated the plan, verified the plan hash and starting revision, and applied
all six bindings in the preflight order. The candidate revision advanced exactly
once per source. A subsequent scan reported all six sources `noop`/current and
disabled the reconnect action because no work remained.

All six sources were then routed through Program. Program and Preview output
identities remained stable, output motion continued, the container stayed healthy
with zero restarts, and invalid, early, late, timeout, reader-reopen,
synchronization, no-common-window, regression, and sustained-alarm counters had
zero deltas. A short post-recovery observation also retained zero deltas in those
hard counters. Sampled candidate CPU ranged from roughly 41% to 113% of one older
server core; sampled container working memory remained bounded between roughly
12 and 23 MiB.

This is a functional qualification of the guarded six-source recovery workflow,
not permission to replace the operator Core. Promotion still requires a planned
maintenance window, operator-stack integration checks, rollback rehearsal, and a
longer duration gate.

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

1. Build and load the versioned local candidate image on an EasyMXL host.
2. Add this catalog file through `EASY_MXL_CATALOG`.
3. Launch Core against a test domain and configuration.
4. Verify state, health, routing, and restart recovery.
5. Add the read-only discovery planner (`tools/mxl_discovery_planner.py`).
6. Qualify atomic rebinding on a candidate Core.
7. Add the Skin, confidence encoders, and output adapters as separate catalog
   entries rather than combining the entire production stack into one image.
