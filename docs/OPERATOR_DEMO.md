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
    +--> ME 1 / ME 2 (persistent 2-up through 8-up layers)
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
- CUT;
- safe off-air preparation of 2-up through 8-up layouts;
- ME 1 / ME 2 status and CUT.

The recommended Stream Deck XL page is Program 1–8 on row one, Preview 1–8 on
row two, 2-up through 8-up Look buttons on row three, and CUT ME / ME 1 Preview /
ME 2 Preview with CUT at the far-right of row four. The same controls remain usable from Companion's browser
button grid when a physical surface is not attached.

The October 2026 rehearsal confirmed this page on Companion 5.1 with live
switching, Preview selection, CUT, tally feedback, and 2-up through 8-up Look
preparation. This is a validated operator path rather than a UI mock-up.

## Rehearsal

Run `scripts/demo-preflight.sh` on the production node immediately before the
session. It is deliberately read-only: it opens no additional MXL readers and
does not change routing, meetings, or containers.

1. Confirm ZoomISO A and B, Core, both SuperSource banks, multiview, the audio
   mixer, MediaMTX, and all three confidence adapters are running with zero
   restarts.
2. Confirm all eight input tiles move and Program and Preview are distinct.
3. Preview inputs 1 through 8; CUT at least twice and confirm tally follows.
4. Prepare a 2-up on the inactive bank, CUT it, then repeat with 4-up and 8-up.
5. Show that the NMOS Node advertises ten senders (eight inputs and two
   SuperSource banks) and two receivers (Program and Preview).
6. End on a direct source in Program and a prepared composition in Preview.

Do not run deep ring probes, the staged compositor gate, or the experimental
Program clock-correction candidate during the live demonstration. A
non-dropping diagnostic reader can backpressure ZoomISO's small audio ring and
make a healthy production path look stalled. The preflight checks existing
service state and HTTP surfaces without attaching another reader.

## Tonight's run of show

Target eight to ten minutes and leave the system in a simple direct-source
state when questions begin.

1. **Frame the point (45 seconds).** Eight isolated Zoom participants enter the
   node once as native MXL video and audio flows. Core, compositors, confidence
   monitors, and control surfaces share that media without another internal
   encode/decode hop.
2. **Prove the sources (60 seconds).** Show the 4x2 multiview, click several
   tiles into Preview, and confirm that all eight pictures are live.
3. **Prove deterministic switching (90 seconds).** Use Companion or the Skin to
   select Preview 1-8 and press CUT two or three times. Point out Program and
   Preview tally and the stable Program identity.
4. **Prove off-air composition (three minutes).** Prepare 2-up on the inactive
   ME bank, CUT it to air, then repeat with 4-up and 8-up. Explain that a Look
   change takes roughly 0.9-1.8 seconds off air while the CUT between already
   warm ME banks is immediate.
5. **Prove control separation (60 seconds).** Make one cut from the browser and
   one from Companion/Stream Deck. Both operate the same Core state; neither is
   in the media path.
6. **Show output direction (60 seconds).** Use the architecture slide to show
   the Program Master feeding WebRTC confidence today and isolated meeting,
   NDI, SRT, SDI, recording, and RTMP adapters as the output roadmap.
7. **Close on the claim (30 seconds).** One native media plane, multiple useful
   production functions, and fewer codec boundaries inside the node.

### Demo-safe defaults

- Keep Companion visible as the fastest operational fallback.
- Keep one ME bank simple (2-up or 4-up) while demonstrating a dense 8-up on
  the other bank.
- Use CUT terminology consistently. Do not rebuild a compositor on air.
- Treat browser Program audio as optional confidence monitoring. If it is
  rough, mute the browser player and continue the video/control proof.
- Do not map the experimental reclocked Program Audio flow into ZoomISO B
  during the session.

### Recovery without heroics

- If the Skin becomes slow, continue from Companion; the control surface is not
  the media path.
