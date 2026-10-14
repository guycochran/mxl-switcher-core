# Online-only production

This path is for remote meetings, webinars, virtual events, and cloud-hosted
production. SDI hardware and TAMS are not prerequisites.

## Expected system

- ZoomISO, SRT, WebRTC, or other remote sources enter through independent
  adapters and become ordinary MXL flows.
- Core and compositors create Program, Preview, and 2-up through 8-up looks.
- Program Master clock recovery supplies exact-rate video and 48 kHz audio.
- Independent meeting-return, WebRTC, SRT, and RTMPS outputs serve audiences.
- The browser Skin, Companion, and EIC health page may be operated remotely.

## First useful deployment

1. Four native MXL meeting sources.
2. Core, Skin, SuperSource, audio mixer, and confidence multiview.
3. Clock-correct Program Master video and audio.
4. Audience-side meeting-return proof.
5. One RTMPS or SRT distribution output with destination health.

The optional `online-production` profile is maintained in
[`mxl-switcher-extensions`](https://github.com/guycochran/mxl-switcher-extensions/tree/main/profiles/online-production).
