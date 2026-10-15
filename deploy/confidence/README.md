# Browser confidence monitoring (lab profile)

This profile restores the ATEM-style skin's browser monitors on a single
production node. It is not the cloud demo profile. Production MXL stays at
full quality; only the browser monitoring copies are reduced.

```text
Core Program (1080p30 v210) --> mxl-confidence-program   --> 960x540 @ 30 fps \
Core Preview (1080p30 v210) --> mxl-confidence-preview   --> 640x360 @ 15 fps  >-- RTMP --> mxl-confidence-mediamtx --> WebRTC
six catalog sources         --> mxl-confidence-multiview --> 1280x720 @ 10 fps /       (loopback)                  /mxl-program/
                                                                                                                  /mxl-preview/
                                                                                                                  /mxl-multiview/
```

## How the encoders stay cheap and safe

`tools/mxl-atem-skin/mxl_confidence_encoder.py`:

- Reads only the newest grain of each flow, **in place**, through the MXL SDK
  (`mxl_direct.py`). There is no 5.5 MB frame copy, no GStreamer colour
  converter, and no scaler. The pixels a monitor needs are taken by exact v210
  decimation: 1/2 (960x540), 1/3 (640x360) or 1/6 (320x180 wall tiles).
- Validates every read after decimation. A grain the writer may have lapped is
  discarded and the previous picture is kept.
- Checks a flow's data file before opening a reader. The lab's SDK build
  crashes (SIGSEGV) when asked to open a flow that does not exist. Missing or
  stalled sources show a NO SIGNAL slate.
- Encodes constrained-baseline H.264 (`x264enc` ultrafast, zero-latency, no
  B-frames) and publishes RTMP to MediaMTX on loopback. Publish failures are
  logged and retried.
- The multiview wall is 1280x720 with a 4x2 grid of 320x180 tiles and
  40-pixel label bars. This is the geometry the skin's click overlay
  expects. Tiles follow the Core source catalog order, and the skin's overlay
  now does the same.

Production flows are only read. The encoders make no Core, routing, or meeting
changes.

## MediaMTX

`mediamtx.yml` pins the configuration for MediaMTX 1.21.1:

- every listener binds to loopback: RTMP 1935, WebRTC HTTP 8889, ICE-TCP 8189,
  and the API on 9997;
- RTSP, HLS, SRT, and the MoQ server (enabled by default in 1.21) are off;
- only the three monitor paths are accepted; and
- WebRTC uses ICE over TCP on 127.0.0.1 so it works through an SSH tunnel.

## Containers

All five components carry explicit names. The four containers use
`--restart unless-stopped`, and each encoder is capped at two CPUs.

| Name | Image | Role |
| --- | --- | --- |
| `mxl-confidence-mediamtx` | `bluenviron/mediamtx:1.21.1` | RTMP ingest, WebRTC playback |
| `mxl-confidence-program` | lab MXL image | Core Program at 960x540, 30 fps, 2.5 Mb/s |
| `mxl-confidence-preview` | lab MXL image | Core Preview at 640x360, 15 fps, 1 Mb/s |
| `mxl-confidence-multiview` | lab MXL image | six-source wall at 1280x720, 10 fps, 1.5 Mb/s |

The skin is started with:

```sh
--program-url http://127.0.0.1:13089/mxl-program/
--preview-url http://127.0.0.1:13089/mxl-preview/
--multiview-url http://127.0.0.1:13089/mxl-multiview/
```

## Operator access

Forward the skin, the WebRTC player pages, and ICE-TCP from the operator
machine:

```sh
ssh -N -L 13010:127.0.0.1:9710 -L 13089:127.0.0.1:8889 -L 8189:127.0.0.1:8189 USER@LAB_HOST
```

Open `http://127.0.0.1:13010/`. Port 8189 must be forwarded as well as 13089;
without it the player page loads but no media flows.

## Measured cost (2026-10-15, shared 20-vCPU lab node already about 65% busy)

| Component | Delivered (decoded read-back) | Work per frame | CPU | RSS |
| --- | --- | --- | --- | --- |
| Program 960x540 | 30.08 fps (loop 22-30 fps under host contention) | 41 ms | about 120-125% | 51 MiB |
| Preview 640x360 | 15.02 fps | 19 ms | about 28-32% | 40 MiB |
| Multiview 1280x720 | 10.0 fps | 16 ms | about 25-38% | 57 MiB |
| MediaMTX | n/a | n/a | about 6-8% | 24 MiB |
| Host total | busy 65.0% to 71.7% | | | |

v210 decimation on this CPU costs 9.2 ms (factor 2), 1.9 ms (factor 3) and
0.3 ms (factor 6) per 1080p grain. Most of the Program cost is H.264 encoding
at 30 fps. On a contended node it is borderline: dedicate CPU to it, or lower
its cadence, if smooth 30 fps confidence matters.

Torn reads were rare: zero to four per minute per source, each one replaced
by the previous picture.
