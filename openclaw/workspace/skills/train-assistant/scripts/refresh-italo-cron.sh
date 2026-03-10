#!/usr/bin/env bash
# Cron script: refresh Italo JWT tokens every 40 minutes.
# On failure, sends Telegram notification via OpenClaw.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="/tmp/italo-refresh.log"
CHAT_ID="172751380"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

result=$(/usr/bin/node "$SCRIPT_DIR/search-italo.mjs" --refresh-only 2>&1) && {
  echo "[$(ts)] OK" >> "$LOG"
} || {
  echo "[$(ts)] FAIL: $result" >> "$LOG"
  openclaw message send --channel telegram -t "$CHAT_ID" \
    -m "⚠️ Italo token refresh fallito: $result" --silent 2>/dev/null || true
}
