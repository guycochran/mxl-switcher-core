# Program master and output-node plan

## Objective

Make one switched and composited MXL Program available simultaneously to a
second meeting, NDI, SRT, SDI/HDMI, WebRTC, recording, and RTMP/RTMPS streaming
without allowing any destination clock, codec, reconnect, or hardware failure
to disturb Core or another output.

The output system is attached to Core through stable MXL Program flows and the
control API. Codecs, network transports, and hardware drivers remain separate
processes. "Bolt on" therefore describes the operator and control experience,
not a new dependency in Core's frame-routing hot path.

## Lessons retained from the original MXL Switcher

The earlier switcher established several patterns worth preserving:

- Program video and Program audio are separate, stable MXL flows.
- A facility manifest pairs their identities and records their roles.
- Encoders read both Program essences explicitly.
- Destinations are independent processes and may run concurrently.
- SRT uses paced MPEG-TS with 1,316-byte payload alignment.
- Output processes expose health and can be restarted without recreating the
  upstream Program flows.

The earlier `mxl-egress.sh` is useful historical evidence, but not the new
master design. It reads an already encoded WebRTC/RTSP rendition, regenerates
timestamps, and then re-encodes. That creates an unnecessary serial dependency
and can hide clock errors. The replacement output nodes read the normalized MXL
Program master directly.

## Non-negotiable media contract

```text
MXL sources -> Core / compositor / audio mixer -> Program clock recovery
                                                  | stable Program Master video
                                                  | stable Program Master audio
                                                  +-------------------------------+
                                                  | Meeting return                |
                                                  | NDI                           |
                                                  | SRT                           |
                                                  | SDI / HDMI                    |
                                                  | RTMP / RTMPS                  |
                                                  | WebRTC                        |
                                                  | recorder / TAMS               |
                                                  +-------------------------------+
```

The Program master is the only timing authority for output nodes:

- video: progressive 1920x1080 v210 at 30000/1001 unless the facility profile
  explicitly selects another rate;
- audio: interleaved Float32 PCM, 48,000 samples/second, stereo in the first
  profile;
- one shared monotonic epoch for video PTS, audio PTS, and control events;
- stable video and audio flow UUIDs that survive source changes and downstream
  failures;
- bounded buffers with visible occupancy and no unbounded latency growth;
- video and audio are paired by manifest and group hint, never by label guessing.

No output may use `use_wallclock_as_timestamps`, `genpts`, or an equivalent
timestamp-regeneration switch as its primary synchronization strategy. An
output converts from the Program epoch into the destination timebase
deliberately and reports the conversion.

## Why clock recovery comes first

Live ZoomISO measurement found valid 48 kHz stereo buffers with continuous
1 ms PTS increments, no clipping, no duplicated non-silent blocks, and steady
Zoom transport packets. The source timeline nevertheless advanced about
0.087% faster than the consuming host's monotonic clock. The mixed Program
inherited the same rate error. A strict 48 kHz microphone consumer must
periodically discard the surplus, which is audible as scratches or skips.

Program clock recovery must therefore precede every distribution adapter. It
will:

1. measure input audio and video rate against the Program monotonic clock;
2. absorb burst delivery in bounded jitter buffers;
3. adaptively resample audio to exactly 48,000 output samples/second;
4. frame-synchronize video to the selected facility rate;
5. create PTS from an output sample/frame accumulator rooted at one epoch;
6. hold or repeat video when necessary while continuing silent/valid audio;
7. publish drift, resampling ratio, buffer depth, corrections, and A/V skew.

Hard sample deletion is a fallback alarm condition, not the normal correction
mechanism. Normal drift is removed by a slowly changing resampling ratio.

## Destination profiles

### Meeting return

- Read Program Master video and audio.
- Publish a paired MXL camera/microphone source for the meeting-return instance.
- Preserve 30000/1001 video and exact 48 kHz stereo audio.
- Keep camera and microphone enable/mute under operator control.
- Acceptance includes a participant-side receive capture, not only local flow
  and sender statistics.

### NDI

- Submit Program video and Float32 audio with a common NDI timecode.
- Use 48 kHz audio and accumulated sample counts; at 30000/1001 the adapter
  must handle the non-integer number of audio samples per video frame.
- Prefer audio buffers near half a video-frame duration while preserving the
  exact long-term sample count.
- Apply the documented NDI-to-SDI/EBU level mapping and expose gain/headroom.

### SRT

- Encode H.264 or H.265 video and AAC-LC 48 kHz audio into MPEG-TS.
- Pace the mux from Program timestamps; SRT timestamp-based packet delivery
  reproduces sender timing and cannot repair a badly paced source.
- Use 1,316-byte live payloads and a latency profile based on measured RTT and
  jitter.
- Expose RTT, retransmissions, loss, send-buffer occupancy, reconnects, and
  continuity-counter errors.

### SDI / HDMI

