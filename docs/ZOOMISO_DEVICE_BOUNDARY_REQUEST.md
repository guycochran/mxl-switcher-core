# ZoomISO meeting-return integration questions

This is the smallest engineering interface needed to finish a broadcast-grade
MXL Program return through ZoomISO Cloud. It does not require ZoomISO source
code or proprietary implementation details. Timing telemetry or a supported
control boundary is enough.

## What is already proven

- The incoming Program pair is one exact MXL timeline: 1080p30 v210 video and
  48 kHz float32 stereo audio.
- The pair is sample/frame aligned immediately before ZoomISO.
- The ZoomISO footer selects one MXL or NDI source for both its virtual camera
  and virtual microphone.
- ZoomISO logs show 48 kHz stereo-to-mono conversion and audio submission.
- The audience-side failure is growing drift, not a fixed delay: a measured
  comparison reached approximately +9,400 ppm audio lag while Core stayed
  clean and the host retained CPU and memory headroom.
- An observer row set to ZoomISO's default 1080p29.97 adds about 1,000 ppm
  against a 30 fps reference, so our gate now requires 1080p30. That removes a
  measurement confounder but does not explain the larger drift.

The open-source reference contract and deterministic simulator are described
in [the paired-clock meeting-return adapter](MEETING_RETURN_ADAPTER.md).

## Questions for the ZoomISO engineering team

### Audio device path

1. Is audio pushed into the Zoom Meeting SDK, pulled by a device callback, or
   handed through PulseAudio before the SDK consumes it?
2. Which clock schedules each send/callback: MXL timestamps, host monotonic
   time, PulseAudio, the Zoom SDK, or another device clock?
3. What block size or range of block sizes is requested or submitted?
4. Are media timestamps supplied with audio blocks? If so, what epoch and
   units do they use?
5. What happens on underrun and overrun: silence, repeat, drop, buffering,
   reset, or back-pressure?
6. Is any sample-rate conversion or clock recovery applied after the logged
   48 kHz float32-stereo to 48 kHz signed-16-mono conversion?

### Video device path

1. Is video pushed or requested on a callback?
2. Which clock determines send cadence, and are MXL grain timestamps retained
   or replaced?
3. Are 30 fps and 29.97 fps distinct device modes end to end?
4. What is the buffering/drop/repeat policy when the input cadence and Zoom's
   requested cadence differ?

### Pairing and observability

1. Do the virtual camera and microphone share any clock, epoch, scheduler, or
   thread, or do they advance independently?
2. Can ZoomISO expose read-only counters for:
   - audio blocks and samples submitted;
   - video frames submitted;
   - callback/send intervals;
   - queue depth and maximum depth;
   - audio underrun/overrun and video drop/repeat;
   - current audio/video media timestamps; and
   - device restart or clock-reset events?
3. Can a local API report the active footer source, the paired audio/video flow
   identities, and the actual capture/output format rather than only the
   configured value?
4. Is there a supported place to apply one shared Program clock and a bounded
   audio resampling ratio before the camera and microphone enter Zoom?

## Minimal useful telemetry contract

A read-only endpoint at ZoomISO is sufficient for initial integration. Field
names can differ; the important part is that they come from the actual device
boundary and share one observation time.

```json
{
  "observed_monotonic_ns": 0,
  "video": {
    "frames_sent": 0,
    "last_media_timestamp_ns": 0,
    "send_interval_ms": {"last": 0, "mean": 0, "max": 0},
    "queue_depth_frames": 0,
    "drops": 0,
    "repeats": 0
  },
  "audio": {
    "blocks_sent": 0,
    "samples_sent": 0,
    "sample_rate": 48000,
    "last_media_timestamp_ns": 0,
    "send_interval_ms": {"last": 0, "mean": 0, "max": 0},
    "queue_depth_samples": 0,
    "underruns": 0,
    "overruns": 0
  },
  "device_resets": 0
}
```

## Integration options

Any one of these unlocks the next engineering step:

1. **Best:** ZoomISO applies the shared-clock contract at its virtual-device
   boundary.
2. **Good:** ZoomISO exposes its device/send clock and counters so an upstream
   MXL adapter can follow the observed clock with bounded resampling.
3. **Diagnostic:** ZoomISO exposes only timestamp/cadence telemetry; this still
   lets the independent audience gate localize drift without guesswork.

The acceptance boundary remains the same in all cases: an independent
audience-side reference capture must pass offset, drift, continuity, cadence,
restart, and resource limits. Simulator or sender-side success alone is not an
end-to-end claim.

