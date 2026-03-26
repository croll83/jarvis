#!/bin/bash
# Stop the market daemon
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PID_FILE="$PROJECT_DIR/state/daemon.pid"

if command -v pm2 &> /dev/null; then
  pm2 stop trading-daemon-v2 2>/dev/null && echo "Daemon stopped (pm2)" || echo "No pm2 process found"
else
  if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    kill "$PID" 2>/dev/null && echo "Daemon stopped (PID: $PID)" || echo "Process not running"
    rm -f "$PID_FILE"
  else
    echo "No PID file found"
  fi
fi
