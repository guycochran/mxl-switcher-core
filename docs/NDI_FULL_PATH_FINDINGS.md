# NDI full-path acceptance findings

Date: 2026-10-11

## Executive conclusion

The MXL Switcher Core can route stable v210 Program and Preview flows, accept
frame-boundary control changes, and feed two simultaneous NDI outputs. The
isolated five-minute gate passed cleanly. The subsequent 30-minute gate, with
both NDI outputs continuously encoded and decoded, exposed a scheduling defect:
the Core's fixed wall-clock-relative input index can fall outside the common
readable window of the selected source rings when the host is under sustained
load.

This is not evidence of corrupt source media, container instability, a memory
leak, or an NDI pipeline crash. All seven containers remained running with zero
restarts, source invalid counters stayed at zero, direct read timeouts stayed at
zero, and Core RSS remained essentially flat. The failure is localized to how
the Core chooses the input grain for each output frame.

The current build is a successful pilot and demonstration path, but it should
not yet be described as production-ready under loaded, long-duration operation.

## Test topology

- Dedicated MXL domain: `/dev/shm/mxl/domain_ndi_lab`
- Operating point: 1920x1080, v210 inside MXL, 30000/1001 fps
- Ring history: one second, 29 grains
- Core input delay: five frames
- Bounded read timeout: 20 ms
- Source A: NDI ingress
- Source B: deterministic synthetic v210 publisher
- Stable outputs: Program and Preview MXL flows
- NDI egress: separate Program and Preview publishers
- Loaded gate: one sustained receiver for each NDI output
- Control load: regular TAKE commands throughout the run

The NDI boundary converts the internal v210 signal to the format required by
the NDI pipeline. The Core itself does not abandon v210 or mutate source
payloads.

## Results

### Isolated five-minute gate: pass

The Core, both sources, and both outputs ran without the sustained receiver load.

| Measure | Result |
| --- | ---: |
| Samples | 25 |
| TAKEs | 4 |
| Routed grains | 19,322 |
| Routed from each source | 9,661 |
| Invalid output grains | 0 |
| Too early | 0 |
| Too late | 0 |
| Direct read timeouts | 0 |
| Reader reopens | 0 |
| Synchronization timeouts | 0 |
| Monitoring errors | 0 |

Evidence directory on VM 101:
`~/mxl-switcher-core/run-ndi-core-5m-history1s-delay5-20261011T185227Z`

### Loaded 30-minute full-path gate: fail

Two receiver containers continuously consumed Program and Preview so that both
NDI encode and decode paths remained under real load.

| Measure | Result |
| --- | ---: |
| Samples | 127 |
| TAKEs / final revision | 29 / 29 |
| Routed grains | 107,314 |
| Invalid output grains | 3,327 |
| Too early | 2,820 |
| Too late | 507 |
| Direct read timeouts | 0 |
| Reader reopens | 0 |
| Synchronization waits | 55,321 |
| Synchronization timeouts | 5,731 |
| Monitoring errors | 0 |

Per-source attribution:

| Source | Routed | Upstream invalid | Too early | Too late | Read timeout | Reopen |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| NDI ingress | 52,395 | 0 | 2,793 | 132 | 0 | 0 |
| Synthetic | 54,919 | 0 | 27 | 375 | 0 | 0 |

All five service containers and both temporary receiver containers were running
with zero restarts at completion. Final state was Program = synthetic and
Preview = NDI.

Resource ranges observed during the loaded gate:

| Service | CPU | RSS |
| --- | ---: | ---: |
| Core | 11.13-56.82% | 309.2-311.1 MiB |
| Synthetic source | 14.76-60.30% | 308.5-311.5 MiB |
| NDI ingress | 56.94-116.17% | 203.1-220.0 MiB |
| Program NDI egress | 38.74-96.99% | 30.42-38.49 MiB |
| Preview NDI egress | 36.51-84.11% | 26.16-38.68 MiB |

Evidence directory on VM 101:
`~/mxl-switcher-core/run-ndi-full-path-loaded-30m-20261011T193447Z`

## What the test proved

- Stable Program and Preview flow identities survive repeated TAKEs.
- The control plane applied 29 frame-boundary TAKEs with matching revision.
- Both Program and Preview were discoverable and decodable as NDI video.
- The v210 MXL-to-NDI path can run concurrently in both directions.
- Containers stayed alive and did not restart under the measured load.
- The Core did not show material RSS growth.
- Invalid outputs were generated locally by out-of-range reads, not attributed
  to invalid upstream grains.

## What failed and why

For output index `N`, the current MXL backend always requests input index
`N - input_delay_frames`. That works only while all selected producers remain
close enough to the Core's wall-clock-derived output cadence for that one index
to be present in every source ring.

Under the combined ingress, two egress encoders, and two decoder loads, producer
heads moved relative to one another. The requested fixed index was sometimes
ahead of a source (`too_early`) and sometimes already outside another source's
history (`too_late`). The occurrence of both conditions, including late reads
from the synthetic source, shows that the problem is the lack of a continuously
computed common read window, not simply an undersized timeout.

