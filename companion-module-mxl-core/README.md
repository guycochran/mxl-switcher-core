# MXL Core control for Bitfocus Companion

This developer module turns an Elgato Stream Deck XL into a physical control
surface for MXL Core. It provides eight-source Program and Preview buses,
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
`NOT SET`; it never fabricates a green registration state.

## Install

Use Companion 5 or newer. Build a signed-local module package and import it
through **Modules → Import module package**:

```sh
npm install
npm run package
```

Import `mxl-core-0.2.2.tgz`, add the **MXL Core** connection, and confirm its
status becomes green/OK. Its default URL is already the secure local tunnel at
`http://127.0.0.1:13010`.

Drag the ready-made presets onto a Stream Deck XL page.

Recommended layout:

- Row 1: Program 1–8 (red tally)
- Row 2: Preview 1–8 (green tally; Program red overrides)
- Row 3: Prepare 2-up through 8-up
- Row 4: CUT ME, ME 1 Preview, ME 2 Preview, utilities, and CUT at far right

The **5 · EIC health** preset category supplies live Core counter health, NMOS
Node response, Core input count, stable Program/Preview flow identity, and
audio-meter status. Red means absent, disabled, or unhealthy; green is derived
from the current API response. CPU/RSS and negotiated input-format indicators
remain separate measurements until a supported telemetry endpoint is added.

`examples/mxl-demo-six-pages-2026-10-14.companionconfig` is a sanitized
Companion 5.1 export of the six-page demonstration surface. It contains no
connection secrets. After importing it, install this module and adjust the two
loopback URLs for the Core Skin and NMOS Node tunnels on the Companion host.

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
BCP-007-03 remains the standards discovery path. IS-05 routing and IS-07 tally
are intentionally separate future control-plane work; this module does not
pretend that read-only discovery is routing.
