#!/usr/bin/env bash
set -euo pipefail

BANK="${1:-}"
COUNT="${2:-}"
shift 2 || true
[[ "$BANK" == "a" || "$BANK" == "b" ]] || { echo "bank must be a or b" >&2; exit 2; }
[[ "$COUNT" =~ ^[2-8]$ ]] || { echo "layout must be 2 through 8" >&2; exit 2; }
[[ "$#" -eq "$COUNT" ]] || { echo "source count does not match layout" >&2; exit 2; }

BASE="${MXL_COMPOSITOR_BASE:-/home/guy/mxl-compositor}"
INPUTS_FILE="${MXL_COMPOSITOR_INPUTS:-${BASE}/private-inputs.conf}"
DOMAIN_VOLUME="${MXL_DOMAIN_VOLUME:-zoomiso-mxl-azure_zoomiso-mxl-domain}"
IMAGE="${MXL_IMAGE:-mxl-lab:latest}"
CONTAINER="mxl-supersource-${BANK}"
if [[ "$BANK" == "a" ]]; then
  OUTPUT_ID="2cbb8ee0-b44a-5f31-a5d0-00000000000a"
  CONTROL_PORT=9721
else
  OUTPUT_ID="2cbb8ee0-b44a-5f31-a5d0-00000000000b"
  CONTROL_PORT=9722
fi

mapfile -t INPUTS < <(sed -e '/^[[:space:]]*#/d' -e '/^[[:space:]]*$/d' "$INPUTS_FILE")
[[ "${#INPUTS[@]}" -ge 8 ]] || { echo "eight configured inputs are required" >&2; exit 1; }
declare -A SEEN=()
SOURCES=()
for SOURCE in "$@"; do
  [[ "$SOURCE" =~ ^[1-8]$ ]] || { echo "source must be 1 through 8" >&2; exit 2; }
  [[ -z "${SEEN[$SOURCE]:-}" ]] || { echo "duplicate source $SOURCE" >&2; exit 2; }
  SEEN[$SOURCE]=1
  SOURCES+=("$SOURCE")
done

SOURCE_CSV="$(IFS=,; echo "${SOURCES[*]}")"
SOURCE_JSON="$(IFS=,; echo "${SOURCES[*]}")"
SCENE_JSON="{\"count\":${COUNT},\"sources\":[${SOURCE_JSON}]}"
STATE="$(sudo docker inspect -f '{{.State.Status}}|{{.RestartCount}}' "$CONTAINER" 2>/dev/null || true)"

# Normal path: the bank stays warm and only its internal compositor child is
# replaced. This removes Docker teardown/startup from an operator button press.
if [[ "$STATE" == "running|0" ]] && curl -fsS --max-time 1 "http://127.0.0.1:${CONTROL_PORT}/state" >/dev/null 2>&1; then
  curl -fsS --max-time 15 -H 'Content-Type: application/json' \
    --data "$SCENE_JSON" "http://127.0.0.1:${CONTROL_PORT}/scene"
  printf '\n'
  exit 0
fi

# One-time migration/recovery path. Subsequent scene changes use the fast path.
sudo docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
ALL_INPUT_ARGS=()
for INPUT in "${INPUTS[@]:0:8}"; do
  ALL_INPUT_ARGS+=(--input "$INPUT")
done

sudo docker run -d --name "$CONTAINER" --restart=unless-stopped \
  -p "127.0.0.1:${CONTROL_PORT}:9720" \
  -v "${DOMAIN_VOLUME}:/mxl-domain" -v "${BASE}:/opt/compositor" \
  "$IMAGE" python3 /opt/compositor/mxl_scene_supervisor.py \
  --bank "$BANK" --domain /mxl-domain "${ALL_INPUT_ARGS[@]}" \
  --output-flow-id "$OUTPUT_ID" \
  --assets "/opt/compositor/supersource-${BANK}-assets" \
  --compositor /opt/compositor/mxl_isolated_compositor.py \
  --rate 30000/1001 --control-port 9720 \
  --initial-count "$COUNT" --initial-sources "$SOURCE_CSV" >/dev/null

for _ready_attempt in $(seq 1 150); do
  if RESULT="$(curl -fsS --max-time 1 "http://127.0.0.1:${CONTROL_PORT}/state" 2>/dev/null)"; then
    if [[ "$RESULT" == *'"state":"ready"'* || "$RESULT" == *'"state": "ready"'* ]]; then
      printf '%s\n' "$RESULT"
      exit 0
    fi
  fi
  sleep 0.1
done

sudo docker logs --tail 60 "$CONTAINER" >&2 || true
echo "SuperSource did not reach a healthy advancing output clock" >&2
exit 1