Increasing only the read timeout cannot repair an index that is already too
late, and increasing only the fixed delay adds latency while moving the failure
boundary rather than adapting to source skew.

## Proposed remediation

### 1. Select each input index from the common readable window

Before routing an output frame, inspect runtime information for every selected
reader and calculate their overlapping readable interval:

- upper bound: the oldest of the selected source heads;
- lower bound: the newest of the selected sources' oldest readable grains;
- chosen input: the newest index safely inside that overlap, with a small
  configurable safety margin.

Store that chosen input index for the entire prepared output frame so Program
and Preview route from the same decision. If no overlap exists, record that
condition explicitly and apply a documented failure policy rather than waiting
at an index that cannot succeed.

### 2. Add timing observability

Expose enough data to diagnose source skew without reconstructing it from
errors:

- reader head and oldest-readable index per source;
- chosen common input index and resulting delay;
- common-window width;
- synchronization status split into timeout, too-early, and too-late outcomes;
- count and attribution of no-overlap decisions.

### 3. Test the scheduler deterministically

Add mocked tests for unequal source heads, jitter, shrinking overlap, and no
overlap. Then repeat live gates in this order:

1. five minutes with both NDI receivers active;
2. 30 minutes with both receivers active and regular TAKEs;
3. source interruption and recovery;
4. Core and NDI egress restart/reconnection behavior;
5. eight-hour loaded acceptance.

A loaded gate passes only with zero invalid, too-early, too-late, read-timeout,
reader-reopen, synchronization-timeout, and monitoring-error counts; zero
container restarts; bounded RSS; and the expected TAKE/revision total.

### 4. Optimize deployment after correctness

Once adaptive scheduling passes, measure CPU placement and contention. Candidate
deployment improvements include CPU affinity or priority separation, placing NDI
egress on a separate lightweight VM, and offering a Program-only NDI profile on
constrained hosts. Keep v210 as the MXL core format; conversions belong at I/O
boundaries. Audio-follow-video and lower-cost multiview/proxy outputs are later
features, not substitutes for correcting the scheduler.

## Additional resilience finding

A Core restart caused the temporary NDI receivers to disconnect even though the
egress containers remained alive. Restart and receiver reconnection therefore
need their own acceptance case after the scheduler is corrected. It is not the
root cause of the loaded timing failure.

## Adaptive scheduler follow-up

The adaptive common-window scheduler was deployed on 2026-10-11. An initial
loaded run retained a five-grain margin behind the newest common head and
recorded one late synthetic-source read. Because an MXL runtime head describes
an already committed grain, that margin reduced protection against overwrite by
a faster producer. The NDI pilot was changed to select the newest committed
grain shared by all active sources.

The resulting loaded five-minute gate passed:

| Measure | Result |
| --- | ---: |
| Samples | 22 |
| TAKEs / final revision | 4 / 4 |
| Routed grains | 20,977 |
| Adaptive selections | 10,489 |
| Common-window width | 23-27 grains |
| Invalid / too early / too late | 0 / 0 / 0 |
| Read timeout / reader reopen | 0 / 0 |
| Sync timeout / sync early / sync late | 0 / 0 / 0 |
| No common window | 0 |
| Monitoring errors / gate failures | 0 / 0 |

All seven containers were running with zero restarts at completion. The two
temporary NDI receiver containers were then removed, leaving the five main
services healthy. Evidence directory on VM 101:
`~/mxl-switcher-core/run-ndi-adaptive-newest-loaded-5m-20261011T222317Z`

Before the longer gates, cadence telemetry is being added for repeated selected
indexes, skipped indexes and grains, regressions, source-head skew, and minimum
and maximum common-window width. This distinguishes readable output from truly
continuous temporal output.

## VM sizing and login-lifecycle follow-up

The first cadence gate was run on a four-vCPU VM while NDI ingress, two v210
routes, two NDI encoders, and two sustained NDI receivers were active. The VM
was CPU-saturated: load was approximately 15, with essentially zero idle CPU,
I/O wait, or steal time. That run produced 2,522 invalid outputs, 58 late reads,
and 1,232 no-common-window decisions. All containers remained alive. This was a
capacity failure at the chosen VM allocation, not evidence of a storage,
network, or memory limit.

The dedicated VM was increased to eight vCPUs and the unrelated VM was stopped
for the isolated capacity test. Under the same seven-container topology, the VM
then retained roughly 75-81% idle CPU and about 6 GiB free memory. No storage or
network changes were required.

A separate lifecycle fault appeared after the VM restart: flows created by the
unprivileged deployment user disappeared when short SSH sessions ended, while
root-owned ingress flows persisted. A system-wide file-operation trace
identified `systemd-logind` as the process recursively deleting the user's MXL
flow directories. The VM still had logind's default `RemoveIPC=yes`, and the
deployment user did not have lingering enabled.

On this dedicated test VM, the validated correction was:

