# MXL ATEM-style Skin

A localhost-only operator surface for MXL Core. It provides separate Program
and Preview buses for inputs 1–8, frame-boundary CUT/TAKE, and an A/B-bank
SuperSource builder for arbitrary 2-up through 8-up compositions.

The web server is deliberately outside the media path. It proxies only the
bounded Core v1 control API and invokes one validated helper for SuperSource
bank preparation. The helper accepts only bank `a` or `b`, layouts 2–8, and
unique source numbers 1–8.

```sh
python3 server.py --core http://127.0.0.1:18085 \
  --program-url http://127.0.0.1:13089/mxl-program/ \
  --preview-url http://127.0.0.1:13089/mxl-preview/ \
  --multiview-url http://127.0.0.1:13089/mxl-multiview/ \
  --supersource-helper /home/guy/mxl-atem-skin/run-supersource-bank.sh
```

The server binds `127.0.0.1:9700` by default. Use an SSH tunnel for remote
operation; do not expose it directly to a public network.

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
