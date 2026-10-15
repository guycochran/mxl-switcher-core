# A/V reference source and analyzer

Status: implemented and unit-tested offline. **Not yet run on a live MXL
domain.** No live A/V-sync result exists until a capture is analyzed and
recorded.

This replaces listening as the acceptance measurement for audio cadence,
sample continuity, and lip sync through Core, Program Audio, and meeting
returns.

## Timeline contract

One 30 fps video grain `g` corresponds to 48 kHz audio sample
`s = g * 1600`. The publisher starts `g` at the host TAI clock's current grain,
so the content identifies wall time as well as order.

| Element | Content |
| --- | --- |
| Frame code | Two rows of 22 blocks at the bottom of the picture: white/black reference blocks, then `g mod 4,294,967,280` (32 bits) and CRC-8. The modulus is a multiple of 120, so event identity survives the wrap. |
| Flash patch | White on frames where `g % 30 == 0`, black otherwise. |
| Audio event | A 20 ms raised-cosine tone burst starting on the first sample of every flash frame. Its frequency (600, 900, 1200, or 1500 Hz) is `(g // 30) % 4`, so pairing is unambiguous for offsets up to +/-2 s. |
| Counter (lossless mode only) | The right channel carries `(s mod 2^20 - 2^19) / 2^22`, exact in Float32. Use it to find sample gaps, duplicates, and regressions on gain-only MXL paths. |
| Human-readable | TAI time-of-day timecode, a moving bar, and the current event code. |

Use `--audio-mode meeting` (the default) for any path that may downmix,
encode, or apply AGC. **Never route lossless mode into a meeting**: the
counter channel is a full-scale sawtooth.

The frame code decodes after downscaling to 480x270 and JPEG quality 20, and
CRC rejects blended or corrupted frames instead of misreading them.

## Publishing

```sh
python3 tools/mxl-atem-skin/mxl_av_reference_source.py \
  --domain /mxl-domain \
  --video-flow <new video flow id> --audio-flow <new audio flow id> \
  --group MXLAVReference --audio-mode meeting \
  [--core-url http://127.0.0.1:<port> --source-id av-reference]
```

One GStreamer pipeline writes both flows from one frame counter, with group
hints `<group>:Video` and `<group>:Audio`. With `--core-url`, it registers a
runtime source of kind `reference` and sets `audio_flow_id` to the paired
audio. Run it in its own container with its own flow identities, and remove
it by exact name. The publisher needs the same Python/GStreamer/OpenCV/MXL
plugin environment as `mxl_demo_sources.py`.

## Capturing

The analyzer needs the video and audio on one capture timeline:

- **Audience side**: a local recording of the destination meeting, or any
  capture ffmpeg can decode. Analyze it with `--media`.
- **Inside MXL**: one GStreamer pipeline with two `mxlsrc` branches muxed into
  one file. Two separately started pipelines do not share a timeline. Validate
  the exact capture pipeline on the lab before relying on it.
- **Raw**: 8-bit luma frames (`--video-raw`, `--width`, `--height`), one capture
  time per frame (`--video-pts`, or `--fps`), and interleaved Float32 48 kHz
  audio (`--audio-raw`, `--audio-start-s` for its first sample's capture time).

## Analyzing

```sh
python3 tools/mxl-atem-skin/mxl_av_analyze.py --media capture.mkv --series
python3 tools/mxl-atem-skin/mxl_av_analyze.py --video-raw v.y8 --video-pts v.pts \
  --audio-raw a.f32 --audio-start-s 0.0 --counter-channel right
```

The JSON report contains:

- **video**: decoded and undecodable frames, step histogram, repeats, skips,
  regressions, and the longest freeze in frames and seconds;
- **audio/sync**: detected events, unmatched events, the minimum/median/maximum
  A/V offset and its range, and drift in ppm. The offset is
  `audio_time - video_time`, so a positive value means audio is late;
- **counter** (when requested): estimated gain, continuous steps, duplicate
  samples, gap events and samples, and regressions.

The exit status is 0 when every threshold passes and 1 when any fails, with
reasons listed. Default limits: audio lead <= 40 ms, audio lag <= 60 ms, offset
range <= 20 ms, drift <= 100 ppm, undecodable frames <= 1%, at least three
pairs, and no counter discontinuities. Every limit is a CLI flag; record the
limits used with each result.

Repeats and skips are expected when the capture rate differs from 30 fps or a
meeting reduces frame rate. Interpret them against the capture rate; a long
freeze or any regression is a finding.

## Tests

```sh
python3 -m unittest tools/mxl-atem-skin/test_av_reference.py
```

The tests run on the standard library only. They cover:

- frame-code round trips at several capture sizes, with low contrast and noise;
- CRC rejection of corrupted frames;
- exact burst placement and Float32 counter exactness;
- onset detection through gain and noise;
- counter gaps, duplicates, regressions, and wrap;
- video repeats, skips, freezes, and regressions;
- drift measurement and pairing beyond half a second; and
- full CLI runs on synthetic captures, including one with 100 ms of audio lag
  and a frozen video segment.

## Next live gate

1. Publish the reference in an isolated container with new flow identities.
2. Capture it directly from MXL. This is the zero-offset baseline and the
   lossless sample-counter check.
3. Register it with the isolated Core candidate, take it to Program, and capture
   Program video with its paired audio.
4. Feed the meeting return and analyze an audience-side recording.
5. Record offsets, drift, cadence, and counters for 10 minutes before changing
   any latency or presentation-offset setting.