- set `RemoveIPC=no` in a local logind drop-in;
- enable lingering for the deployment user;
- restart logind;
- clean only the six UUID directories owned by the isolated pilot;
- start ingress, synthetic source, Core, and NDI outputs in dependency order;
- verify the flow directories from a new login before beginning a gate.

The post-fix loaded five-minute gate passed:

| Measure | Result |
| --- | ---: |
| Samples | 24 |
| TAKEs / final revision | 4 / 4 |
| Routed grains | 20,794 |
| Routed from each source | 10,397 |
| Invalid / too early / too late | 0 / 0 / 0 |
| Read timeout / reader reopen | 0 / 0 |
| Sync timeout / sync early / sync late | 0 / 0 / 0 |
| No common window | 0 |
| Selected-index regressions | 0 |
| Repeat / skip events / skipped grains | 1 / 1 / 1 |
| Common-window width | 27-28 grains |
| Source-head skew | 0-1 grain |
| Monitoring errors / gate failures | 0 / 0 |

All seven containers finished running with zero restarts. The repeat and skip
are cadence measurements, not failures; the selected input never regressed.
Evidence directory on VM 101:
`~/mxl-switcher-core-adaptive/run-logind-fix-5m-20261012T005133Z`.

This result isolates three previously conflated concerns: adaptive scheduling
corrected grain selection, eight vCPUs removed artificial CPU starvation, and
the logind policy preserved unprivileged MXL flow lifetime across administrative
logouts. The loaded 30-minute and eight-hour gates described below complete the
release evidence for this deployment profile.

## Final lifecycle-fix confirmation

The corrected eight-vCPU topology passed both confirmation gates on 2026-10-12.
The topology contained one real NDI ingress, one VM-local synthetic v210 source,
stable MXL Program and Preview flows, two continuous NDI egress encoders, and one
sustained receiver on each output. This accepts that isolated profile; it does
not substitute for the later two-independent-real-source gate.

### Loaded 30-minute gate

| Measure | Result |
| --- | ---: |
| Samples | 144 |
| TAKEs / final revision | 29 / 33 |
| Routed grains | 129,678 |
| Routed from each source | 64,839 |
| Invalid / too early / too late | 0 / 0 / 0 |
| Read timeout / reader reopen | 0 / 0 |
| Sync timeout / sync early / sync late | 0 / 0 / 0 |
| No common window / selected-index regressions | 0 / 0 |
| Repeat / skip events / skipped grains | 15 / 13 / 15 |
| Common-window width | 26-28 grains |
| Source-head skew | 0-2 grains |
| Monitoring errors / gate failures | 0 / 0 |

All seven containers were running with zero restarts. Core CPU was 8.28-19.91%
with 309.2-311.4 MiB RSS. Source CPU was 8.85-20.38%, ingress 43.61-62.77%,
Program egress 28.17-37.75%, and Preview egress 25.84-35.47%.

### Loaded eight-hour gate

The final gate ran from `2026-10-12T01:27:55Z` through
`2026-10-12T09:28:17Z` and exited zero.

| Measure | Result |
| --- | ---: |
| Samples | 885 |
| TAKEs / revision movement | 479 / 33 to 512 |
| Output selections during gate | 863,730 |
| Routed source grains during gate | 1,727,460 |
| Routed from each source during gate | 863,730 |
| Final cumulative selections / routed grains | 930,566 / 1,861,132 |
| Invalid / too early / too late | 0 / 0 / 0 |
| Read timeout / reader reopen | 0 / 0 |
| Sync timeout / sync early / sync late | 0 / 0 / 0 |
| No common window / selected-index regressions | 0 / 0 |
| Repeat / skip-event / skipped-grain delta | 97 / 66 / 97 |
| Common-window width | 10-28 grains |
| Source-head skew | 0-18 grains |
| Monitoring errors / gate failures | 0 / 0 |

The temporary reductions in common-window width and increases in source skew
were absorbed by the adaptive scheduler without a read, validity, timing, or
synchronization error. RSS remained bounded.

| Service | CPU range | RSS range |
| --- | ---: | ---: |
| Core | 8.00-23.49% | 309.2-311.1 MiB |
| Synthetic source | 8.12-28.17% | 308.5-310.5 MiB |
| NDI ingress | 39.44-61.94% | 204.7-206.6 MiB |
| Program NDI egress | 27.38-47.92% | 26.33-26.58 MiB |
| Preview NDI egress | 25.31-40.63% | 26.33-26.58 MiB |

At completion, Program was NDI and Preview was synthetic. All seven containers
were running with zero restarts; Core, source, and ingress were healthy. The two
temporary receiver probes were then removed and the five main services remained
running.

The raw evidence remains on VM 101 at
`~/mxl-switcher-core-adaptive/run-logind-fix-8h-20261012T012755Z`. A concise,
hash-indexed public record is in
[`ACCEPTANCE_2026-10-12.md`](ACCEPTANCE_2026-10-12.md).
