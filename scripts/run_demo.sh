#!/usr/bin/env bash
# Bring the whole system up and stream a herd through it.
#
# Starts the cloud service, the edge gateway and the collar simulator, then
# leaves the dashboard running at http://localhost:4000 so the results can be
# inspected. Ctrl-C stops everything.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
LOG_DIR="$ROOT/.demo-logs"
ANIMALS="${ANIMALS:-12}"
DAYS="${DAYS:-3}"
SPEED="${SPEED:-40000}"

[[ -x "$PY" ]] || { echo "No virtualenv. Run: make setup"; exit 1; }
[[ -f "$ROOT/artifacts/models/vetra_dx_edge.tflite" || -f "$ROOT/artifacts/models/sklearn_model.joblib" ]] \
  || { echo "No trained model. Run: make data && make train"; exit 1; }

mkdir -p "$LOG_DIR"
PIDS=()

cleanup() {
  echo ""
  echo "Stopping ..."
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  echo "Stopped. Logs are in $LOG_DIR"
}
trap cleanup EXIT INT TERM

port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && exec 3>&- ; }

for port in 4000 5001; do
  if port_busy "$port"; then
    echo "Port $port is already in use. Run 'make stop' first."
    exit 1
  fi
done

# Build the dashboard once so the cloud service can serve it.
if [[ ! -d "$ROOT/dashboard/dist" ]]; then
  echo "Building the dashboard ..."
  (cd dashboard && npm run build >"$LOG_DIR/dashboard-build.log" 2>&1) \
    || { echo "Dashboard build failed; see $LOG_DIR/dashboard-build.log"; exit 1; }
fi

echo "Starting the cloud service on :4000 ..."
node server/src/index.js >"$LOG_DIR/cloud.log" 2>&1 &
PIDS+=($!)

for _ in {1..30}; do port_busy 4000 && break; sleep 0.5; done
port_busy 4000 || { echo "Cloud service failed to start; see $LOG_DIR/cloud.log"; exit 1; }

echo "Starting the edge gateway on :5001 ..."
"$PY" edge/agent.py --http-port 5001 --cloud-url http://localhost:4000 --sync-interval 5 \
  >"$LOG_DIR/gateway.log" 2>&1 &
PIDS+=($!)

for _ in {1..40}; do port_busy 5001 && break; sleep 0.5; done
port_busy 5001 || { echo "Gateway failed to start; see $LOG_DIR/gateway.log"; exit 1; }

echo ""
echo "Streaming $ANIMALS animals over $DAYS simulated days ..."
"$PY" edge/simulator.py --transport rest --animals "$ANIMALS" --days "$DAYS" \
  --speed "$SPEED" --batch-size 64 2>&1 | grep -vE "^\s*$" || true

echo ""
echo "Waiting for the gateway to finish syncing ..."
sleep 12

echo ""
echo "==================================================================="
echo "  Dashboard   http://localhost:4000"
echo "  Cloud API   http://localhost:4000/api/summary"
echo "  Gateway     http://localhost:5001/status"
echo "==================================================================="
echo ""
echo "Alerts raised:"
grep -E "OPENED|ESCALATED" "$LOG_DIR/gateway.log" | sed 's/.*vetra.agent: /  /' || echo "  (none yet)"
echo ""
echo "Press Ctrl-C to stop."

wait
