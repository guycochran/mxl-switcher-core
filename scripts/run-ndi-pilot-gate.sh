#!/bin/sh
set -eu

output_dir=${1:-"run-ndi-pilot-$(date -u +%Y%m%dT%H%M%SZ)"}
duration=${DURATION:-300}
interval=${INTERVAL:-10}
base_url=${BASE_URL:-http://127.0.0.1:18080/api/mxl/v1}

docker_call() {
  if docker info >/dev/null 2>&1; then
    docker "$@"
  else
    sudo -n docker "$@"
  fi
}

mkdir -p "$output_dir"
: >"$output_dir/samples.tsv"
: >"$output_dir/controls.jsonl"
: >"$output_dir/errors.log"

printf 'timestamp\tcore\tingress\tprogram_out\tpreview_out\tresources\thealth\tstate\n' \
  >>"$output_dir/samples.tsv"

started=$(date +%s)
deadline=$((started + duration))
next_take=$((started + 60))
samples=0
takes=0
monitor_errors=0

while [ "$(date +%s)" -lt "$deadline" ]; do
  timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  core=$(docker_call inspect --format '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}/{{.RestartCount}}' mxl-core-pilot 2>>"$output_dir/errors.log" || true)
  ingress=$(docker_call inspect --format '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}/{{.RestartCount}}' mxl-ndi-core-ingress 2>>"$output_dir/errors.log" || true)
  program_out=$(docker_call inspect --format '{{.State.Status}}/{{.RestartCount}}' mxl-ndi-core-program-out 2>>"$output_dir/errors.log" || true)
  preview_out=$(docker_call inspect --format '{{.State.Status}}/{{.RestartCount}}' mxl-ndi-core-preview-out 2>>"$output_dir/errors.log" || true)
  resources=$(docker_call stats --no-stream --format '{{.Name}}={{.CPUPerc}},{{.MemUsage}}' mxl-core-pilot mxl-core-pilot-source mxl-ndi-core-ingress mxl-ndi-core-program-out mxl-ndi-core-preview-out 2>>"$output_dir/errors.log" | tr '\n' ';' || true)
  health=$(curl -fsS --connect-timeout 2 --max-time 3 "$base_url/health" 2>>"$output_dir/errors.log" || true)
  state=$(curl -fsS --connect-timeout 2 --max-time 3 "$base_url/state" 2>>"$output_dir/errors.log" || true)
  if [ -z "$core" ] || [ -z "$ingress" ] || [ -z "$program_out" ] ||
     [ -z "$preview_out" ] || [ -z "$health" ] || [ -z "$state" ]; then
    monitor_errors=$((monitor_errors + 1))
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$timestamp" "$core" "$ingress" "$program_out" "$preview_out" \
    "$resources" "$health" "$state" >>"$output_dir/samples.tsv"
  samples=$((samples + 1))

  now=$(date +%s)
  if [ "$now" -ge "$next_take" ]; then
    receipt=$(curl -fsS -X POST --connect-timeout 2 --max-time 3 "$base_url/take" 2>>"$output_dir/errors.log" || true)
    if [ -z "$receipt" ]; then
      monitor_errors=$((monitor_errors + 1))
    else
      takes=$((takes + 1))
    fi
    printf '{"timestamp":"%s","receipt":%s}\n' "$timestamp" "${receipt:-null}" >>"$output_dir/controls.jsonl"
    next_take=$((next_take + 60))
  fi
  sleep "$interval"
done

finished=$(date -u +%Y-%m-%dT%H:%M:%SZ)
final_health=$(curl -fsS --connect-timeout 2 --max-time 3 "$base_url/health" 2>>"$output_dir/errors.log" || true)
final_state=$(curl -fsS --connect-timeout 2 --max-time 3 "$base_url/state" 2>>"$output_dir/errors.log" || true)
gate_failures=$monitor_errors
health_totals=${final_health%%,\"sources\"*}
for counter in invalid too_early too_late timeouts reader_reopens \
  sync_timeouts sync_too_early sync_too_late no_common_window; do
  value=$(printf '%s' "$health_totals" |
    sed -n "s/.*\"$counter\":\([0-9][0-9]*\).*/\1/p")
  if [ -z "$value" ] || [ "$value" -ne 0 ]; then
    gate_failures=$((gate_failures + 1))
    printf 'nonzero_or_missing_counter=%s value=%s\n' \
      "$counter" "${value:-missing}" >>"$output_dir/errors.log"
  fi
done
if [ "$core" != "running/healthy/0" ] ||
   [ "$ingress" != "running/healthy/0" ] ||
   [ "$program_out" != "running/0" ] ||
   [ "$preview_out" != "running/0" ]; then
  gate_failures=$((gate_failures + 1))
  printf 'unhealthy_final_container_state core=%s ingress=%s program=%s preview=%s\n' \
    "$core" "$ingress" "$program_out" "$preview_out" \
    >>"$output_dir/errors.log"
fi
{
  printf 'finished=%s samples=%s takes=%s monitor_errors=%s gate_failures=%s\n' \
    "$finished" "$samples" "$takes" "$monitor_errors" "$gate_failures"
  printf 'health=%s\nstate=%s\n' "$final_health" "$final_state"
} >"$output_dir/summary.txt"
touch "$output_dir/done"
if [ "$gate_failures" -ne 0 ]; then
  exit 1
fi
