#!/usr/bin/env bash
# =============================================================================
# JARVIS Stack Manager
# =============================================================================
# Gestisce l'ordine di avvio dello stack Docker locale (Atomman) e verifica
# la disponibilità dei servizi audio sul GX10 (Parakeet STT + Qwen3-TTS).
#
# STT e TTS ora girano sul GX10 DGX Spark (via Tailscale):
#   - Parakeet STT: http://100.98.187.12:7865
#   - Qwen3-TTS:    http://100.98.187.12:9880
# Whisper e XTTSv2 DEPRECATI — rimossi dallo stack Atomman.
#
# Con restart automatico disabilitato, se un container muore lo stack va
# riavviato con: jarvis.sh restart
#
# Usage:
#   jarvis.sh start       Avvio ordinato (Ollama → model load → GX10 check → rest)
#   jarvis.sh stop        Ferma tutti i container
#   jarvis.sh down        Stop + rimuove container
#   jarvis.sh restart     Stop + Start
#   jarvis.sh status      Stato container + VRAM + GX10 services
#   jarvis.sh vram        Solo VRAM usage
#   jarvis.sh logs [svc]  Segui i log (opzionale: nome servizio)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"

# Modello da pre-caricare in VRAM (deve matchare quello usato in ai_engines.py)
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:3b}"
OLLAMA_URL="http://localhost:11434"

# GX10 DGX Spark (audio services via Tailscale)
GX10_IP="${GX10_IP:-100.98.187.12}"
PARAKEET_URL="http://${GX10_IP}:7865"
QWEN3_TTS_URL="http://${GX10_IP}:9880"

# ── Colori ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log()  { echo -e "${CYAN}[JARVIS]${NC} $*"; }
ok()   { echo -e "${GREEN}[  OK  ]${NC} $*"; }
warn() { echo -e "${YELLOW}[ WARN ]${NC} $*"; }
err()  { echo -e "${RED}[ERROR ]${NC} $*" >&2; }

dc() { docker compose -f "$COMPOSE_FILE" "$@"; }

# ── Helper: aspetta che un URL risponda ───────────────────────────────────────
wait_for() {
    local url="$1" name="$2" timeout="${3:-120}"
    local elapsed=0
    printf "  Aspetto %s..." "$name"
    while ! curl -sf --max-time 3 "$url" > /dev/null 2>&1; do
        if (( elapsed >= timeout )); then
            echo ""
            err "$name non risponde dopo ${timeout}s"
            return 1
        fi
        sleep 2
        elapsed=$((elapsed + 2))
        printf "."
    done
    echo ""
    ok "$name pronto (${elapsed}s)"
}

# ── Helper: verifica GPU layers dal log Ollama ────────────────────────────────
check_gpu_layers() {
    # Cerca la riga "offloaded X/Y layers to GPU" nei log recenti
    local offload_line
    offload_line=$(docker logs jarvis_ollama --tail 50 2>&1 | grep 'offloaded.*layers to GPU' | tail -1 || true)

    if [ -z "$offload_line" ]; then
        warn "Non trovo info layer offload nei log"
        return 1
    fi

    # Estrai X e Y da "offloaded X/Y layers to GPU"
    local gpu_layers total_layers
    gpu_layers=$(echo "$offload_line" | grep -oP 'offloaded \K\d+')
    total_layers=$(echo "$offload_line" | grep -oP 'offloaded \d+/\K\d+')

    if [ "$gpu_layers" = "$total_layers" ]; then
        ok "Modello caricato: ${gpu_layers}/${total_layers} layers su GPU"

        # Mostra VRAM usata dal modello
        local vram_line
        vram_line=$(docker logs jarvis_ollama --tail 50 2>&1 | grep 'msg="total memory"' | tail -1 || true)
        if [ -n "$vram_line" ]; then
            local total_size
            total_size=$(echo "$vram_line" | grep -oP 'size="[^"]*"')
            log "  Memoria modello totale: ${total_size}"
        fi
        return 0
    else
        warn "Solo ${gpu_layers}/${total_layers} layers su GPU"
        warn "Possibili cause: altro processo GPU attivo, o VRAM insufficiente"
        return 1
    fi
}

# ── Pre-load modello in VRAM ──────────────────────────────────────────────────
preload_model() {
    log "Pre-loading ${BOLD}${OLLAMA_MODEL}${NC} in VRAM..."

    # Verifica che il modello esista, altrimenti pull
    if ! curl -sf "${OLLAMA_URL}/api/tags" | grep -q "\"${OLLAMA_MODEL}\""; then
        warn "Modello ${OLLAMA_MODEL} non trovato, pulling..."
        docker exec jarvis_ollama ollama pull "${OLLAMA_MODEL}"
    fi

    # Request dummy per forzare il caricamento in VRAM
    # stream=false per aspettare che completi prima di continuare
    local http_code
    http_code=$(curl -sf -o /dev/null -w "%{http_code}" \
        "${OLLAMA_URL}/api/generate" \
        -d "{
            \"model\": \"${OLLAMA_MODEL}\",
            \"prompt\": \"ok\",
            \"options\": {\"num_predict\": 1, \"num_ctx\": ${OLLAMA_NUM_CTX:-32768}},
            \"stream\": false
        }" 2>/dev/null) || true

    if [ "$http_code" = "200" ]; then
        # Verifica layers (non-fatal se parziale — continua comunque)
        sleep 2
        check_gpu_layers || true
    else
        err "Pre-load fallito (HTTP ${http_code:-timeout})"
        err "Controlla: docker logs jarvis_ollama --tail 30"
        return 1
    fi
}

