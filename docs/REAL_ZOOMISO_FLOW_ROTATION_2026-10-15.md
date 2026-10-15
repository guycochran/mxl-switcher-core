# Real ZoomISO output identity rotation

Date: 2026-10-15

## Result

A controlled, isolated test changed one unused ZoomISO output from MXL to NDI
and then back to MXL while a separate ZoomISO output remained the control. The
test established an important lifecycle boundary:

- switching away from MXL stops the existing video and audio writers;
- switching back to MXL creates new video and audio flow UUIDs;
- the stable ZoomISO output number and source identity remain the same;
- a Core source statically bound to the old flow UUID correctly holds/falls
  back, but cannot discover the replacement UUID by itself.

This is not a media-reader recovery failure. It is the expected boundary
between Core's stable logical source ID and the discovery/lifecycle controller
that resolves an ephemeral writer flow.

## Measurements

Before interruption, a content-level preflight confirmed changing 1080p30
picture on both the selected and control outputs. During the controlled gap:

| Measurement | Result |
|---|---:|
| Isolated Program probe | 29.99 fps |
| Isolated Preview/control probe | 30.02 fps |
| Probe drops after baseline | 0 / 0 |
| Probe stalls after baseline | 0 / 0 |
| Core invalid / too-early / too-late / timeout deltas | 0 / 0 / 0 / 0 |
| Selected-input regressions | 0 |
| Permanent-service restarts | 0 |

The gate was deliberately recorded as **FAIL / sequence incomplete** because
the restored MXL writer had a new identity and no discovery-driven rebind was
performed. All temporary candidate containers and candidate output flows were
removed; permanent services were unchanged.

## Required production behavior

The production path must treat ZoomISO's output number (plus writer ownership
and NMOS group/role metadata) as the stable source identity, and its MXL flow
UUIDs as replaceable bindings:

1. discover the replacement video/audio pair without using labels alone;
2. reject missing, inactive, duplicate, or ambiguous writer matches;
3. generate a deterministic reconciliation plan;
4. atomically rebind the existing Core source ID with
   `PUT /api/mxl/v1/sources/{id}`;
5. preserve Program/Preview selection and stable Core output identities;
6. verify changing content, bounded lag, clean audio/video cadence, and a
   stable post-rebind hold.

The repository already contains the read-only discovery planner, guarded
rebind executor, and atomic Core source-binding API. The next acceptance gate
must connect those pieces to this real ZoomISO identity-rotation sequence.

## Operational lesson

An advancing MXL head alone does not prove that live meeting content is
present: ZoomISO can continue publishing cadence while an output is unassigned
or a meeting is between scheduled restarts. Future gates must require a
content-motion preflight (or an equivalent explicit live-content assertion)
before taking the baseline.
