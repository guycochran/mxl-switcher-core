# Corporate and on-premises production

This path is for ballrooms, studios, campuses, houses of worship, and production
trucks. TAMS and public cloud services are not prerequisites.

## Expected system

- SDI/HDMI, NDI, SRT, RTSP, presentation, and playback sources enter through
  independent adapters.
- Core provides stable Program and Preview MXL flows.
- The reference Skin or another control surface provides M/E operation,
  SuperSource layouts, audio control, multiview, and health.
- Companion or another controller provides physical buttons and tally.
- Program exits through the outputs actually required for the event: normally
  SDI/HDMI, NDI, SRT, a local recorder, or a combination.

## First useful deployment

1. Core with two live sources and stable Program/Preview.
2. Reference Skin plus Companion.
3. One scheduled DeckLink or NDI Program output with embedded 48 kHz audio.
4. PowerPoint/screen and file-playback MXL sources.
5. EIC health showing signal, clock, buffer, output, and restart state.

The optional `corporate-on-prem` profile is maintained in
[`mxl-switcher-extensions`](https://github.com/guycochran/mxl-switcher-extensions/tree/main/profiles/corporate-on-prem).
