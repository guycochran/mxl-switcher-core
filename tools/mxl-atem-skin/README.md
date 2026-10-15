# MXL ATEM-style Skin

A localhost-only operator surface for MXL Core. It provides separate Program
and Preview buses for inputs 1–8, frame-boundary CUT, and two persistent Mix
Effects layers for arbitrary 2-up through 8-up compositions. The operator UI
calls the internal `supersource-a` and `supersource-b` sources ME 1 and ME 2.

The web server is deliberately outside the media path. It proxies only the
bounded Core v1 control API and invokes one validated helper for SuperSource
bank preparation. The helper accepts only bank `a` or `b`, layouts 2–8, and
unique source numbers 1–8.

```sh
python3 server.py --core http://127.0.0.1:18085 \
  --program-url http://127.0.0.1:13089/mxl-program/ \
  --preview-url http://127.0.0.1:13089/mxl-preview/ \
  --multiview-url http://127.0.0.1:13089/mxl-multiview/ \
  --audio-meter-url http://127.0.0.1:9730 \
  --audio-mixer-url http://127.0.0.1:9740 \
  --nmos-node-url http://127.0.0.1:8020 \
  --nmos-registry-url http://127.0.0.1:8010 \
  --supersource-helper /home/guy/mxl-atem-skin/run-supersource-bank.sh
```

The server binds `127.0.0.1:9700` by default. Use an SSH tunnel for remote
operation; do not expose it directly to a public network.

`mxl_demo_sources.py` publishes two lightweight 1080p30 diagnostic bolt-ons:
moving `MXL Playout TC` and `MXL Keyed TC` flows with frame-counting timecode.
They make repeats, freezes, and monitor latency visible without relying on a
person speaking or moving. When `--core-url` is supplied, the process registers
both sources with a runtime-registration-capable Core; no Core restart or
configuration edit is needed:

```sh
python3 mxl_demo_sources.py \
  --domain /mxl-domain \
  --playout-flow 00000000-0000-4000-8000-000000000001 \
  --keyed-flow 00000000-0000-4000-8000-000000000002 \
  --core-url http://127.0.0.1:18085
```

The diagnostic publishers require GStreamer with the MXL plugin plus NumPy and
OpenCV. They are reference bolt-ons, not replacements for a playlist-aware
file player or an HTML/fill-key graphics service.

The read-only engineering dashboard is available at `/health`. It separates
current faults from lifetime measurements, checks aggregate and per-source
ring motion, shows Program/Preview assignments and timing-window telemetry,
and reports the configured NMOS Node/Registry inventory. Fault counters show a
delta from the moment the browser page was opened so an old test event does
not masquerade as a new incident. The operator skin links to this page through
the **ENGINEERING HEALTH** button; switching controls remain on the main page.

`mxl_audio_meter.py` reads the native planar Float32 audio rings without
decoding video or inserting a media conversion. Its loopback-only `/meters`
endpoint drives stereo VU bars over each multiview tile. This demonstrates
source audio presence and silence; it does not by itself claim Core
audio-follow-video or end-to-end A/V synchronization.

`mxl_audio_mixer.py` reads the available stereo ZoomISO Float32 audio flows and
writes one stable Program Audio MXL flow. The browser surface exposes eight
faders, ON/mute buttons, a master fader, and two modes: AFV follows direct
Program cuts, while Manual builds a multi-participant mix for ME/SuperSource
layouts. The Program WebRTC publisher subscribes to this stable output so its
H.264 + Opus session stays intact across video cuts.

For a ZoomISO return-feed instance, the stable Program video and Program Audio
flows must share the same NMOS group-hint prefix. Set that prefix with
`--group-hint`; the default produces `ZoomISOMXLProgram:Audio` to match the
reference Core Program video's `ZoomISOMXLProgram:Video`. Matching the prefix
makes ZoomISO present one combined Program input instead of separate "video
only" and "audio only" entries.

