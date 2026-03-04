#!/usr/bin/with-contenv bashio
# =============================================================================
# JARVIS HA Memory Service — Add-on Entrypoint
# Legge la config dall'API Supervisor (bashio) e lancia main.py
# =============================================================================
# NOTA: #!/usr/bin/with-contenv bashio carica le env var di S6 (incluso
# SUPERVISOR_TOKEN) prima di eseguire lo script.
# =============================================================================

bashio::log.info "Starting JARVIS HA Memory Service..."

# ===========================================================================
# Read addon options via bashio
# ===========================================================================
export LOCATION_ID=$(bashio::config 'location_id')
export AI_BACKEND=$(bashio::config 'ai_backend')

# --- Local mode (Ollama) ---
export OLLAMA_URL=$(bashio::config 'ollama_url')
export EMBEDDING_MODEL=$(bashio::config 'embedding_model')
export SUMMARY_MODEL=$(bashio::config 'summary_model')

# --- API mode (OpenRouter + Gemini) ---
export OPENROUTER_API_KEY=$(bashio::config 'openrouter_api_key')
export GEMINI_API_KEY=$(bashio::config 'gemini_api_key')
export OPENROUTER_SUMMARY_MODEL=$(bashio::config 'openrouter_summary_model')

# --- Tuning ---
export SUMMARY_TEMPERATURE=$(bashio::config 'summary_temperature')
export SUMMARY_TIMEOUT=$(bashio::config 'summary_timeout')
export EMBEDDING_TIMEOUT=$(bashio::config 'embedding_timeout')
export SKIP_ENTITY_PREFIXES=$(bashio::config 'skip_entity_prefixes')
export SKIP_ENTITY_SUFFIXES=$(bashio::config 'skip_entity_suffixes')

# ===========================================================================
# Home Assistant connection — automatica via Supervisor
# ===========================================================================
export HA_URL="http://supervisor/core"
export HA_TOKEN="${SUPERVISOR_TOKEN:-}"

bashio::log.info "Location: ${LOCATION_ID}"
bashio::log.info "AI Backend: ${AI_BACKEND}"
bashio::log.info "HA URL: ${HA_URL}"

if [ -z "${HA_TOKEN}" ]; then
    bashio::log.warning "SUPERVISOR_TOKEN not set — HA WebSocket auth may fail"
fi

if [ "${AI_BACKEND}" = "api" ]; then
    bashio::log.info "Mode: Cloud (OpenRouter + Gemini embeddings)"
    if [ -z "${OPENROUTER_API_KEY}" ]; then
        bashio::log.warning "OPENROUTER_API_KEY is empty — summarization will fail"
    fi
    if [ -z "${GEMINI_API_KEY}" ]; then
        bashio::log.warning "GEMINI_API_KEY is empty — embeddings will fail"
    fi
else
    bashio::log.info "Mode: Local (Ollama at ${OLLAMA_URL})"
fi

# ===========================================================================
# Data paths — addon persistent storage
# ===========================================================================
DATA_DIR="/data"
export DB_PATH="${DATA_DIR}/ha_memory.db"
export CHROMA_PATH="${DATA_DIR}/chroma"
export SERVICE_PORT=8100

mkdir -p "${DATA_DIR}"

bashio::log.info "DB: ${DB_PATH}"
bashio::log.info "ChromaDB: ${CHROMA_PATH}"
bashio::log.info "Port: ${SERVICE_PORT}"

# ===========================================================================
# Launch
# ===========================================================================
bashio::log.info "Launching main.py..."
exec python3 /app/main.py
