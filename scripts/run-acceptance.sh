#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
  echo "usage: run-acceptance.sh CONFIG OUTPUT_DIRECTORY" >&2
  exit 2
fi

config=$1
output_dir=$2
core_binary=${MXL_CORE_BINARY:-./build/mxl-core-live}

mkdir -p "$output_dir"
core_log="$output_dir/core.log"
events_log="$output_dir/events.log"
health_log="$output_dir/health.jsonl"
state_log="$output_dir/state.jsonl"
resource_log="$output_dir/resources.log"
control_log="$output_dir/control.jsonl"
summary_log="$output_dir/summary.log"

"$core_binary" --check-config "$config" >"$output_dir/config-check.log"
"$core_binary" --config "$config" >"$core_log" 2>&1 &
core_pid=$!
echo "$core_pid" >"$output_dir/core.pid"

cleanup() {
  if kill -0 "$core_pid" 2>/dev/null; then
    kill -TERM "$core_pid" 2>/dev/null || true
    wait "$core_pid" 2>/dev/null || true
  fi
  if [ "${events_pid:-}" ]; then
    kill -TERM "$events_pid" 2>/dev/null || true
  fi
  if [ "${monitor_pid:-}" ]; then
    kill -TERM "$monitor_pid" 2>/dev/null || true
  fi
}
trap cleanup HUP INT TERM

control_host=$(sed -n 's/^control_host=//p' "$config")
control_port=$(sed -n 's/^control_port=//p' "$config")
base_url="http://$control_host:$control_port/api/mxl/v1"

# Give the media loop time to create outputs and bind control.
attempt=0
while ! curl -fsS --connect-timeout 1 "$base_url/state" >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ] || ! kill -0 "$core_pid" 2>/dev/null; then
    echo "control endpoint did not become ready" >>"$summary_log"
    cleanup
    exit 1
  fi
  sleep 1
done

(
  while kill -0 "$core_pid" 2>/dev/null; do
    curl -sS -N --connect-timeout 2 --max-time 300 \
      "$base_url/events" >>"$events_log" 2>>"$output_dir/events-errors.log" || true
    sleep 1
  done
) &
events_pid=$!

(
  counter=0
  while kill -0 "$core_pid" 2>/dev/null; do
    timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    health=$(curl -fsS --connect-timeout 2 --max-time 3 "$base_url/health" ||
      echo '{"error":"health request failed"}')
    state=$(curl -fsS --connect-timeout 2 --max-time 3 "$base_url/state" ||
      echo '{"error":"state request failed"}')
    echo "{\"timestamp\":\"$timestamp\",\"health\":$health}" >>"$health_log"
    echo "{\"timestamp\":\"$timestamp\",\"state\":$state}" >>"$state_log"
    ps -o pid=,etime=,pcpu=,rss=,stat= -p "$core_pid" \
      | sed "s/^/$timestamp /" >>"$resource_log" || true

    if [ $((counter % 6)) -eq 0 ]; then
      receipt=$(curl -fsS -X POST --connect-timeout 2 --max-time 3 \
        "$base_url/take" || echo '{"error":"take request failed"}')
      echo "{\"timestamp\":\"$timestamp\",\"receipt\":$receipt}" \
        >>"$control_log"
    fi
    counter=$((counter + 1))
    sleep 10
  done
) &
monitor_pid=$!

set +e
wait "$core_pid"
core_exit=$?
set -e

kill -TERM "$events_pid" "$monitor_pid" 2>/dev/null || true
wait "$events_pid" 2>/dev/null || true
wait "$monitor_pid" 2>/dev/null || true

finished=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "finished=$finished core_exit=$core_exit" >>"$summary_log"
tail -n 5 "$core_log" >>"$summary_log"
trap - HUP INT TERM
exit "$core_exit"
