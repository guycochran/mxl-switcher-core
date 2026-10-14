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
video cuts. That prevents treating this run as a clean acceptance gate.

The next gate must correlate those recovered video events with cut timing,
measure content-level lip sync rather than timestamp proximity alone, exercise
manual multi-participant mixing, and run staged 10-minute, 30-minute, and
eight-hour durations. Program Audio remains a candidate until those checks
pass.

No participant identity, meeting credential, private address, flow UUID,
host name, or raw operational log is included here.
