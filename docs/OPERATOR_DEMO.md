# Operator demo profile

This profile demonstrates a complete software-defined production chain while
keeping the native MXL media path separate from the confidence-monitor path.

## Signal flow

```text
Zoom meeting
    |
    v
ZoomISO (8 x 1080p29.97 v210 MXL sources)
    |
    +--> MXL Core --> stable Program / Preview flows
    |                    |
    |                    +--> Program confidence: 1080p29.97 WebRTC
    |                    +--> Preview confidence: 720p15 WebRTC
    |
    +--> SuperSource A/B (2-up through 8-up)
    |
    +--> eight-source multiview --> 720p15 WebRTC

Operator Skin / Companion --> loopback control API --> Core
NMOS IS-04 sidecar --------> read-only discovery of sources and buses
```

The MXL sources, Core routes, Program/Preview outputs, and SuperSource outputs
remain 1920x1080p29.97 v210. Only the browser confidence encodes are reduced.
That distinction is important: operator-monitor bandwidth and decode cost can be
tuned without changing the production signal.

## Demo setup

Keep the Azure control and confidence services on loopback. Forward them to the
operator computer with SSH:

```sh
ssh -N \
  -L 13010:127.0.0.1:9710 \
  -L 13021:127.0.0.1:8021 \
  -L 13089:127.0.0.1:8889 \
  USER@AZURE_HOST
```

Open `http://127.0.0.1:13010/` for the operator Skin. The optional NMOS Node API
is then visible at `http://127.0.0.1:13021/x-nmos/node/v1.3/`.

For a Stream Deck, build `companion-module-mxl-core` with `npm run package`,
import the resulting `.tgz` from Companion's Modules page, add an MXL Core
connection, and use `http://127.0.0.1:13010` as its base URL. The supplied
presets provide:

- Program 1-8 with red tally;
- Preview 1-8 with green tally;
- TAKE;
- safe off-air preparation of 2-up through 8-up layouts;
- SuperSource A/B status and TAKE.

The recommended Stream Deck XL page is Program 1–8 on row one, Preview 1–8 on
row two, 2-up through 8-up Look buttons on row three, and TAKE / TAKE SS / bank
controls on row four. The same controls remain usable from Companion's browser
button grid when a physical surface is not attached.

The October 2026 rehearsal confirmed this page on Companion 5.1 with live
switching, Preview selection, TAKE, tally feedback, and 2-up through 8-up Look
preparation. This is a validated operator path rather than a UI mock-up.

## Rehearsal

1. Confirm ZoomISO, Core, both SuperSource banks, multiview, MediaMTX, and all
   three confidence adapters are running with zero restarts.
2. Confirm all eight input tiles move and Program and Preview are distinct.
3. Preview inputs 1 through 8; TAKE at least twice and confirm tally follows.
4. Prepare a 2-up on the inactive bank, TAKE it, then repeat with 4-up and 8-up.
5. Show that the NMOS Node advertises ten senders (eight inputs and two
   SuperSource banks) and two receivers (Program and Preview).
6. End on a direct source in Program and a prepared composition in Preview.

## Suggested live story

1. Punch all eight sources through Preview to establish that they are live.
2. TAKE two or three direct cuts and point out frame-boundary switching and
   Companion tally.
3. Punch a 2-up Look into the inactive SuperSource bank, then TAKE it.
4. Repeat with 4-up and 8-up to show that Program remains undisturbed while the
   next Look is prepared off air.
5. Show the browser Skin and Companion controlling the same Core state.
6. Finish with the eight-input multiview and the NMOS discovery surface.

The concise message is: eight live sources enter once, remain native through
switching and compositing, generate multiple Looks, and can be operated from a
browser or a physical control surface without inserting another encode/decode
stage between production functions.

## Performance interpretation

The browser is deliberately asymmetric: Program is the confidence surface that
deserves full frame rate and resolution, while Preview and multiview favor low
latency and reduced decode cost. CPU use grows primarily with the number of
active SuperSource boxes because both A/B banks stay live for seamless cuts.
This is expected compositing work, not MXL transport overhead.

For the cleanest live presentation, keep the two A/B banks warm and punch the
next Look into the inactive bank. Scene changes do not rebuild Docker; the
validated path changes 2-up through 8-up layouts in about 0.9–1.8 seconds while
the on-air bank remains untouched. Very dense layouts in both A and B
simultaneously are a useful stress test, but leave less safety margin than a
typical show state.

## Validated now and next gates

Validated in the current demonstration profile:

- eight ZoomISO-derived 1920x1080p29.97 MXL video inputs;
- stable Program and Preview identities with frame-boundary direct cuts;
- persistent SuperSource A/B banks and 2-up through 8-up compositions;
- approximately 0.9–1.8 second off-air Look reconfiguration;
- browser Program, Preview, and eight-input multiview confidence monitoring;
- Companion 5.1 button-grid switching with live tally; and
- read-only NMOS IS-04 discovery.

The next acceptance gates are intentionally separate:

- end-to-end audio transport, channel mapping, metering, audio-follow-video,
  and measured A/V synchronization;
- matched MXL-versus-NDI compute and latency benchmarks on identical hardware;
- measured camera-to-display latency rather than an inferred transport claim;
- controlled component-failure and recovery testing;
- production edge gateways for SDI, NDI, WebRTC, recording, and streaming;
- authenticated control, TLS, secrets management, roles, and audit history;
- multi-operator conflict feedback and locking; and
- transitions, key/fill, graphics, borders, crops, and reusable named Looks.

Audio should be described precisely during the demonstration: this acceptance
proves the MXL video production path, switching, compositing, monitoring, and
control. It does not yet prove embedded audio or audio-follow-video. A silent
audio observation appeared on both local and higher-capacity hosts, so it
should not be presented as evidence of CPU starvation without a dedicated
audio-path test.

The browser confidence encodes are also not the production outputs. Program,
Preview, inputs, and SuperSource flows remain native 1080p; the lower-resolution
Preview and multiview browser views are an intentional operator-bandwidth
choice.

## Questions to expect

- **Why MXL?** It keeps native media available between production functions and
  avoids making every internal hop another codec boundary.
- **Is a Look change instant?** TAKE between already-prepared A/B banks is the
  live switch. Reconfiguring the inactive bank currently takes about 0.9–1.8
  seconds and does not disturb Program.
- **How many sources scale?** Eight have been demonstrated here. A final maximum
  requires a controlled capacity matrix rather than extrapolation.
- **Does it save compute?** The architecture removes avoidable encode/decode
  work. A matched MXL-versus-NDI benchmark is still required for a defensible
  numerical claim.
- **What happens on failure?** The A/B design protects the on-air Look during
  off-air changes; controlled restart and recovery-time evidence remains a
  release gate.
- **Can existing systems control it?** The browser Skin and Companion use the
  same control API, while NMOS provides the standards-facing discovery path.
- **What does cloud cost?** VM size, runtime, egress, and edge codecs dominate;
  one test VM is not a general cost model.

## Standards boundary

`tools/mxl_core_nmos.py` provides a live read-only IS-04 v1.3 view using
`urn:x-nmos:transport:mxl` and BCP-007-03-style tags. It supports peer-to-peer
inspection immediately and optional registry registration with `--registry`.
It does not claim IS-05 routing or IS-07 tally. Companion remains the physical
control/tally bridge until those standards surfaces are implemented.
