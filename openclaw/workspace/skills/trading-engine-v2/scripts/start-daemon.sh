#!/bin/bash
# Start the market daemon with pm2 or as a background process
# Usage: ./scripts/start-daemon.sh [--dry-run]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_DIR/state"

mkdir -p "$LOG_DIR"

DRY_RUN=""
if [ "$1" = "--dry-run" ]; then
  DRY_RUN="DRY_RUN=true"
  echo "Starting in DRY-RUN mode"
fi

# Check if pm2 is available
if command -v pm2 &> /dev/null; then
  echo "Starting with pm2..."
  pm2 start "$PROJECT_DIR/daemon/market-daemon.mjs" \
    --name "trading-daemon-v2" \
    --interpreter node \
    --env "$DRY_RUN" \
    --log "$LOG_DIR/daemon-pm2.log" \
    --time
  pm2 save
  echo "Daemon started. Use 'pm2 logs trading-daemon-v2' to monitor."
else
  echo "Starting as background process (install pm2 for production use)..."
  cd "$PROJECT_DIR"
  $DRY_RUN nohup node daemon/market-daemon.mjs >> "$LOG_DIR/daemon.log" 2>&1 &
  echo $! > "$LOG_DIR/daemon.pid"
  echo "Daemon started (PID: $(cat "$LOG_DIR/daemon.pid")). Logs: $LOG_DIR/daemon.log"
fi
