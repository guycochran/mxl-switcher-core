# Real ZoomISO Writer-Recovery Gate

This gate verifies that an isolated candidate Core preserves Program and
Preview cadence when one real ZoomISO MXL output disappears and returns under
the same flow identity. It is intentionally separate from the operator Core.

Changing a ZoomISO output from MXL to another protocol and back is **not** this
same-identity gate. Live qualification showed that this operation creates new
video and audio flow UUIDs. Use the discovery-driven atomic rebind gate for
that lifecycle; see
[Real ZoomISO output identity rotation](REAL_ZOOMISO_FLOW_ROTATION_2026-10-15.md).

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
   ZoomISO output's original participant assignment and flow identity. Require
   changing content in addition to advancing cadence before taking a baseline.
2. Start the isolated candidate and independent Program and Preview probes.
   Put the selected output on the candidate Program bus and a different,
   untouched ZoomISO source on candidate Preview as the control. Confirm both
   assignments from the candidate `/state` response before taking a baseline.
3. Allow startup measurements to settle, then capture a counter baseline only
   after both probes sustain approximately 30 fps with zero drops.
4. The operator performs only a previously proven interruption that preserves
   the selected flow identity. Do not use a protocol flip: ZoomISO rotates flow
   UUIDs on return. Do not restart the ZoomISO service or alter any other
   output.
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

## Observer harness

`tools/mxl-atem-skin/mxl_recovery_observer.py` prepares and watches this gate.
**The operator still performs the interruption and restoration by hand.** The
harness never calls a ZoomISO API or container, and never changes a meeting,
a mapping, the network, or a permanent service.

```sh
python3 tools/mxl-atem-skin/mxl_recovery_observer.py plan \
  --image <immutable candidate Core image> --probe-image <local MXL test image> \
  --library <libmxl path in that image> --domain-path <host MXL domain> \
  --selected-source gate-selected --selected-flow <ZoomISO output video flow> \
  --selected-audio-flow <its audio flow>        # or --selected-audio-channel N
  --control-source gate-control --control-flow <different, untouched output> \
  --operator-core-url http://127.0.0.1:<operator port> \
  --program-audio-url http://127.0.0.1:<Program Audio port> \
  --permanent <container> --permanent <container> ...  \
  --host-port <unused loopback port> --forbid-port <each permanent port>
# Without --apply it only plans. Add --apply to run.
```

### Before anything starts

The harness runs these read-only checks first. A dry run performs only these
checks and starts nothing. It refuses if any of the following holds:

- the selected output is the operator Core's Program source;
- the selected output is on the operator Preview (unless
  `--allow-operator-preview` is given);
- the selected output is active in Program Audio;
- its Program Audio input cannot be identified;
- the selected and control outputs are the same;
- a container name, the loopback port, or a declared permanent port
  collides;
- an image is absent;
- a permanent container is not running;
- the evidence directory exists;
- a source flow is missing;
- a candidate output flow already exists in the domain.

### Phases

| Phase | What happens | Bound |
| --- | --- | --- |
| Start | The candidate Core runs on fresh output IDs, with selected on Program and control on Preview, confirmed from `/state`. Two `--network none` probes then read those outputs by index, with the domain mounted read-only. | `--start-timeout` |
| Baseline | Both probes sustain 29–31 fps with no new drops or stalls, and holds, skips, and fallbacks are quiet. | `--baseline-seconds` |
| Awaiting interruption | The harness tells the operator to interrupt, then waits for the selected head to freeze or the reader to reopen. | `--interruption-timeout` |
| Awaiting restoration | The harness tells the operator to restore the exact assignment, then waits for the head to advance again. | `--restoration-timeout` |
| Recovery | The lag must fall to `input_delay_frames + 1` or less, fallbacks must stop, and the alarm must clear. | `--recovery-timeout` |
| Stable hold | Fallback frames and entries must not move, and the lag must stay bounded. | `--stable-seconds` (≥ 30) |

### Failures at any point

These fail the gate immediately:

- any change in `invalid`, `too_early`, `too_late`, `timeouts`, or
  `selected_input_regressions`;
- a probe drop or stall after the baseline;
- a candidate or probe container exiting or restarting;
- Program or Preview no longer carrying selected or control.

### Verdict

The verdict requires **exactly one** recovery signature:

- **destroyed/recreated**: the selected reader reopened at least once, a
  timeline reset occurred, there were no skips, and an alarm was raised;
- **stopped/continued**: no reopen, no alarm, and one skip whose magnitude is
  within one grain of the measured outage. The outage is measured as held
  output frames.

A skip of a million grains or more always fails as an epoch artefact.
Permanent containers must keep their start time and restart count.

### Evidence and cleanup

Evidence is written before cleanup, including after a timeout, a failure, or
Ctrl-C:

- `plan.json`, `preflight.json`, `samples.jsonl`, `events.json`,
  `verdict.json`, and container logs;
- `public-summary.json`, which holds aggregates only, with no flow IDs,
  source names, URLs, or paths.

Cleanup removes only containers labelled `org.mxl.recovery-gate.run=<run id>`.
Then, once the candidate Core is gone, it removes the two candidate output flow
directories with a one-shot helper. It never removes source flows.

Exit codes: 0 is PASS, 1 is FAIL, 2 is refused with nothing changed.

## Next gate

After this gate passes, feed the unchanged Program video and aligned Program
Audio pair to the audience meeting. Record at the audience side and use the A/V
reference analyzer to qualify cadence, artifacts, and lip-sync before starting
the 30-minute and eight-hour holds.
