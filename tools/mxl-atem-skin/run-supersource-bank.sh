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
else
  OUTPUT_ID="2cbb8ee0-b44a-5f31-a5d0-00000000000b"
fi

mapfile -t INPUTS < <(sed -e '/^[[:space:]]*#/d' -e '/^[[:space:]]*$/d' "$INPUTS_FILE")
[[ "${#INPUTS[@]}" -ge 8 ]] || { echo "eight configured inputs are required" >&2; exit 1; }
declare -A SEEN=()
ARGS=()
for SOURCE in "$@"; do
  [[ "$SOURCE" =~ ^[1-8]$ ]] || { echo "source must be 1 through 8" >&2; exit 2; }
  [[ -z "${SEEN[$SOURCE]:-}" ]] || { echo "duplicate source $SOURCE" >&2; exit 2; }
  SEEN[$SOURCE]=1
  ARGS+=(--input "${INPUTS[$((SOURCE - 1))]}")
done

sudo docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
for _remove_attempt in $(seq 1 20); do
  sudo docker inspect "$CONTAINER" >/dev/null 2>&1 || break
  sleep 0.1
done
set +e
timeout 20s sudo docker run -d --name "$CONTAINER" --restart=no \
  -v "${DOMAIN_VOLUME}:/mxl-domain" -v "${BASE}:/opt/compositor" \
  "$IMAGE" python3 /opt/compositor/mxl_isolated_compositor.py \
  --domain /mxl-domain "${ARGS[@]}" \
  --output-flow-id "$OUTPUT_ID" \
  --output-label "MXL SUPERSOURCE ${BANK^^} ${COUNT}-UP" \
  --assets-dir "/opt/compositor/supersource-${BANK}-assets" \
  --reader-stale-seconds 8 >/dev/null
RUN_RESULT=$?
set -e
if [[ "$RUN_RESULT" -ne 0 ]]; then
  STATE="$(sudo docker inspect -f '{{.State.Status}}|{{.RestartCount}}' "$CONTAINER" 2>/dev/null || true)"
  if [[ "$STATE" != "running|0" ]]; then
    sudo docker logs --tail 40 "$CONTAINER" >&2 || true
    echo "unable to start $CONTAINER (docker result $RUN_RESULT, state $STATE)" >&2
    exit 1
  fi
fi

sleep 12
STATE="$(sudo docker inspect -f '{{.State.Status}}|{{.RestartCount}}' "$CONTAINER" 2>/dev/null || true)"
if [[ "$STATE" == "running|0" ]]; then
  printf '{"bank":"%s","layout":%s,"state":"ready"}\n' "$BANK" "$COUNT"
  exit 0
fi
sudo docker logs --tail 40 "$CONTAINER" >&2 || true
exit 1
