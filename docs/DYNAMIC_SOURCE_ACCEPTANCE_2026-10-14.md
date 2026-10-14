# Runtime bolt-on source acceptance — 2026-10-14

## Result

An isolated Core candidate passed a 600.951-second live gate with two existing
meeting sources and two runtime bolt-ons: a moving-timecode Playout source and
a moving-timecode Keyed/graphics source. The installed default Core and every
permanent support service remained untouched and at zero restarts.

The gate collected 59 health samples and issued 30 grain-boundary TAKEs across
all four sources. Program continued advancing throughout. The output index
advanced by 17,719 grains and the routed counter advanced from 12,346 to
44,320.

Every baseline-to-final hard counter delta was zero:

- invalid, too early, too late, timeout, and reader reopen;
- sync timeout, sync too early, and sync too late;
- no common window and selected-input regression; and
- sustained-unavailable alarms and active alarms.

The Core candidate and diagnostic publisher both remained running with zero
restarts. Two resource spot checks observed Core at 54.58–75.43% CPU and
76.97–77.29 MiB RSS, and the software diagnostic publisher at 424.67–427.92%
CPU and 149.7–150.3 MiB RSS. The publisher deliberately renders two moving
1080p30 v210 test sources in software; its cost is not Core transport cost.

## Lifecycle proof

While both bolt-ons were off air, the harness removed the Keyed source. The
catalog contracted from four sources to three while Core continued routing.
The same descriptor was then registered again, the catalog returned to four,
and the Keyed source was subsequently taken back to Program. No Core restart or
configuration rewrite occurred.

Registration was also verified as idempotent outside the timed gate. Core
rejected an attempted removal while a source was selected and accepted removal
once that source was neither Program nor Preview.

## NMOS and control-surface proof

The live NMOS bridge discovered all four sources from Core's catalog. It
advertised Playout with `playout` kind and playback/timecode/health
capabilities, and Keyed with `graphics` kind and graphics/timecode/health
capabilities. An immediate IS-05 activation routed Keyed to Preview. Removing
Keyed contracted the peer-to-peer IS-04 Source inventory from four to three;
re-registering it restored the inventory to four.

The NMOS Registry loop now withdraws stale Sender, Flow, and Source resources
in dependency order. The Companion 0.5.0 module likewise rebuilds Program and
Preview action choices, tally choices, and available presets when Core's source
catalog changes. The packaged module passed its build and includes the dynamic
catalog code.

## Timing interpretation

The meeting inputs and software test generator used independent source
timelines. Consequently, observed common-window width ranged from zero to five
grains and observed source skew ranged from zero to 20 grains. Those values are
telemetry for a transition that would require a shared window; direct CUT/TAKE
routing remained valid, the output never stalled, and the hard
`no_common_window` counter remained zero.

## Verification

- All Core, control API, HTTP transport, live-config, and read-window tests
  passed.
- Eight NMOS unit tests passed, including capability tags and Registry
  withdrawal ordering.
- The Companion catalog test and package build passed.
- The live MXL SDK syntax check passed.
- The isolated 10-minute dynamic-source gate and subsequent live IS-05/NMOS
  lifecycle proof passed.

This result establishes the intended bolt-on boundary: an extension can create
a stable MXL flow, register it into a running Core, become controllable through
the Skin, Companion, and NMOS, and leave cleanly while off air. Production
playout, graphics, and key/fill implementations still require their own media,
audio, failure-recovery, and long-duration acceptance gates.