- If a dense Look is not ready, CUT to a direct source or the known-good 2-up
  bank. Do not restart ZoomISO or Core during the session.
- If browser confidence stalls but Program tally continues, explain the edge
  monitor boundary and keep switching from Companion.
- If audio becomes scratchy, mute the browser and state the measured result:
  native 48 kHz PCM is present and metered; the clock-correct meeting-return
  path is a validated candidate, not tonight's default.

## Suggested live story

1. Punch all eight sources through Preview to establish that they are live.
2. CUT two or three direct sources and point out frame-boundary switching and
   Companion tally.
3. Punch a 2-up Look into the inactive SuperSource bank, then CUT it.
4. Repeat with 4-up and 8-up to show that Program remains undisturbed while the
   next Look is prepared off air.
5. Show the browser Skin and Companion controlling the same Core state.
6. Start the diagnostic Playout/Keyed bolt-on and show its moving timecode
   sources appear in Core, NMOS, and Companion without restarting Core. Preview
   and CUT each one, then safely remove one while it is off air.
7. Finish with the eight-input multiview and the NMOS discovery/routing surface.

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
- persistent ME 1 / ME 2 layers and 2-up through 8-up compositions;
- approximately 0.9–1.8 second off-air Look reconfiguration;
- browser Program, Preview, and eight-input multiview confidence monitoring;
- Companion 5.1 button-grid switching with live tally and dynamic source
  choices/presets;
- runtime source registration/removal for camera, playout, graphics, and other
  bolt-ons; and
- NMOS IS-04 discovery plus immediate IS-05 Program/Preview routing.

The next acceptance gates are intentionally separate:

- Core audio-follow-video, output channel mapping, and measured A/V
  synchronization;
- matched MXL-versus-NDI compute and latency benchmarks on identical hardware;
- measured camera-to-display latency rather than an inferred transport claim;
- controlled component-failure and recovery testing;
- production edge gateways for SDI, NDI, WebRTC, recording, and streaming;
- authenticated control, TLS, secrets management, roles, and audit history;
- multi-operator conflict feedback and locking; and
- transitions, key/fill, graphics, borders, crops, and reusable named Looks.

Audio should be described precisely during the demonstration. ZoomISO exposes
eight native 48 kHz stereo Float32 MXL audio flows, their ring indices advance,
and live program material produced measurable RMS and peak levels. The browser
multiview displays per-source stereo VU meters, and the Program mixer provides
AFV and manual modes. An isolated Program Master candidate measured the source
clock and produced a clock-paced exact-48-kHz output with zero PTS
discontinuities and zero hard sample corrections during its clean live trace.
That candidate remains deliberately outside the meeting-return mapping. This
is strong clock-recovery evidence, not yet a finished end-to-end A/V-sync or
audience-meeting acceptance result.

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
- **Is audio finished?** Native 48 kHz source audio, metering, mixing, and an
  isolated clock-correct output candidate are working. The final meeting-return
  mapping and measured end-to-end A/V sync remain the next acceptance gate.
- **Can existing systems control it?** The browser Skin and Companion use the
  same control API, while NMOS provides the standards-facing discovery path.
- **What does cloud cost?** VM size, runtime, egress, and edge codecs dominate;
  one test VM is not a general cost model.

## Standards boundary

`tools/mxl_core_nmos.py` provides a live IS-04 v1.3 view using
`urn:x-nmos:transport:mxl` and BCP-007-03-style tags. It supports peer-to-peer
inspection and optional registry registration with `--registry`. Its IS-05
v1.1 Receiver API exposes the Program and Preview buses as destinations;
immediate routes map to Core's grain-boundary `cut` and `preview` commands.

Scheduled IS-05 activation, receiver disconnect, and IS-07 tally are not yet
claimed. Companion remains the richer physical control/tally bridge, while an
NMOS controller such as Bitfocus Buttons can use the standards-native router.
