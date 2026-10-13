# MXL ATEM-style Skin

A localhost-only operator surface for MXL Core. It provides separate Program
and Preview buses for inputs 1–8, frame-boundary CUT/TAKE, and an A/B-bank
SuperSource builder for arbitrary 2-up through 8-up compositions.

The web server is deliberately outside the media path. It proxies only the
bounded Core v1 control API and invokes one validated helper for SuperSource
bank preparation. The helper accepts only bank `a` or `b`, layouts 2–8, and
unique source numbers 1–8.

```sh
python3 server.py --core http://127.0.0.1:18085 \
  --supersource-helper /home/guy/mxl-atem-skin/run-supersource-bank.sh
```

The server binds `127.0.0.1:9700` by default. Use an SSH tunnel for remote
operation; do not expose it directly to a public network.

Core must include these two stable sources before live SuperSource preparation:

```ini
source=supersource-a|2cbb8ee0-b44a-5f31-a5d0-00000000000a|SuperSource A
source=supersource-b|2cbb8ee0-b44a-5f31-a5d0-00000000000b|SuperSource B
```
