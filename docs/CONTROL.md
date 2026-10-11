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
- `POST /api/mxl/v1/preview`
- `POST /api/mxl/v1/cut`
- `POST /api/mxl/v1/take`
- `GET /api/mxl/v1/events` (reserved for the event-stream transport)

The first six routes are covered by executable API and real-socket transport
tests. Mutation routes
return `202 Accepted` with the monotonic command id and accepted revision; the
command still takes effect only at the next media grain boundary.

The live harness binds `127.0.0.1:8080` by default. `--control-host` and
`--control-port` override that address. Binding a non-loopback address should be
done only behind an authenticated reverse proxy; authentication and TLS are not
implemented by the core transport.
