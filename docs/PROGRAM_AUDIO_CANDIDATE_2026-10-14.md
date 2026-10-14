# Program Audio candidate — 2026-10-14

This record describes a live candidate test, not an accepted production
profile.

## Configuration

A separately supervised audio process read four ZoomISO MXL stereo flows and
published one stable Program Audio Master. Audio-follow-video polled Core's
applied Program state and changed mixer-pad gain without replacing the output
flow. The output remained 48 kHz, two-channel Float32 and used the same NMOS
group-hint prefix as Program Video.

ZoomISO's own diagnostic log confirmed that its input and output sample rates
were both 48 kHz. Its conversion was S16LE interleaved to Float32 planar; no
44.1/48 kHz conversion was present.

## Measured result

- Four direct Program cuts selected the corresponding four audio channels.
- The output ring remained active with no pipeline restart.
- A 20-second concurrent timing sample received 19,997 buffers and 959,856
  stereo sample frames.
- The output timeline measured 1.53 ppm from wall time and had zero timestamp
  discontinuities.
- Audio was present at the final sample: approximately -28.3 dBFS RMS and
  -20.8 dBFS peak on both channels.
- The audio process used approximately 29% of one CPU and 21 MiB RSS in the
  sampled window.
- Program Video and Program Audio readers produced their first buffer within
  0.34 ms of each other. Arrival-paired PTS skew ranged from audio leading by
  27.25 ms to 0.58 ms, within one 30 fps video frame.
- The operator Skin's engineering-health response now includes Program Audio
  state, active AFV channel, output progress age, generation, and restart
  count.

## Remaining gate items

The combined cut/recording exercise ended with no active sustained alarm and
all invalid, timing, timeout, reader-reopen, synchronization,
no-common-window, and selected-input-regression counters at zero. Core did,
however, accumulate recovered sustained-unavailability events during repeated
video cuts while concurrent probes and software recording were active. A
follow-up direct-cut-only control advanced every source and added zero alarm
events. A CPU-isolated real-time recorder sample reduced, but did not eliminate,
the recovered events. This identifies co-located output encoding load as the
remaining isolation problem rather than AFV or direct-cut correctness. It still
prevents treating the combined run as a clean acceptance gate.

The next gate must correlate those recovered video events with cut timing,
measure content-level lip sync rather than timestamp proximity alone, exercise
manual multi-participant mixing, and run staged 10-minute, 30-minute, and
eight-hour durations. Program Audio remains a candidate until those checks
pass.

## Isolated 10-minute Program Master qualification

The clock-correct Program Audio stage subsequently passed an isolated
10-minute qualification alongside the stable 1920x1080p30 Program video flow.
The test did not change Core routing or the meeting-return mapping. It observed
the existing AFV mix, a separate reclocking candidate, Program video, Core
health, and permanent service state concurrently.

| Measure | Result |
| --- | ---: |
| Duration / resource samples | 600 seconds / 49 |
| Monitoring errors / exit status | 0 / 0 |
| Audio buffers / stereo sample frames | 579,995 / 27,839,760 |
| Audio timestamp step, minimum / median / p95 / maximum | 1 / 1 / 1 / 1 ms |
| Audio PTS discontinuities | 0 |
| Measured audio PTS drift | +6.38 ppm |
| Candidate pipeline restarts / hard sample corrections | 0 / 0 |
| Program video frames / average rate / drops | 18,047 / 30.00 fps / 0 |

The candidate ended healthy and clock-locked. Its final measured input drift
was -45.65 ppm, the controller applied +44.84 ppm correction, and the output
timeline remained continuous. Core's invalid, timing, timeout, reader-reopen,
synchronization, no-common-window, selected-index-regression, and sustained
alarm counters were unchanged at zero. The temporary candidate and probes were
removed; permanent ZoomISO, Core, Program mixer, and NMOS services remained
running with zero restarts.

| Service | CPU range | RSS range |
| --- | ---: | ---: |
| ZoomISO | 368.14-713.90% | 1.47-1.47 GiB |
| Core | 27.48-61.08% | 77.03-78.51 MiB |
| Program Audio mixer | 13.80-50.63% | 21.80-22.21 MiB |
| Program Audio reclocker | 15.85-46.46% | 20.21-21.36 MiB |

This closes the isolated 10-minute clock-continuity gate. It does not yet claim
content-level lip sync or an accepted audience-meeting return. The next release
gate is a controlled topology promotion in which the reclocked audio and stable
Program video share one discoverable group, followed by audience-side capture
and measured A/V synchronization.

No participant identity, meeting credential, private address, flow UUID,
host name, or raw operational log is included here.

## Preserved experimental tuning controls

The meeting-return investigation also produced three CLI controls that are
committed for continuation but are **not** accepted production settings:

- `mxl_audio_mixer.py --pipeline-latency-ms` makes the GStreamer mixer latency
  explicit while retaining the measured 250 ms default;
- `mxl_program_reclock.py --presentation-offset-ms` shifts the published audio
  presentation epoch without changing its sample cadence; and
- `mxl_program_reclock.py --unsynced-sink` permits a future-dated output
  timeline to be written without the sink waiting on that future clock time.

Focused tests cover the timeline arithmetic and generated pipeline properties.
These controls were motivated by audible artifacting and apparent return-path
lag, but no value has yet passed content-level A/V-sync or audience-meeting
acceptance. A continuation must measure the actual audience-side waveform and
moving timecode before selecting latency or presentation-offset values. Do not
promote the buffered or tuned return scripts solely because the processes are
healthy or their MXL heads advance.
