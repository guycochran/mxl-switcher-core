# Control semantics (engine v0)

The current C++ API is the boundary that the HTTP service will wrap.

| Operation | Accepted immediately | Applied | Result |
|---|---|---|---|
| `set_preview(source)` | yes | next grain boundary | PVW selects `source` |
| `cut(source)` | yes | next grain boundary | PGM selects `source`; PVW is unchanged |
| `take()` | yes | next grain boundary | PGM and PVW swap |

Each accepted command receives a monotonic command id and the state revision at
acceptance. Each applied command increments the state revision. Multiple commands
queued before one grain are applied in order as one boundary transaction.

The planned HTTP surface remains compatible with the existing switcher proposal:

- `GET /api/mxl/v1/meta`
- `GET /api/mxl/v1/state`
- `GET /api/mxl/v1/sources`
- `POST /api/mxl/v1/preview`
- `POST /api/mxl/v1/cut`
- `POST /api/mxl/v1/take`
- `GET /api/mxl/v1/events`

The HTTP process is a control adapter. It will not touch video buffers.

