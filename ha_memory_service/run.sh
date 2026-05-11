#!/bin/sh
# =============================================================================
# JARVIS HA Memory Service — Add-on Entrypoint
# Legge la config da /data/options.json (scritto dal Supervisor)
# =============================================================================
# NON usa bashio/s6-overlay — compatibile con init: false (Docker tini)
# SUPERVISOR_TOKEN è iniettato come env var dal Supervisor
# =============================================================================

set -e

log() { echo "[$(date '+%H:%M:%S')] $1: $2"; }

log "INFO" "Starting JARVIS HA Memory Service..."

# ===========================================================================
# Read addon options from /data/options.json (written by HA Supervisor)
# ===========================================================================
OPTIONS_FILE="/data/options.json"
if [ ! -f "$OPTIONS_FILE" ]; then
    log "ERROR" "Options file not found: $OPTIONS_FILE"
    exit 1
fi

# Parse JSON with python (always available in the base image)
eval "$(python3 -c "
import json, sys
with open('$OPTIONS_FILE') as f:
    opts = json.load(f)
for key, val in opts.items():
    # Convert key to uppercase for env var
    env_key = key.upper()
    print(f'export {env_key}=\"{val}\"')
")"

# ===========================================================================
# Home Assistant connection — automatica via Supervisor
# ===========================================================================
export HA_URL="http://supervisor/core"
export HA_TOKEN="${SUPERVISOR_TOKEN:-}"

log "INFO" "Location: ${LOCATION_ID}"
log "INFO" "AI Backend: ${AI_BACKEND}"
log "INFO" "HA URL: ${HA_URL}"

if [ -z "${HA_TOKEN}" ]; then
    log "WARNING" "SUPERVISOR_TOKEN not set — HA WebSocket auth may fail"
fi

if [ "${AI_BACKEND}" = "api" ]; then
    log "INFO" "Mode: Cloud (OpenRouter + cloud embeddings)"
else
    log "INFO" "Mode: Local (Ollama at ${OLLAMA_URL})"
fi

# ===========================================================================
# Data paths — addon persistent storage
# ===========================================================================
DATA_DIR="/data"
export DB_PATH="${DATA_DIR}/ha_memory.db"
export SERVICE_PORT=8100

mkdir -p "${DATA_DIR}"

log "INFO" "DB: ${DB_PATH}"
log "INFO" "Port: ${SERVICE_PORT}"

# ===========================================================================
# Launch
# ===========================================================================
log "INFO" "Launching main.py..."
exec python3 /app/main.py
