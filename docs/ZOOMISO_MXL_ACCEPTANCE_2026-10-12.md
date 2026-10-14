# ZoomISO MXL four-output acceptance — 2026-10-12

## Result

The candidate independent-CUT scheduler passed both a 10-minute gate and a
30-minute confirmation with four simultaneous ZoomISO MXL inputs. All four
inputs and the stable Program and Preview outputs negotiated native
1920x1080p30 progressive v210. Six concurrent rate probes averaged 30.00 fps
and reported zero dropped frames at the end of both gates.

This result validates the intended architectural boundary: an ordinary CUT
selects a safe grain independently from the chosen source ring. Unrelated input
index timelines no longer need a common readable index. The common-window
calculation remains observable telemetry and is reserved as a future primitive
for operations that actually combine two pictures, such as a transition.

## Test profile

- Four independent ZoomISO MXL input flows.
- One candidate Core process with stable Program and Preview outputs.
- Six simultaneous rate probes: four inputs, Program, and Preview.
- One concurrent content-uniqueness detector covering the same six flows.
- Fixed producer ring depth of six grains; the ZoomISO producer exposed no
  deeper-ring control.
- Live control sequence: TAKE, TAKE, TAKE, direct CUT to source 3, set Preview
  to source 4, TAKE, direct CUT to source 1, set Preview to source 2.
- The permanent ZoomISO process remained running with zero restarts, and each
  gate removed only its temporary validation containers during teardown.

## Clean gates

| Gate | Hold samples | Input/output rate | Probe drops | Common-window width | Source skew |
|---|---:|---:|---:|---:|---:|
| 10 minutes | 19 | 30.00 fps | 0 across all six probes | 4–5 grains | 0–1 grain |
| 30 minutes | 55 | 30.00 fps | 0 across all six probes | 4–5 grains | 0–1 grain |

The 30-minute confirmation ended with these six probe totals:

| Flow | Rendered grains | Dropped | Average rate |
|---|---:|---:|---:|
| Input 1 | 56,301 | 0 | 30.00 fps |
| Input 2 | 56,226 | 0 | 30.00 fps |
| Input 3 | 56,158 | 0 | 30.00 fps |
| Input 4 | 56,096 | 0 | 30.00 fps |
| Program | 55,949 | 0 | 30.00 fps |
| Preview | 55,850 | 0 | 30.00 fps |

The content detector continued to receive approximately 30 grains per second
from every flow. Content uniqueness varied with meeting motion, as expected,
but no flow entered `WEDGE`, `silent`, or `reader_error` state. This separates
content health from cross-source timeline alignment and confirms that the prior
window collapse was not masking a content wedge.

## Core health

After the 60-second stabilization baseline, both gates recorded zero movement
in every failure counter used by the acceptance harness:

- invalid, too-early, too-late, timeout, and reader-reopen;
- synchronization timeout, synchronization too-early, and synchronization
  too-late;
- no-common-window and selected-input regression;
- sustained-unavailable alarm events and active sustained-unavailable alarms.

The 30-minute Core snapshot ended at 106,059 routed grains and 112,058
independent selections. It observed 56,029 common-window selections for
telemetry, 14 source-timeline resets across startup and live control changes,
and zero recovery-floor clamps. Repeats, held grains, and skips remained
measured cadence behavior rather than failures; no output stalled and no
sustained-unavailable alarm became active.

## Resource envelope

Across the 55 samples in the 30-minute confirmation:

- ZoomISO used 268.58–839.35% CPU and 1.780–1.803 GiB RSS.
- The candidate Core used 30.76–73.30% CPU and 77.01–78.51 MiB RSS.
- The saturation streak remained zero throughout the gate.

The low ZoomISO CPU sample coincided with a live command interval; its normal
loaded samples were substantially higher. These measurements establish a
validated four-output operating point, not an eight-output capacity claim.

## Acceptance conclusion

The candidate fixes the reproduced failure signature: four healthy sources can
have non-overlapping input-index rings even while each source continues to
publish valid changing media. CUT routing is now independent per output/source
timeline, a cut resets that timeline, and same-timeline recovery cannot rewind
behind its forward-only floor. A one-shot sustained-unavailable alarm makes a
true persistent outage visible without treating ordinary repeats or observed
common-window gaps as immediate service failures.

## 2026-10-14 cadence-hold correction

A later 29.97/30 fps control exposed one remaining semantic gap. Independent
source selection correctly classified a repeated input index as cadence
behavior, but the live backend committed that valid repeated picture through
the same function used for an unavailable reader. Five consecutive cadence
holds could therefore increment the sustained-unavailable alarm even though
the source ring, payload, and output remained healthy.

The correction separates a valid cadence hold from a read failure:

- a repeated input index re-emits the last valid picture at the next monotonic
  output index without advancing the unavailable alarm;
- the cached picture must belong to the source currently selected on that
  output, preventing a failed first read after a CUT from leaking the prior
  source on air;
- missing readers, out-of-range reads, timeouts, invalid grains, and payload
  mismatches continue through the unavailable/hold/alarm path unchanged.

An isolated live candidate gate exercised 60 rapid TAKE/CUT/Preview commands
across four simultaneous 1080p30 inputs in two minutes. After stabilization it
processed 6,650 additional fresh routes and 616 cadence holds, while timeline
resets advanced from 2 to 71. Sustained-unavailable alarms remained 0→0; all
hard read-error and regression counters remained zero. Concurrent Program and
Preview probes both averaged 30.01 fps with zero drops. The production-facing
Core and supporting services remained running with zero restarts, and all
candidate containers were removed after the test.

The targeted gate was followed by staged 10-minute and 30-minute switching
confirmations using the same isolated candidate and four simultaneous inputs.
The harness issued a control command approximately every two seconds, mixing
TAKE, direct CUT, and Preview selections across all four sources.

| Gate | Commands | Fresh routes | Cadence holds | Timeline resets | Program / Preview |
|---|---:|---:|---:|---:|---:|
| 10 minutes | 295 | +35,031 | +969 | +345 | 18,619 / 18,595 grains |
| 30 minutes | 885 | +104,729 | +3,263 | +1,034 | 54,635 / 54,606 grains |

Both output probes averaged 30.00 fps and reported zero drops in both gates.
Common-window width remained 4–5 grains and observed source skew remained 0–1
grain. Every strict failure counter stayed unchanged at zero after
stabilization: invalid, too-early, too-late, timeout, reader-reopen,
synchronization timeout/early/late, no-common-window, selected-input
regression, sustained-unavailable alarms, and active sustained-unavailable
state. Each gate exited successfully, removed only its temporary candidate and
probe containers, and left the permanent ZoomISO, operator Core, and Program
Audio services running with zero restarts.

These longer confirmations close the cadence-hold regression qualification.
The candidate remains intentionally unmerged and has not replaced the
installed default Core; promotion should be a separate, reversible operational
decision.
