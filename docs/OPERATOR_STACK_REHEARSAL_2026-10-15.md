# Operator stack parallel rehearsal — 2026-10-15

This was a short, reversible rehearsal of the six-input operator stack on the
CPU-only lab host. It did not replace the installed operator Core or modify the
meeting, ZoomISO mappings, network, or permanent services.

## Stack exercised

- Core built from the exact continuation commit and launched under a temporary
  name on a loopback-only control port;
- six qualified ZoomISO video/audio input pairs;
- Program Audio in AFV mode;
- NMOS IS-04 inventory and immediate IS-05 Program routing;
- the browser Skin pointed only at the temporary services; and
- a separate confidence encoder and media server publishing H.264 confidence
  video at 640x360p15.

## Result

The temporary Core exposed all six sources and remained healthy. An immediate
IS-05 route moved Program from input 1 to input 2, Program Audio followed to
channel 2 without restarting, and a second IS-05 route restored Program and
AFV audio to input 1. Confidence video remained online through both routes with
zero encoder restarts, zero source tears, zero missing frames, and zero media
server frame errors. The installed operator Core retained the same Program,
Preview, revision, source catalog, and restart counts throughout.

Core hard-error counters remained at zero: invalid, too early, too late,
timeouts, reader reopens, synchronization timeouts, no-common-window,
regressions, copy-overwrite suspects, and sustained-unavailable alarms. Cadence
repeats and skips were observed on this older CPU host and remain performance
measurements rather than a claim of full six-input 1080p30 capacity.

A final point-in-time resource sample reported approximately:

| Service | CPU | RSS |
| --- | ---: | ---: |
| Core | 69% | 77 MiB |
| Program Audio | 66% | 22 MiB |
| Confidence encoder | 41% | 40 MiB |
| Confidence media server | 4% | 23 MiB |
| NMOS bridge | 2% | 16 MiB |
| Skin | below 1% | 13 MiB |

These figures are observations from one short gate, not sizing guarantees.

## NMOS identity correction

The parallel launch exposed that two bridge processes originally generated the
same deterministic NMOS Node and dependent resource IDs. The bridge now accepts
an explicit `--instance-id`. The default preserves every existing stable ID;
additional instances use the supplied namespace and therefore advertise a
fully disjoint Node, device, source, flow, sender, and receiver set. The
temporary bridge was then correctly shown as online but intentionally absent
from the production Registry.

The full local test suite passed after this correction, including ten NMOS
tests that cover legacy-ID compatibility and parallel-instance isolation.

## Rollback and cleanup proof

All temporary containers, output flows, test images, configuration, and the
detached build worktree were removed by exact name. No volume or Docker-wide
prune was used. Every permanent ZoomISO, Core, Program Audio, NMOS, Registry,
and confidence service remained running with zero restarts after cleanup.

This qualifies the bolt-on contracts and scoped rollback procedure for a later
declared maintenance-window promotion. It is not itself a production promotion
or a long-duration six-input performance acceptance.