# ── Mostra VRAM ───────────────────────────────────────────────────────────────
show_vram() {
    if ! command -v nvidia-smi &> /dev/null; then
        warn "nvidia-smi non disponibile"
        return
    fi

    echo -e "\n${BOLD}VRAM:${NC}"
    nvidia-smi --query-gpu=memory.used,memory.total,memory.free \
        --format=csv,noheader,nounits 2>/dev/null | while IFS=, read -r used total free; do
        used=$(echo "$used" | tr -d ' ')
        total=$(echo "$total" | tr -d ' ')
        free=$(echo "$free" | tr -d ' ')
        local pct=$((used * 100 / total))
        echo -e "  ${BOLD}${used}${NC} / ${total} MiB usati (${pct}%) — ${free} MiB liberi"
    done

    echo -e "\n${BOLD}Processi GPU:${NC}"
    nvidia-smi --query-compute-apps=pid,process_name,used_gpu_memory \
        --format=csv,noheader 2>/dev/null | while IFS=, read -r pid name mem; do
        name=$(echo "$name" | tr -d ' ' | xargs basename 2>/dev/null || echo "$name")
        mem=$(echo "$mem" | tr -d ' ')
        printf "  %-8s %-30s %s MiB\n" "$pid" "$name" "$mem"
    done
}

# =============================================================================
# COMANDI
# =============================================================================

cmd_start() {
    echo -e "\n${BOLD}${CYAN}━━━ JARVIS Stack Start ━━━${NC}\n"
    log "Strategia: Ollama unico servizio GPU locale"
    log "           STT + TTS su GX10 DGX Spark (via Tailscale)\n"

    # ── Fase 1: Ollama (unico processo GPU locale) ────────────────────────
    log "═══ Fase 1: Ollama ═══"
    dc up -d ollama
    wait_for "${OLLAMA_URL}/api/tags" "Ollama API" 60
    preload_model
    echo ""

    # ── Fase 2: Verifica servizi audio su GX10 ───────────────────────────
    log "═══ Fase 2: GX10 Audio Services (Parakeet STT + Qwen3-TTS) ═══"
    dc up -d fastembed
    if wait_for "${PARAKEET_URL}/health" "Parakeet STT (GX10)" 15; then
        ok "Parakeet STT disponibile su GX10"
    else
        warn "Parakeet STT non raggiungibile — input vocale non funzionerà"
    fi
    if wait_for "${QWEN3_TTS_URL}/health" "Qwen3-TTS (GX10)" 15; then
        ok "Qwen3-TTS disponibile su GX10"
    else
        warn "Qwen3-TTS non raggiungibile — output vocale non funzionerà"
    fi
    echo ""

    # ── Fase 3: Servizi non-GPU ───────────────────────────────────────────
    # NOTA: chromadb e mem0-server NON sono piu' qui — vivono nel repo
    # croll83/mem0-stack (deploy separato). L'orchestrator li usa via MEM0_BASE_URL.
    log "═══ Fase 3: Orchestrator + DB + Ontology ═══"
    dc up -d orchestrator postgres mongo adminer
    wait_for "http://localhost:5000/health" "Orchestrator" 60
    echo ""

    ok "Stack JARVIS avviato!"
    show_vram
    echo ""
}

cmd_stop() {
    echo -e "\n${BOLD}${CYAN}━━━ JARVIS Stack Stop ━━━${NC}\n"
    dc stop
    ok "Stack fermato"
    echo ""
}

cmd_down() {
    echo -e "\n${BOLD}${CYAN}━━━ JARVIS Stack Down ━━━${NC}\n"
    dc down
    ok "Stack rimosso"
    echo ""
}

cmd_restart() {
    cmd_stop
    cmd_start
}

cmd_status() {
    echo -e "\n${BOLD}${CYAN}━━━ JARVIS Stack Status ━━━${NC}\n"
    dc ps

    echo -e "\n${BOLD}GX10 Audio Services:${NC}"
    if curl -sf --max-time 3 "${PARAKEET_URL}/health" > /dev/null 2>&1; then
        ok "Parakeet STT (${PARAKEET_URL})"
    else
        err "Parakeet STT (${PARAKEET_URL}) — non raggiungibile"
    fi
    if curl -sf --max-time 3 "${QWEN3_TTS_URL}/health" > /dev/null 2>&1; then
        ok "Qwen3-TTS (${QWEN3_TTS_URL})"
    else
        err "Qwen3-TTS (${QWEN3_TTS_URL}) — non raggiungibile"
    fi

    show_vram
    echo ""
}

cmd_vram() {
    show_vram
    echo ""
}

cmd_logs() {
    local service="${1:-}"
    if [ -n "$service" ]; then
        dc logs -f --tail=50 "$service"
    else
        dc logs -f --tail=20
    fi
}

# =============================================================================
# MAIN
# =============================================================================

usage() {
    echo -e "${BOLD}JARVIS Stack Manager${NC}"
    echo ""
    echo "Usage: $(basename "$0") <comando>"
    echo ""
    echo "Comandi:"
    echo "  start       Avvio ordinato (Ollama → GX10 check → rest)"
    echo "  stop        Ferma tutti i container"
    echo "  down        Stop + rimuove container"
    echo "  restart     Stop + Start"
    echo "  status      Stato container + VRAM"
    echo "  vram        Solo VRAM usage"
    echo "  logs [svc]  Segui i log (opzionale: nome servizio)"
    echo ""
    echo "Variabili:"
    echo "  OLLAMA_MODEL   Modello da pre-caricare (default: qwen3.5:4b)"
}

case "${1:-}" in
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    down)    cmd_down ;;
    restart) cmd_restart ;;
    status)  cmd_status ;;
    vram)    cmd_vram ;;
    logs)    cmd_logs "${2:-}" ;;
    -h|--help|help) usage ;;
    *)
        usage
        exit 1
        ;;
esac
