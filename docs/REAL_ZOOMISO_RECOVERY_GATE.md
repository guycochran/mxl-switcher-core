# Real ZoomISO Writer-Recovery Gate

This gate verifies that an isolated candidate Core preserves Program and
Preview cadence when one real ZoomISO MXL output disappears and returns under
the same flow identity. It is intentionally separate from the operator Core.

The gate changes one live ZoomISO output for a short, declared interval. Run it
only with an operator present and explicit approval for that interruption.

## Safety boundary

The gate must:

- use unique candidate container names, output flow identities, control port,
  and evidence directory;
- read the existing ZoomISO source flow identities without changing operator
  Core mappings;
- leave the meeting, networking, VM resources, and permanent services alone;
- record the exact selected ZoomISO output and its original assignment before
  any change;
- prove read-only that the selected output is not the operator Core's current
  Program source and is not an active Program Audio channel; also exclude
  operator Preview when it is being used as an operator monitor;
- change only that one output, then restore its original assignment;
- remove only resources bearing the gate's exact run identity.

Refuse to start if any candidate name, port, or output identity collides; the
selected output's original assignment cannot be recorded; either output probe
cannot establish a stable baseline; any permanent service has restarted; or a
second writer already owns a candidate output. Also refuse if the selected
source is on the operator Program bus, active in Program Audio, or on an
operator Preview bus that must remain uninterrupted.

## Operator sequence

1. Confirm the meeting and source content are active. Record the selected
   ZoomISO output's original participant assignment and flow identity.
2. Start the isolated candidate and independent Program and Preview probes.
   Put the selected output on the candidate Program bus and a different,
   untouched ZoomISO source on candidate Preview as the control. Confirm both
   assignments from the candidate `/state` response before taking a baseline.
3. Allow startup measurements to settle, then capture a counter baseline only
   after both probes sustain approximately 30 fps with zero drops.
4. The operator disables or unassigns the selected ZoomISO output. Do not
   restart the ZoomISO service or alter any other output.
5. Hold the output absent long enough to observe hold-last behavior and identify
   which recovery signature below applies. A stopped-but-present writer may
   remain cadence-held and need not produce an unavailable transition.
6. The operator restores the exact original assignment and flow identity.
7. Continue observing until the candidate has produced one of the two valid
   recovery signatures, fallback activity has stopped, source lag has returned
   to its normal bounded value, and both probes have remained advancing for at
   least 30 seconds.
8. Capture final health, state, events, probe totals, resource ranges, logs, and
   permanent-service restart counts. Restore first, collect evidence second,
   and clean up the exact temporary resources last.

## Pass criteria

- The candidate and both probes remain alive with zero restarts.
- Program and Preview never stall and report zero probe drops.
- `invalid`, `too_early`, `too_late`, `timeouts`, and
  `selected_input_regressions` do not move.
- The affected source exhibits exactly one valid recovery signature:
  - **Destroyed/recreated flow:** at least one reader reopen, a fresh timeline
    reset, zero selected skips, and a sustained-unavailable alarm that latches
    during the gap and clears after restoration.
  - **Stopped/continued writer:** zero reader reopens, no unavailable alarm,
    and exactly one selected Skip whose magnitude is within one grain of the
    measured outage duration in output frames.
- No synthetic multi-billion-grain skip is reported.
- `sustained_unavailable_active` is zero at the end. Alarm events, held frames,
  repeats, fallbacks, and fallback entries are retained as outage measurements.
- Presentation fallback activity stops after recovery and final source lag is
  no greater than `input_delay_frames + 1`. Fallback frames and entries do not
  move during the final stable 30-second hold.
- The restored ZoomISO output is visible again to the operator path.
- Permanent services remain running with their original restart counts.
- Exact temporary resources are removed and the evidence directory is retained.

Any output stall, probe drop, regression, recovery signature outside the two
declared cases, unbounded lag, failed restore, process loss, permanent-service
restart, or active alarm at the end is a failure. Stop the candidate, restore
the recorded original output assignment, and do not proceed to the
audience-return gate.

## Evidence record

The result should include timestamps for baseline, interruption, restoration,
first reopened read, fallback exit, and stable recovery; baseline-to-final
counter deltas; both probe totals/rates/drops; source lag and fallback ranges;
CPU/RSS ranges; exact cleanup; and the operator's confirmation that the original
ZoomISO assignment is restored.

Do not publish participant names, meeting credentials, private addresses, flow
identities, raw evidence paths, or operational logs. Public documentation should
contain only aggregated results.

## Next gate

After this gate passes, feed the unchanged Program video and aligned Program
Audio pair to the audience meeting. Record at the audience side and use the A/V
reference analyzer to qualify cadence, artifacts, and lip-sync before starting
the 30-minute and eight-hour holds.