- Use DeckLink scheduled playback rather than immediate best-effort writes.
- Output v210/UYVY in the selected hardware mode with embedded 48 kHz PCM.
- Begin with stereo; allow 8/16-channel facility profiles later.
- Preroll both essences and use the DeckLink hardware reference clock. Expose
  reference/genlock state plus buffered video frames and audio sample frames.

### RTMP / RTMPS and YouTube

- Encode directly from Program Master, not from the WebRTC rendition.
- Use a destination profile for codec, GOP, rate control, and audio format.
- The YouTube stereo profile uses H.264, a two-second GOP, AAC at 128 kb/s, and
  44.1 kHz as currently documented by YouTube Live. The single high-quality
  48 kHz to 44.1 kHz conversion occurs only in this adapter.
- Use RTMPS and expose encoder queue, reconnects, bytes sent, and platform
  stream-health status.

### WebRTC and recording

- WebRTC encodes Program Master video and Opus 48 kHz audio independently of
  the meeting-return and RTMP outputs.
- Recorders preserve Program PTS and capture-time metadata. A TAMS adapter may
  segment a compressed rendition or, later, group native MXL grains.

## Output-node API and state

Output nodes share a small contract:

- `name`, `kind`, `enabled`, `state`, and `revision`;
- Program Master video/audio flow IDs and selected profile;
- start, stop, restart, and profile-update commands;
- readiness separate from liveness;
- last error and bounded restart count;
- destination-specific statistics;
- secrets referenced by external secret name, never returned by the API.

The Core API may aggregate this state for a Skin and Companion, but it does not
own output processes. NMOS discovery advertises Program Master and applicable
output senders. Routing and output activation remain explicit operations.

## Required telemetry

Every output records:

- input and output audio sample rate measured against monotonic time;
- clock drift in parts per million;
- adaptive resampling ratio;
- inserted/dropped samples and hard-correction events;
- audio jitter-buffer depth, underruns, overruns, and silence state;
- input/output video cadence, repeats, drops, and frame-buffer depth;
- current and worst A/V skew;
- encode latency and queue depth;
- process CPU/RSS, restarts, and destination connection state.

Measurements are counters and ranges. A failure is raised for an active stall,
unbounded buffer, hard correction above policy, sustained A/V skew, restart,
or destination-specific transport failure.

## Acceptance method

Every adapter passes the same layers:

1. **Synthetic clock test:** tone plus moving timecode under injected positive
   and negative drift, burst delivery, and short stalls.
2. **Local loopback:** decode/capture the destination output and compare cadence,
   audio continuity, A/V skew, content motion, and channel mapping.
3. **Destination test:** receive through the real meeting, NDI receiver, SRT
   receiver, DeckLink loopback, or platform ingest.
4. **Soak:** 10 minutes, 30 minutes, then eight hours with active switching,
   layouts, audio changes, and one controlled output restart.

Minimum initial gates:

- zero unexpected process or container restarts;
- zero audio underruns/overruns after stabilization;
- zero unexplained hard audio corrections;
- average output rate exactly matches the profile within measurement error;
- A/V skew remains bounded to the declared policy;
- one output may be stopped, failed, or restarted without changing Program
  Master identity or another output's counters.

## Delivery phases

### Phase 0 — contract and measurement

- Land this architecture and a versioned output-profile schema.
- Add reusable clock-rate, buffer-depth, correction, and A/V-skew telemetry.
- Capture the existing meeting-return drift as a regression fixture.

### Phase 1 — Program Master clock recovery

- Build the audio PLL/adaptive resampler and video frame synchronizer.
- Publish stable paired Program Master flows.
- Add synthetic drift/burst/stall tests and a live measurement harness.

### Phase 2 — meeting-to-meeting proof

- Feed the corrected Program Master into the return meeting.
- Capture the audience-side receive result with audio.
- Prove direct cuts and 2-up through 8-up layouts, then add slides, screen share,
  and file playback as ordinary MXL sources.

### Phase 3 — software/network outputs

- Add the shared output-node supervisor and API.
- Implement NDI, SRT, WebRTC, recording, and YouTube RTMPS profiles.
- Put output state, meters, and alarms into the Skin and Companion.

### Phase 4 — hardware output

- Implement DeckLink scheduled SDI/HDMI playback with embedded audio.
- Validate free-run and genlocked modes plus cable loopback.

### Phase 5 — operational acceptance

- Exercise simultaneous meeting, NDI, SRT, SDI, WebRTC, and RTMP outputs.
- Establish CPU/GPU sizing profiles and graceful degradation priority.
- Complete 10-minute, 30-minute, and eight-hour acceptance gates.

## Graceful degradation order

Program and audio continuity are the product. Under resource pressure:

1. reduce multiview refresh rate;
2. reduce previews and thumbnails;
3. reduce non-critical output resolution/bitrate;
4. shed optional outputs according to operator priority;
5. never silently change Program audio rate, channel mapping, or clock source.

