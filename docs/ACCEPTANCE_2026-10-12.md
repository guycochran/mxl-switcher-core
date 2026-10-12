# MXL/NDI loaded acceptance — 2026-10-12

## Decision

**Pass.** The isolated MXL Switcher Core NDI profile completed eight loaded
hours with zero media, timing, synchronization, monitoring, process, or
container-restart failures.

This result covers one real NDI input and one VM-local synthetic v210 input,
stable MXL Program and Preview flows, two continuous NDI output publishers, and
two sustained NDI receiver probes. A gate using two independent real sources is
a separate milestone.

## Environment

- Supermicro Proxmox host
- dedicated VM 101 with 8 vCPUs and 8 GiB RAM
- dedicated shared-memory MXL test domain
- `systemd-logind` configured with `RemoveIPC=no`
- lingering enabled for the unprivileged deployment user
- five persistent services plus two temporary receiver probes

The gate began at `2026-10-12T01:27:55Z`, finished at
`2026-10-12T09:28:17Z`, and exited with status `0`.

## Exact result

| Measure | Result |
| --- | ---: |
| Samples | 885 |
| TAKEs | 479 |
| Revision | 33 to 512 |
| Output selections during gate | 863,730 |
| Routed source grains during gate | 1,727,460 |
| Routed grains per source during gate | 863,730 |
| Final cumulative selections | 930,566 |
| Final cumulative routed source grains | 1,861,132 |
| Monitoring errors | 0 |
| Gate failures | 0 |

Every aggregate and per-source `invalid`, `too_early`, `too_late`, `timeout`,
`reader_reopen`, and `no_common_window` counter was zero. Aggregate
`sync_timeout`, `sync_too_early`, `sync_too_late`, and
`selected_input_regressions` were also zero.

Cadence counters increased by 97 repeats, 66 skip events, and 97 skipped grains.
These measure the relationship between independently clocked producers; the
selected input index never regressed. Observed common-window width was 10-28
grains and source-head skew was 0-18 grains.

## Resource envelope

| Service | CPU range | RSS range |
| --- | ---: | ---: |
| Core | 8.00-23.49% | 309.2-311.1 MiB |
| Synthetic source | 8.12-28.17% | 308.5-310.5 MiB |
| NDI ingress | 39.44-61.94% | 204.7-206.6 MiB |
| Program NDI output | 27.38-47.92% | 26.33-26.58 MiB |
| Preview NDI output | 25.31-40.63% | 26.33-26.58 MiB |

All seven containers were running with zero restarts at completion. Core,
source, and ingress health checks were healthy. Final state was Program = NDI,
Preview = synthetic, revision 512. After evidence collection, only the two
temporary probes were removed; the five main services remained running.

## Evidence index

Raw evidence directory on VM 101:

```text
/home/guycochran/mxl-switcher-core-adaptive/run-logind-fix-8h-20261012T012755Z
```

SHA-256 hashes:

```text
7945943c3f0125c9ea24a88e206706ab155345b32b1e131d79ae47b96a798930  summary.txt
fa4846f3c163f49a06d9a7d16d8db060456eb5eab69bb13f1aa1dcb0f1e4e0a0  samples.tsv
1866966bdc70963b15a9b7feae4e6fc321d5fd19beaa1bef489783573a5b12ee  controls.jsonl
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  errors.log
9a271f2a916b0b6ee6cecb2426f0b3206ef074578be55d9bc94f6f3fe3ab86aa  exit-status.txt
```

## Source-loss recovery addendum

After acceptance, a controlled three-second interruption recreated the real NDI
input flow under the same UUID with a new shared-memory inode (`2654` to `2761`).
Core automatically detected the replacement, retried until the new reader was
ready, reopened it once, and resumed synchronized routing.

Both direct NDI receiver probes remained under active decode load throughout.
Core held 212 Program/Preview output grains using the last valid pictures and
emitted zero invalid grains. All too-early, too-late, timeout, synchronization,
and regression counters remained zero. The five services and two probes were
still running with zero restarts at completion.

## Two-real-NDI-source addendum

The next milestone replaced the synthetic source with an independent real NDI
sender at `192.168.1.107:5961`. Its native 30/1 video was normalized at the I/O
boundary to the Core operating point of 30000/1001. The sender was video-only;
disabling the unused audio branch was required to prevent that combined ingest
pipeline from stopping after its first 29-grain ring.

The five-minute loaded gate passed on 2026-10-12:

| Measure | Result |
| --- | ---: |
| Samples | 24 |
| TAKEs / final revision | 5 / 5 |
| Final routed source grains | 23,276 |
| Routed grains per source | 11,638 |
| Invalid / too early / too late | 0 / 0 / 0 |
| Read timeout / reader reopen | 0 / 0 |
| Sync timeout / sync early / sync late | 0 / 0 / 0 |
| No common window / regression | 0 / 0 |
| Monitoring errors / gate failures | 0 / 0 |
| Common-window width | 28 grains |
| Source-head skew | 0 grains |

Both sustained NDI receivers remained running with zero restarts. Program
rendered at least 11,203 frames and Preview at least 11,199 frames, each with
zero decoder drops and approximately 29.98 and 29.97 average fps respectively.
All five active service containers were healthy with zero restarts at cleanup;
only the two temporary probes were removed. The stopped synthetic container was
retained as a rollback source.

Resource ranges:

| Service | CPU range | RSS range |
| --- | ---: | ---: |
| Core | 13.56-25.88% | 319.9-320.2 MiB |
| NDI ingress A | 43.28-58.81% | 183.3-191.7 MiB |
| NDI ingress B | 20.36-26.11% | 183.2-187.4 MiB |
| Program NDI output | 32.76-43.60% | 25.19-27.64 MiB |
| Preview NDI output | 31.18-40.33% | 25.82-27.36 MiB |

Raw evidence on VM 101:

```text
/home/guycochran/mxl-switcher-core-adaptive/run-two-real-ndi-5m-20261012T122000Z
```

SHA-256 hashes:

```text
379656d52f8fbedc6ed33413c300430691a36cdb1ee1fe22a986ad830732069f  summary.txt
9413da8b602f6bd93d6917758c0cdeb6f97c81ba63a9b0e12168fbf5faa6d9ea  samples.tsv
1c266961e03265cba2aa627eea27bc82aca06735913a167957b1101b87b6899e  controls.jsonl
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  errors.log
```
