#!/usr/bin/env bash
# =============================================================================
# JARVIS Stack Manager
# =============================================================================
# Gestisce l'ordine di avvio per garantire che Ollama carichi il modello
# con tutta la VRAM disponibile (33/33 GPU layers) PRIMA che XTTS e Whisper
# occupino la loro porzione.
#
# Con restart automatico disabilitato, se un container muore lo stack va
# riavviato con: jarvis.sh restart
#
# Usage:
#   jarvis.sh start       Avvio ordinato (Ollama → model load → GPU services → rest)
#   jarvis.sh stop        Ferma tutti i container
#   jarvis.sh down        Stop + rimuove container
#   jarvis.sh restart     Stop + Start
#   jarvis.sh status      Stato container + VRAM
#   jarvis.sh vram        Solo VRAM usage
#   jarvis.sh logs [svc]  Segui i log (opzionale: nome servizio)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"

# Modello da pre-caricare in VRAM (deve matchare quello usato in ai_engines.py)
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen3.5:4b}"
OLLAMA_URL="http://localhost:11434"

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
    # Cerca l'ultimo log "model weights" per CPU — se assente, tutto è su GPU
    local cpu_line
    cpu_line=$(docker logs jarvis_ollama --tail 30 2>&1 | grep 'msg="model weights"' | grep 'device=CPU' | tail -1 || true)
    if [ -z "$cpu_line" ]; then
        ok "Modello caricato: 33/33 layers su GPU"
        return 0
    else
        local cpu_size
        cpu_size=$(echo "$cpu_line" | grep -oP 'size="[^"]*"' | head -1)
        warn "Modello parzialmente su CPU (${cpu_size}) — VRAM insufficiente"
        warn "Possibili cause: altro processo GPU attivo, o XTTS/Whisper partiti troppo presto"
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
            \"options\": {\"num_predict\": 1, \"num_ctx\": 3072, \"think\": false},
            \"stream\": false
        }" 2>/dev/null) || true

    if [ "$http_code" = "200" ]; then
        # Verifica layers
        sleep 1
        check_gpu_layers
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
    log "Strategia: Ollama carica modello PRIMO (33/33 GPU layers)"
    log "           poi XTTS + Whisper prendono la VRAM rimanente\n"

    # ── Fase 1: Ollama (unico processo GPU) ───────────────────────────────
    log "═══ Fase 1: Ollama ═══"
    dc up -d ollama
    wait_for "${OLLAMA_URL}/api/tags" "Ollama API" 60
    preload_model
    echo ""

    # ── Fase 2: Servizi GPU (XTTS + Whisper) ──────────────────────────────
    log "═══ Fase 2: XTTS + Whisper ═══"
    dc up -d xtts whisper
    # XTTS ha start_period 120s al primo avvio (download modello)
    wait_for "http://localhost:8890/docs" "XTTS" 240
    wait_for "http://localhost:9000/docs" "Whisper" 120
    echo ""

    # ── Fase 3: Servizi non-GPU ───────────────────────────────────────────
    log "═══ Fase 3: Orchestrator + DB + Ontology ═══"
    dc up -d
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
    echo "  start       Avvio ordinato (Ollama → GPU services → rest)"
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
