# Control semantics (engine v0)

The `ControlApi` C++ adapter implements the versioned request/response contract.
`HttpServer` exposes it using a bounded, dependency-free HTTP/1.1 transport. The
transport remains a separate boundary and does not receive MXL handles or touch
video buffers.

| Operation | Accepted immediately | Applied | Result |
|---|---|---|---|
| `set_preview(source)` | yes | next grain boundary | PVW selects `source` |
| `cut(source)` | yes | next grain boundary | PGM selects `source`; PVW is unchanged |
| `take()` | yes | next grain boundary | PGM and PVW swap |

Each accepted command receives a monotonic command id and the state revision at
acceptance. Each applied command increments the state revision. Multiple commands
queued before one grain are applied in order as one boundary transaction.

The implemented request surface is:

- `GET /api/mxl/v1/meta`
- `GET /api/mxl/v1/state`
- `GET /api/mxl/v1/sources`
- `POST /api/mxl/v1/sources`
- `PUT /api/mxl/v1/sources/{id}`
- `DELETE /api/mxl/v1/sources/{id}`
- `GET /api/mxl/v1/health`
- `POST /api/mxl/v1/preview`
- `POST /api/mxl/v1/cut`
- `POST /api/mxl/v1/take`
- `GET /api/mxl/v1/events`

The same HTTP listener serves a dependency-free reference operator Skin at `/`
and `/skin`. It is an API client, not part of the media path: it uses ordinary
JSON requests for initial state and health, the SSE stream for frame-applied
state/tally revisions, and the mutation routes for Preview, Cut, and Take. The
Skin intentionally provides no video proxy; monitoring remains an independent
MXL or edge-I/O function.

The routes are covered by executable API and real-socket transport tests.
Mutation routes return `202 Accepted` with the monotonic command id and accepted
revision; the command still takes effect only at the next media grain boundary.

## Runtime source registration

Bolt-on media functions can join the running switcher without changing its
configuration or restarting Core. After creating a stable MXL video flow, the
extension registers its control-plane identity:

```http
POST /api/mxl/v1/sources
Content-Type: application/json

{
  "id": "playout-a",
  "flow_id": "00000000-0000-4000-8000-000000000001",
  "audio_flow_id": "00000000-0000-4000-8000-000000000002",
  "label": "Playout A",
  "kind": "playout",
  "capabilities": ["video", "audio", "playback", "health"]
}
```

`audio_flow_id` is optional metadata for an audio service or Skin; the v1 Core
continues to route video without decoding or mixing it. Registration is
idempotent when the complete descriptor is unchanged. Reusing an existing id or
video flow for a different descriptor is rejected.

An extension can remove an off-air source with
`DELETE /api/mxl/v1/sources/{id}`. Core rejects removal while that source is on
Program, Preview, or referenced by an accepted command. Catalog mutations
advance the state revision so SSE clients can refresh their source lists.

When a writer restarts and creates new flow UUIDs, replace the ephemeral media
binding without changing the stable source identity:

```http
PUT /api/mxl/v1/sources/camera-1
Content-Type: application/json

{
  "flow_id": "00000000-0000-4000-8000-000000000101",
  "audio_flow_id": "00000000-0000-4000-8000-000000000102"
}
```

`audio_flow_id` is required and may be `null`. The operation validates the new
video flow is not already assigned to another source, updates video and audio
under one Core lock, preserves Program/Preview selection, advances the catalog
revision exactly once, and is idempotent when both bindings are unchanged. Its
response includes the previous and resulting bindings for audit logging. A
frame already being routed keeps the descriptor it copied at frame start; the
next frame uses the new binding.

The control listener remains loopback-only by default. A deployment that makes
registration remotely reachable must place authentication and TLS in front of
Core.

`events` is a Server-Sent Events stream. It sends an initial `state` event and
then one event for each applied revision, supports `Last-Event-ID` reconnection,
and emits a keepalive comment while idle. An open event stream does not block
reads or commands on other connections. The server limits concurrent control
connections and shuts them down before the core exits.

`health` reports live aggregate and per-flow MXL routing counters. The aggregate
`held` counter records output grains that repeated the last valid picture while
input was unavailable or had not advanced. Statistics snapshots are synchronized
with the media thread, so a skin can poll health without racing counter updates.
`observed_common_window_empty` is timing telemetry and does not fault an
independent CUT. `no_common_window` is reserved for an operation that actually
requires a shared index, such as a transition. Sustained unavailability reports
both a cumulative alarm-event count and the number of currently active alarms;
only an active alarm degrades service health.

The live harness binds `127.0.0.1:8080` by default. `--control-host` and
`--control-port` override that address. Binding a non-loopback address should be
done only behind an authenticated reverse proxy; authentication and TLS are not
implemented by the core transport.
