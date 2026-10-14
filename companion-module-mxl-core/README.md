# MXL Core control for Bitfocus Companion

This developer module turns an Elgato Stream Deck XL into a physical control
surface for MXL Core. It builds its Program and Preview buses from Core's live
source catalog,
frame-boundary CUT, 2-up through 8-up Mix Effects preparation, two persistent
ME layers, live red/green tally feedback, and an EIC health category backed by
the Core and NMOS APIs.

## Secure connection

Keep the Azure control API private. Run a tunnel on the computer that runs
Companion:

```sh
ssh -N -L 13010:127.0.0.1:9710 USER@AZURE_HOST
```

The module's default base URL is `http://127.0.0.1:13010`.

For the optional NMOS registration indicator, create a second loopback-only
tunnel to the Node API and enter it as **NMOS Node URL**:

```sh
ssh -N -L 13012:127.0.0.1:8021 USER@CORE_HOST
```

The resulting URL is `http://127.0.0.1:13012`. Leaving it empty reports
`NOT SET`.

To prove that the Node is registered—rather than merely responding—also tunnel
the Registry Query API and enter it as **NMOS Registry Query URL**:

```sh
ssh -N -L 13011:127.0.0.1:8010 USER@CORE_HOST
```

The resulting URL is `http://127.0.0.1:13011`. The module matches the live
Node ID against the Registry's node inventory before displaying `REGISTERED`.

## Install

Use Companion 5 or newer. Build a signed-local module package and import it
through **Modules → Import module package**:

```sh
npm install
npm run package
```

Import `mxl-core-0.5.0.tgz`, add the **MXL Core** connection, and confirm its
status becomes green/OK. Its default URL is already the secure local tunnel at
`http://127.0.0.1:13010`.

Drag the ready-made presets onto a Stream Deck XL page.

The module polls `/api/mxl/v1/sources`. When a camera, playout, graphics, or
other bolt-on registers with Core, its Program/Preview choices and presets are
rebuilt automatically. Removing an off-air source removes it from newly shown
choices without restarting Companion. Existing ZoomISO and ME controls remain
available.

Recommended layout:

- Row 1: Program 1–8 (red tally)
- Row 2: Preview 1–8 (green tally; Program red overrides)
- Row 3: Prepare 2-up through 8-up
- Row 4: CUT ME, ME 1 Preview, ME 2 Preview, utilities, and CUT at far right

The **5 · EIC health** preset category supplies Core API reachability, active
alarms, routing-counter motion, live/configured input count, Program/Preview
flow identity, true NMOS Registry membership, and audio-meter status. The color
contract is intentionally operational: green means healthy, red means action
is required, neutral gray means intentionally off or not configured, and blue
means informational. `METER OFF` is therefore neutral; only `METER FAIL` is red.
CPU/RSS and negotiated input-format indicators remain separate measurements
until a supported telemetry endpoint is added.

The **6 · NMOS intelligence** category exposes the Node API, Registry API,
actual registration, IS-04 device/source/flow/sender/receiver inventory, and
the advertised reference-clock class. These are read-only engineering facts,
not simulated routing or tally.

`examples/mxl-demo-nine-pages-2026-10-14.companionconfig` is a sanitized
Companion 5.1 export of the demonstration surface, including EIC health and
NMOS detail pages. It contains no connection secrets, private addresses, or
physical-surface identifiers. After importing it, install this module and
adjust the three loopback URLs for the Core Skin, NMOS Node, and NMOS Registry
tunnels on the Companion host.

The smart ME 1 and ME 2 buttons are always safe Preview actions. Their labels
and colors follow the current role: red `ON AIR`, green `PREVIEW`, amber
`SAFE · LOAD`, and dark `NOT READY`. They never cut directly to Program. The
Look buttons rebuild the ME opposite the on-air bank and put the result in
Preview. Use CUT or CUT ME only after checking Preview.

The operator rehearsal validated this packaged module on Companion 5.1:
Program and Preview selection, CUT, tally feedback, and the 2-up through 8-up
Look presets all controlled the live Core instance.

## NMOS boundary

The Companion module is the physical control and tally path. NMOS IS-04 with
BCP-007-03 is the standards discovery path, and the bridge maps immediate IS-05
Program/Preview receiver activations onto Core's grain-boundary control API.
Source kind and capability tags follow dynamic registrations into the NMOS
Source, Flow, and Sender resources. When an off-air source is removed, the
bridge withdraws its Sender, Flow, and Source from a configured Registry in
dependency order. Scheduled activation, disconnect, and IS-07 tally remain
explicit future work.
