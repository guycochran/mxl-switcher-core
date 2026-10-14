#!/usr/bin/env bash
set -eu

# Read-only preflight for the community demo profile. It does not create MXL
# readers, change routing, restart containers, or touch the meeting.

DOCKER_CMD=${DOCKER_CMD:-"sudo docker"}
SKIN_URL=${SKIN_URL:-http://127.0.0.1:9710}
WEBRTC_URL=${WEBRTC_URL:-http://127.0.0.1:8889}

required_containers="
zoomiso
zoomiso-b
azure-zoomiso-core-candidate
mxl-supersource-a
mxl-supersource-b
mxl-operator-multiview
mxl-core-program-webrtc
mxl-core-preview-webrtc
mxl-multiview-webrtc
mxl-audio-mixer
"

failures=0
printf 'MXL demo preflight (read only)\n'

for name in $required_containers; do
  state=$($DOCKER_CMD inspect -f '{{.State.Status}}|{{.RestartCount}}' "$name" 2>/dev/null || true)
  if [ "$state" = "running|0" ]; then
    printf '  PASS  %-34s %s\n' "$name" "$state"
  else
    printf '  FAIL  %-34s %s\n' "$name" "${state:-missing}"
    failures=$((failures + 1))
  fi
done

for path in / /api/skin/state; do
  if curl -fsS --connect-timeout 2 --max-time 4 "${SKIN_URL}${path}" >/dev/null; then
    printf '  PASS  skin endpoint                     %s%s\n' "$SKIN_URL" "$path"
  else
    printf '  FAIL  skin endpoint                     %s%s\n' "$SKIN_URL" "$path"
    failures=$((failures + 1))
  fi
done

for path in /mxl-program/ /mxl-preview/ /mxl-multiview/; do
  if curl -fsS --connect-timeout 2 --max-time 4 "${WEBRTC_URL}${path}" >/dev/null; then
    printf '  PASS  confidence endpoint               %s%s\n' "$WEBRTC_URL" "$path"
  else
    printf '  FAIL  confidence endpoint               %s%s\n' "$WEBRTC_URL" "$path"
    failures=$((failures + 1))
  fi
done

if $DOCKER_CMD inspect mxl-program-reclock-candidate >/dev/null 2>&1; then
  printf '  WARN  experimental audio candidate is still attached\n'
else
  printf '  PASS  experimental audio candidate      absent\n'
fi

if [ "$failures" -ne 0 ]; then
  printf '\nNOT READY: %s required check(s) failed. Do not restart services during the show.\n' "$failures"
  exit 1
fi

printf '\nREADY: permanent demo path is present with zero container restarts.\n'
printf 'Open the Skin and keep Companion as the control fallback.\n'
