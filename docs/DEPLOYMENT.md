# Linux deployment

The deployment shape is one unprivileged process, one local configuration file,
and the existing MXL shared-memory domain. No container runtime, database, web
framework, GPU, or media codec package is required by the core.

## Build and stage

Build `mxl-core-live` against the official MXL SDK with `make live`. The
`install-live` target stages the binary, example configuration, and systemd unit.
`DESTDIR` can be used by a package builder:

```sh
make live
sudo make install-live
```

The target deliberately installs the configuration as an example and never
overwrites `/etc/mxl-switcher-core/core.conf`.

## Configure

Copy `packaging/mxl-switcher-core.conf.example` to
`/etc/mxl-switcher-core/core.conf`, replace the placeholder UUIDs, and place the
two output flow definitions beside it. Validate without opening the MXL domain:

```sh
/usr/local/bin/mxl-core-live \
  --check-config /etc/mxl-switcher-core/core.conf
```

Each `source` entry is `operator_id|MXL_flow_UUID|label`. Source ids and flow ids
must be unique. At least two sources are required, but there is no two-input
limit in the engine or control API.

Keep the control listener on loopback unless an authenticated reverse proxy is
used. The core intentionally does not contain account management, TLS keys, or
an internet-facing web stack.

## Service identity and MXL access

Create a locked `mxl-switcher` service account. Grant that account access to the
MXL domain through the facility's existing shared-memory group or ACL; do not run
the switcher as root and do not make unrelated shared memory world-writable.

The supplied unit uses systemd hardening and has no Linux capabilities. If the
facility uses a dedicated MXL group, add it with a local systemd drop-in rather
than editing the versioned unit:

```ini
[Service]
SupplementaryGroups=mxl
```

After installing the unit and local configuration, reload systemd and enable the
service using the host's normal administration workflow.

## Shutdown and restart

SIGTERM stops acceptance of new control connections, finishes the current media
iteration, closes SSE clients, releases MXL handles, and exits. PGM and PVW use
the configured stable UUIDs when systemd starts the process again; cuts never
replace those identities.

Use `GET /api/mxl/v1/health` for the service probe and
`GET /api/mxl/v1/events` for tally/state changes. Upstream invalid or late media
is visible per input rather than being mistaken for a process failure.

Set `input_delay_frames` to the desired safety margin behind the oldest selected
source head. The Core applies that margin to the common live window rather than
subtracting it from wall clock. For
each output frame it inspects every selected discrete ring and chooses the newest
grain inside their common readable window. Program and Preview use that same
prepared input index while retaining their stable output clock and flow IDs.

Use zero when producers publish committed grains, as the MXL runtime head does:
the newest common grain has the most remaining history before a faster producer
can overwrite it. A nonzero margin is available for integrations whose reported
head needs settling time, but it deliberately reduces overwrite headroom. The
NDI pilot therefore uses zero.

If the source rings have no common readable grain, the Core publishes invalid
output grains for that frame and increments `no_common_window` rather than
waiting for an index that cannot succeed. Health also reports the latest source
heads, oldest-readable indexes, selected input index, and common-window width.

Set `read_timeout_ns` to the bounded wait allowed for a selected grain after its
target time. Keep it below one frame period so transport jitter cannot stall the
output cadence; the default is 5 ms.

## Hardware acceptance

`scripts/run-acceptance.sh CONFIG OUTPUT_DIRECTORY` runs a bounded configuration
as a child process and records media output, health, state, SSE events, HTTP TAKE
receipts, CPU, and RSS. It issues a TAKE once per minute and reconnects the event
stream every five minutes. The configuration determines the duration; eight
hours at 30 fps is `frames=864000`.

The driver does not install a service or alter the MXL domain. Run it from the
repository checkout after compiling the live binary. A successful process exit
is necessary but not sufficient: acceptance also requires exactly two output
grains per configured frame, no reader reopenings or read timeouts, bounded RSS,
and explicit attribution of any invalid grains to their upstream inputs.

For the measured NDI pilot topology, loaded acceptance must include sustained
receivers on both Program and Preview. An unloaded gate can hide producer skew
caused by simultaneous NDI ingress, egress encoding, and receiving. See
[NDI full-path acceptance findings](NDI_FULL_PATH_FINDINGS.md) for the baseline
results, resource ranges, and pass criteria.

For a test that is fully isolated from production publishers, create a separate
MXL domain with a domain `options.json` that sets
`urn:x-mxl:option:history_duration/v1.0` to at least two seconds. Build
`live-test-source` and run `mxl-test-source` with two unique v210 flow
definitions in that domain. The publisher writes two deterministic, frame-locked
sources and reports the actual grain count allocated by MXL.
