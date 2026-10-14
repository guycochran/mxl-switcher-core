# Recording, archive, and replay

Recording is an optional MXL consumer, not part of Core's routing loop. A
recorder can be added to either an on-premises or online system without changing
the switcher.

## Levels of recording

1. **File recorder:** a bounded, clock-correct Program recording for users who
   need a conventional deliverable.
2. **ISO recorder:** selected MXL sources recorded independently alongside
   Program.
3. **TAMS research path:** time-addressable recording first, followed by
   separately gated playback, trimming/export, and replay work.

The original switcher recorded and registered TAMS segments, but dependable
playback and trimming were not achieved. The new path therefore accepts the
recorder independently before attempting a reader or editor. If replay is later
accepted, its playout adapter will publish a new MXL source and Core will see it
as another routable source.

```text
Program Master -> TAMS recorder -> object store + timeline index
                                         |
                              separately gated reader
                                         |
                            separately gated trim/export
                                         |
                            optional future MXL replay
```

The optional `archive-replay` profile is maintained in
[`mxl-switcher-extensions`](https://github.com/guycochran/mxl-switcher-extensions/tree/main/profiles/archive-replay).
