# MXL Core control for Bitfocus Companion

This developer module turns an Elgato Stream Deck XL into a physical control
surface for MXL Core. It provides eight-source Program and Preview buses,
frame-boundary TAKE, 2-up through 8-up SuperSource preparation, A/B-bank
status, and live red/green tally feedback.

## Secure connection

Keep the Azure control API private. Run a tunnel on the computer that runs
Companion:

```sh
ssh -N -L 13010:127.0.0.1:9710 USER@AZURE_HOST
```

The module's default base URL is `http://127.0.0.1:13010`.

## Install

Use Companion 5 or newer. Build a signed-local module package and import it
through **Modules → Import module package**:

```sh
npm install
npm run package
```

Import `mxl-core-0.2.0.tgz`, add the **MXL Core** connection, and confirm its
status becomes green/OK. Its default URL is already the secure local tunnel at
`http://127.0.0.1:13010`.

Drag the ready-made presets onto a Stream Deck XL page.

Recommended layout:

- Row 1: Program 1–8 (red tally)
- Row 2: Preview 1–8 (green tally; Program red overrides)
- Row 3: Prepare 2-up through 8-up
- Row 4: TAKE, TAKE SS, SuperSource A/B, and operator utility keys

The SuperSource preset buttons punch the selected layout on the safe inactive
warm bank and place it in Preview. Use TAKE or TAKE SS to put it on air.

## NMOS boundary

The Companion module is the physical control and tally path. NMOS IS-04 with
BCP-007-03 remains the standards discovery path. IS-05 routing and IS-07 tally
are intentionally separate future control-plane work; this module does not
pretend that read-only discovery is routing.
