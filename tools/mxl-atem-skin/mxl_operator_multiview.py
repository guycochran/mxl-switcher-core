#!/usr/bin/env python3
"""Run the isolated-reader MXL compositor as a low-cost operator multiview.

This entry point keeps the proven independent reader supervision while dropping
each source to the wall cadence before scaling and colour conversion. Eight
browser encoders become one native MXL wall and one edge WebRTC encode.

Deploy beside ``mxl_compositor.py`` and ``mxl_isolated_compositor.py``.
"""

from __future__ import annotations

import json
import signal

from mxl_isolated_compositor import IsolatedCompositor, Reader, parser


def low_rate_description(self: Reader) -> str:
    """Build a reader which drops cadence before its expensive transforms."""
    return (
        f"mxlsrc domain={self.args.domain} video-flow-id={self.source.flow_id} "
        "automatic-eos=false "
        "! queue max-size-buffers=3 leaky=downstream "
        "! videorate drop-only=true "
        f"! video/x-raw,framerate={self.args.rate} "
        "! videoconvert n-threads=2 "
        "! videoscale method=1 add-borders=false "
        f"! video/x-raw,format=BGRA,width={self.tile.width},"
        f"height={self.tile.video_height},framerate={self.args.rate} "
        f"! videobox bottom=-{self.tile.bar_height} fill=black "
        f"! video/x-raw,format=BGRA,width={self.tile.width},"
        f"height={self.tile.height},framerate={self.args.rate} "
        f"! gdkpixbufoverlay location={self.asset} offset-y={self.tile.video_height} "
        "! queue max-size-buffers=2 leaky=downstream "
        "! appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
    )


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if not 1 <= len(args.input) <= 8:
        raise SystemExit("one to eight --input values are required")
    Reader.description = low_rate_description
    compositor = IsolatedCompositor(args)
    signal.signal(signal.SIGTERM, lambda *_: compositor.stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: compositor.stop_event.set())
    print(json.dumps({
        "event": "launch",
        "mode": "operator_multiview",
        "inputs": len(args.input),
        "output": f"{args.width}x{args.height}@{args.rate}",
    }), flush=True)
    try:
        compositor.start()
        return compositor.supervise()
    except Exception as error:
        compositor.fatal = f"startup:{error}"
        compositor.stop()
        print(json.dumps({"event": "fatal", "reason": compositor.fatal}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
