# Recording, archive, and replay

Recording is an optional MXL consumer, not part of Core's routing loop. A
recorder can be added to either an on-premises or online system without changing
the switcher.

## Levels of recording

1. **File recorder:** a bounded, clock-correct Program recording for users who
   need a conventional deliverable.
2. **ISO recorder:** selected MXL sources recorded independently alongside
   Program.
3. **TAMS recorder:** time-addressable segments, live scrub, metadata-only
   clips, and replay while the event is still recording.

The TAMS path consumes paired Program Master video/audio, preserves their
timeline in registered Flow Segments, and publishes selected playback as a new
MXL source. Core only sees that replay as another routable source.

```text
Program Master -> TAMS recorder -> object store + timeline index
                                         |
                                scrub / mark / clip
                                         |
                              TAMS playout adapter
                                         |
                                         +-> MXL replay -> Preview -> Cut
```

The optional `archive-replay` profile is maintained in
[`mxl-switcher-extensions`](https://github.com/guycochran/mxl-switcher-extensions/tree/main/profiles/archive-replay).
