# Immutable Core live promotion acceptance

Date: 2026-10-15

The versioned Core deployment bundle was exercised in a live, CPU-only
eight-source facility. The qualified runtime source was commit `d00178b`; it
included the output-collision guard, corrected TCP port probe, and optional
EasyMXL active-writer check.

## Guard behavior

Before the maintenance replacement, two independent dry runs refused to
launch:

- the rollback-config comparison detected that the running operator already
  owned the candidate Program and Preview flows; and
- the EasyMXL check observed the live writers directly and refused the same
  outputs.

Neither refusal created a container or changed routing.

After the operator stopped, the replacement preflight reported:

- rollback state `exited`;
- launch kind `replacement_with_rollback_stopped`;
- the expected overlapping output identities;
- no active writer on either output; and
- the control port immediately available despite connections from the stopped
  listener entering TCP `TIME_WAIT`.

The immutable candidate then launched successfully. The previous operator was
retained, stopped, as the rollback target.

## Ten-minute hold

The post-promotion hold collected 61 samples across 602.8 seconds.

| Measurement | Result |
| --- | ---: |
| Output frames advanced | 17,999 |
| Container restarts | 0 |
| Core CPU, minimum / average / maximum | 49.22% / 65.32% / 122.10% |
| Core memory, first / last | 77.41 MiB / 77.45 MiB |

All monitored hard counters remained zero:

- invalid, too-early, too-late, and timeout reads;
- reader reopen and synchronization failures;
- empty common-window and selected-input regressions; and
- sustained-unavailable alarms and active state.

Program and Preview retained their pre-maintenance source selections. Every
sample showed advancing output. The reference Skin, Program Audio, NMOS
registration, and Program/Preview/Multiview confidence paths remained healthy,
and all dependent services retained zero restarts. Periodic EasyMXL samples
attributed both outputs to the immutable replacement.

Cadence holds and skipped source grains continued to be recorded as workload
measurements on the older CPU host; they were not accompanied by stalled
output, regressions, read errors, or alarms.

## Operational conclusion

The bundle now supports the intended guarded promotion sequence:

1. Prove a running writer is refused.
2. Stop but retain the rollback operator.
3. Prove its outputs are inactive and the replacement port is bindable.
4. Launch the immutable image.
5. Verify routing, dependent bolt-ons, writer ownership, counters, and resource
   stability before declaring promotion complete.

The active-writer check is optional because it requires an EasyMXL observer,
but it closes the gap left by comparing only the named rollback service's
configuration.