The mixer preserves ZoomISO's native 48 kHz MXL timestamps, explicitly converts
planar/interleaved layout as needed, uses non-dropping one-second input queues
and a 250 ms live-mixer latency, and keeps the output at 48 kHz. The resampler
operates at unity when the input already matches the Program bus.
Its loopback `/state` response includes the output-ring head index, progress
age, pipeline generation, and restart count. A bus/error and progress watchdog
rebuilds the GStreamer pipeline if the Program Audio ring stops advancing; a
running container alone is not considered proof of healthy audio.

`mxl_program_reclock.py` is the isolated Phase 1 Program Master candidate. It
reads the stable Program Audio mix, measures its timeline against monotonic
time from cumulative received sample frames on a steady observation cadence
(rather than trusting regressing source PTS or mistaking scheduler bursts for
clock movement), and uses a bounded software PLL to make small continuous tempo
changes. Source PTS regressions remain visible as a separate counter. The first
candidate applies rate feed-forward only: GStreamer
queue levels remain visible but do not drive correction because they are not a
sample-accurate FIFO occupancy measurement.
High-quality resampling returns the result to exact 48 kHz; byte-accumulated
timestamps are rooted at one monotonic epoch and the sink is clock-paced. The
candidate publishes a different flow and group hint, so evaluation cannot
silently replace the meeting-return input. Its `/state` endpoint exposes input
drift, correction ratio, queue depth, ignored scheduler-burst samples, output
production rate, PTS discontinuities, progress, restart/reopen count, and
hard-correction count. These exact output measurements run on the existing sink
pad and do not add an MXL reader. If the upstream MXL
ring is replaced, the candidate rebuilds only its reader pipeline and advances
the new retimestamping epoch by the audio it already emitted, preventing a PTS
rewind across recovery.

## Confidence monitoring

The Program and Preview monitors embed MediaMTX WebRTC players with
`--program-url` and `--preview-url`. Each player is fed by one MXL-to-H.264
adapter subscribed to Core's corresponding stable output flow. Video remains
on the media plane; the skin carries control and status only.

The named 2-up through 8-up presets are starting points for the composition
matrix; every box remains independently assignable. MIX is deliberately
disabled: Core currently routes one selected source per output, so a real
dissolve requires the planned dual-source sampler rather than a browser-side
simulation.

For an eight-source operator multiview, follow the proven first-switcher
pattern: use low-rate JPEG thumbnails for the source grid, or produce one
MXL-native multiview flow and encode that single result at the edge. Do not
create eight independent browser encodes.

`mxl_operator_multiview.py` is the low-rate operator-wall entry point. Deploy
it beside the proven isolated compositor modules, run the eight readers at
15 fps, publish the resulting v210 wall as one MXL flow, and attach one WebRTC
adapter to that flow. The skin overlays clickable 4x2 hit areas with Program
and Preview tally borders; clicking a tile arms that source in Preview.

Core must include these two stable sources before live SuperSource preparation:

```ini
source=supersource-a|2cbb8ee0-b44a-5f31-a5d0-00000000000a|SuperSource A
source=supersource-b|2cbb8ee0-b44a-5f31-a5d0-00000000000b|SuperSource B
```

Each A/B bank is a persistent warm container controlled only over a loopback
HTTP endpoint. A Look change replaces the compositor child inside the inactive
bank instead of tearing down and recreating Docker. A bank currently assigned
to Program or Preview is never changed. This preserves the on-air flow while
the alternate composition is prepared and attached off air.

The first migration or recovery start warms the container once. Measured warm
2-up through 8-up changes complete in roughly 0.9–1.8 seconds on the validated
Azure host, versus roughly 12–13 seconds when Docker was recreated for every
button press. The compositor must set `automatic-eos=false` on each live
`appsrc`; the scene supervisor also retries a transient fixed-flow handoff
without restarting its bank container.

## A/V reference

`mxl_av_reference_source.py` publishes paired frame-coded video and 48 kHz
event audio. `mxl_av_analyze.py` measures captures for cadence, sample
continuity, A/V offset, and drift. `av_reference.py` holds the shared timeline
and is standard-library only. See [the A/V reference guide](../../docs/AV_REFERENCE.md).
