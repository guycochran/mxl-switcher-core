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

## Flow-rotation rebind gate (harness)

`tools/mxl-atem-skin/mxl_rotation_gate.py` connects the existing pieces to
this sequence. It does not add new decision logic:

- **Candidate, probes, and safety:** the isolated candidate, probes, on-air
  refusals, invariants, evidence, and exact cleanup come from
  `mxl_recovery_observer.py`.
- **Matching:** identity matching uses `mxl_discovery_planner.py`, with writer
  attribution plus NMOS group and video/audio roles, never labels.
- **Writes:** every write goes through `mxl_rebind_apply.py`, with its revision
  gate, ownership checks, verification, and reverse-order rollback.

**The operator changes the declared output's protocol and changes it back.**
The harness never calls ZoomISO or changes a meeting, a mapping, or a
permanent service. It runs no daemon: one invocation observes one rotation.

```sh
python3 tools/mxl-atem-skin/mxl_rotation_gate.py plan \
  <the same isolated-candidate options as mxl_recovery_observer.py> \
  --easy-mxl-url http://127.0.0.1:<port> --easy-mxl-domain <domain> \
  --writer-container <ZoomISO container> --group <output NMOS group> \
  --video-role <role> --audio-role <role>
# no flag: plan only (read-only)
# --apply: run the gate as a rehearsal; it stops at the rebind plan and writes nothing
# --apply --confirm APPLY_REBINDS: the executor may write bindings
```

| Phase | What must hold | Bound |
| --- | --- | --- |
| Old pair | Exactly one active video/audio pair matches the mapping. It is `--selected-flow`, and one writer container owns both halves. With confirmation, the audio half is bound through the executor. | preflight |
| Content motion | The selected and control sources show changing pictures (`--min-change-fps`). This rejects cadence with a static picture, such as a meeting reset or an unassigned output. | `--motion-seconds` |
| Baseline | As in the recovery gate. The Core revision, the Program/Preview selection, and the output identities are recorded. | `--baseline-seconds` |
| Old pair inactive | EasyMXL reports both the old video and the old audio as no longer active. | `--interruption-timeout` |
| Replacement | The planner reports `rebind` (a rotated pair) or `noop` (the same pair continued). Both halves must be owned by the recorded writer container. These refuse immediately: `ambiguous`, `incompatible`, `create_required`, an ownership collision, a missing or undeterminable writer, or video and audio owned by different writers. | `--restoration-timeout` |
| Rebind | Refused if the Core revision or Program/Preview changed since baseline. Otherwise the executor applies the plan at the baseline revision. A failure rolls back and is recorded. Without confirmation the gate stops here, saving the plan and the dry-run preflight. | — |
| Verification | The stable source is bound to the discovered pair. Program/Preview and the output identities are unchanged. The lag is ≤ `input_delay_frames + 1`, fallbacks have stopped, and the alarm is clear. The candidate Program output shows changing content. | `--recovery-timeout` |
| Stable hold | No fallback movement and bounded lag. | `--stable-seconds` |

The must-not-move counters, probe drops and stalls, and the restart and
routing invariants fail the gate at any time.

Evidence includes:

- the old-pair, replacement, and executor plans and receipts;
- the samples;
- a redacted `public-summary.json`, with no flow IDs, writers, groups, source
  names, URLs, or paths.

Exit codes: 0 is PASS, 1 is FAIL or a rehearsal stop, 2 is refused with
nothing changed.

## Confirmed live acceptance

The completed harness was then exercised against a real, unused ZoomISO
output in three deliberate stages:

1. **Plan only:** uniquely matched the active video/audio pair to one writer
   using group and role metadata; confirmed the selected source was absent
   from the operator's Program, Preview, and Program Audio paths.
2. **Rehearsal:** observed MXL → non-MXL → MXL, proved that both UUIDs
   rotated, saved the executor preflight, and stopped before writing exactly
   as designed.
3. **Confirmed gate:** bound the old video/audio pair on the isolated
   candidate, observed another rotation, atomically rebound the stable source
   to the discovered replacement pair at the recorded revision, and verified
   recovery.

The confirmed result was **PASS**:

| Check | Result |
| --- | ---: |
| Stable source rebind | applied atomically |
| Program / Preview selection | unchanged |
| Candidate output identities | unchanged |
| Selected content before interruption | 29.10 changes/s |
| Control content before interruption | 29.99 changes/s |
| Program content after rebind | 29.81 changes/s |
| Program / Preview probe frames after baseline | 2,602 / 2,589 |
| Probe drops / stalls | 0 / 0 |
| Invalid / too early / too late / timeouts | 0 / 0 / 0 / 0 |
| Selected-input regressions | 0 |
| Final selected-input lag | 1 frame |
| Active sustained alarm | 0 |
| Permanent-service restarts | 0 |

The required stable hold completed, every exact candidate container and
candidate flow was removed, and the temporary discovery observer was removed
after its private evidence was saved. This closes the real ZoomISO
flow-identity-rotation acceptance for an isolated Core candidate.
