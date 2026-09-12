#!/usr/bin/env bash
# gb10-mode — switch del GB10 fra modalita' LLM e creator (mutuamente esclusive).
#
# Il GB10 ha una pool di memoria UNIFICATA (~121 GiB) condivisa fra CPU e GPU.
# Saturarla blocca il kernel senza OOM ne' log — e' gia' successo tre volte
# (flash-next/docs/memory-incident-2026-09-04.md). Per questo le due modalita'
# non si sovrappongono MAI: si spegne del tutto quella uscente, si attende che
# la memoria sia davvero tornata libera, e solo allora si accende l'altra.
# In caso di dubbio lo script ABORTISCE invece di forzare.
#
#   llm      vllm UP    | comfyui DOWN   ace-step DOWN
#   creator  vllm DOWN  | comfyui UP     ace-step UP
#
# TTS (cosyvoice3), STT (parakeet) e la dashboard restano su in ENTRAMBE.
#
# Uso:
#   gb10-mode status [--json]     # sola lettura, sempre sicuro
#   gb10-mode llm                 # torna alla pipeline LLM
#   gb10-mode creator             # passa alla pipeline video/audio
#
# Lo switch e' sincrono e lungo (vllm carica ~98 GiB: diversi minuti). Il
# progresso viene scritto in STATE_FILE cosi' la dashboard puo' fare polling
# mentre lo script gira in background.
set -uo pipefail

FLASH_DIR="${FLASH_DIR:-/home/jarvis/flash-next}"
# /run e' root-only: lo script gira come jarvis (stesso utente della dashboard).
STATE_DIR="${STATE_DIR:-/var/lib/gb10-mode}"
[[ -d "$STATE_DIR" && -w "$STATE_DIR" ]] || STATE_DIR="${TMPDIR:-/tmp}/gb10-mode"
STATE_FILE="$STATE_DIR/state.json"
LOCK_FILE="$STATE_DIR/lock"
LOG_FILE="${LOG_FILE:-$FLASH_DIR/logs/gb10-mode.log}"

VLLM_CONTAINER="${VLLM_CONTAINER:-vllm-fn-tp1}"
VLLM_URL="${VLLM_URL:-http://127.0.0.1:30000}"
COMFY_URL="${COMFY_URL:-http://127.0.0.1:8188}"
SVC_COMFY="comfyui.service"
SVC_ACE="ace-step.service"
SVC_TTS="cosyvoice3-tts.service"
SVC_STT="parakeet-stt.service"
SVC_DASH="comfyui-dashboard.service"

# Soglie di memoria (GiB di MemAvailable) richieste PRIMA di accendere.
# vllm chiede ~72 GiB di pesi + overhead; comfyui+WAN arrivano a ~85 in punta.
MEM_FOR_VLLM="${MEM_FOR_VLLM:-80}"
MEM_FOR_CREATOR="${MEM_FOR_CREATOR:-60}"
MEM_WAIT_TIMEOUT="${MEM_WAIT_TIMEOUT:-240}"
VLLM_READY_TIMEOUT="${VLLM_READY_TIMEOUT:-900}"
COMFY_READY_TIMEOUT="${COMFY_READY_TIMEOUT:-180}"

mkdir -p "$STATE_DIR" 2>/dev/null || true
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

log() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*" | tee -a "$LOG_FILE" >&2; }

# ── stato ────────────────────────────────────────────────────────────────────

svc_active() { [[ "$(systemctl is-active "$1" 2>/dev/null)" == "active" ]]; }

vllm_container_up() {
    [[ -n "$(docker ps -q -f "name=^${VLLM_CONTAINER}$" 2>/dev/null)" ]]
}
vllm_ready()  { curl -sf --max-time 5 "$VLLM_URL/v1/models" >/dev/null 2>&1; }
comfy_ready() { curl -sf --max-time 5 "$COMFY_URL/system_stats" >/dev/null 2>&1; }

mem_available_gib() {
    awk '/^MemAvailable:/ {printf "%d", $2/1048576}' /proc/meminfo
}

# llm | creator | mixed | idle  — dedotta dai servizi reali, non da un file.
detect_mode() {
    local v=0 c=0
    vllm_container_up && v=1
    { svc_active "$SVC_COMFY" || svc_active "$SVC_ACE"; } && c=1
    if   (( v == 1 && c == 0 )); then echo llm
    elif (( v == 0 && c == 1 )); then echo creator
    elif (( v == 1 && c == 1 )); then echo mixed
    else echo idle
    fi
}

json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

state_write() {  # busy phase message
    local busy="$1" phase="$2" msg="$3"
    printf '{"mode":"%s","busy":%s,"phase":"%s","message":"%s","mem_available_gib":%s,"ts":"%s"}\n' \
        "$(detect_mode)" "$busy" "$(json_escape "$phase")" "$(json_escape "$msg")" \
        "$(mem_available_gib)" "$(date '+%Y-%m-%dT%H:%M:%S')" > "$STATE_FILE" 2>/dev/null || true
}

status_json() {
    local busy=false phase=idle msg=""
    # Un lock NON acquisibile significa "switch in corso" solo se il fd e'
    # valido: se il file non si e' potuto aprire, flock fallisce comunque e
    # dedurne "busy" sarebbe un falso positivo.
    if [[ "${LOCK_OK:-0}" == "1" ]] && ! flock -n 9 2>/dev/null; then
        busy=true; phase=switching
        [[ -f "$STATE_FILE" ]] && { cat "$STATE_FILE"; return; }
    fi
    printf '{"mode":"%s","busy":%s,"phase":"%s","message":"%s","mem_available_gib":%s,' \
        "$(detect_mode)" "$busy" "$phase" "$(json_escape "$msg")" "$(mem_available_gib)"
    printf '"services":{"vllm":{"container":%s,"ready":%s},"comfyui":{"active":%s,"ready":%s},' \
        "$(vllm_container_up && echo true || echo false)" \
        "$(vllm_ready && echo true || echo false)" \
        "$(svc_active "$SVC_COMFY" && echo true || echo false)" \
        "$(comfy_ready && echo true || echo false)"
    printf '"acestep":{"active":%s},"tts":{"active":%s},"stt":{"active":%s},"dashboard":{"active":%s}},' \
        "$(svc_active "$SVC_ACE" && echo true || echo false)" \
        "$(svc_active "$SVC_TTS" && echo true || echo false)" \
        "$(svc_active "$SVC_STT" && echo true || echo false)" \
        "$(svc_active "$SVC_DASH" && echo true || echo false)"
    printf '"ts":"%s"}\n' "$(date '+%Y-%m-%dT%H:%M:%S')"
}

# ── attese ───────────────────────────────────────────────────────────────────

wait_mem() {  # target_gib
    local target="$1" waited=0 avail
    avail=$(mem_available_gib)
    log "attendo memoria: disponibili ${avail} GiB, servono ${target} GiB"
    while (( avail < target )); do
        (( waited >= MEM_WAIT_TIMEOUT )) && {
            log "ABORT: dopo ${waited}s la memoria disponibile e' ancora ${avail} GiB (< ${target})"
            return 1
        }
        sleep 5; waited=$((waited + 5)); avail=$(mem_available_gib)
        state_write true waiting-memory "memoria: ${avail}/${target} GiB (${waited}s)"
    done
    log "memoria ok: ${avail} GiB disponibili"
}

wait_until() {  # description timeout check_cmd...
    local desc="$1" timeout="$2"; shift 2
    local waited=0
    while ! "$@" >/dev/null 2>&1; do
        (( waited >= timeout )) && { log "ABORT: timeout ${timeout}s in attesa di ${desc}"; return 1; }
        sleep 5; waited=$((waited + 5))
        state_write true "waiting-${desc}" "${desc}: ${waited}s/${timeout}s"
    done
    log "${desc}: ok dopo ${waited}s"
}

# ── azioni ───────────────────────────────────────────────────────────────────

stop_vllm() {
    vllm_container_up || { log "vllm gia' spento"; return 0; }
    log "spengo vllm ($VLLM_CONTAINER) via flash-next/stop.sh"
    state_write true stopping-vllm "spengo vllm"
    "$FLASH_DIR/stop.sh" >>"$LOG_FILE" 2>&1 || log "stop.sh ha segnalato un errore (procedo, verifico sotto)"
    local waited=0
    while vllm_container_up; do
        (( waited >= 120 )) && { log "ABORT: container ancora presente dopo ${waited}s"; return 1; }
        sleep 3; waited=$((waited + 3))
    done
    log "vllm spento"
}

start_vllm() {
    if vllm_ready; then log "vllm gia' pronto"; return 0; fi
    # Non accendere mai vllm con il creator ancora su: competerebbero per la pool.
    if svc_active "$SVC_COMFY" || svc_active "$SVC_ACE"; then
        log "ABORT: comfyui/ace-step ancora attivi, non accendo vllm"; return 1
    fi
    wait_mem "$MEM_FOR_VLLM" || return 1
    log "avvio vllm via flash-next/start.sh (parametri da .env: ABLIT=1, PORT=30000)"
    state_write true starting-vllm "avvio vllm (carica ~98 GiB, alcuni minuti)"
    ( cd "$FLASH_DIR" && ./start.sh ) >>"$LOG_FILE" 2>&1 &
    local pid=$!
    wait_until vllm-ready "$VLLM_READY_TIMEOUT" vllm_ready || { wait $pid 2>/dev/null; return 1; }
    wait $pid 2>/dev/null || true
    log "vllm pronto su $VLLM_URL"
}

stop_creator() {
    local stopped=0
    for svc in "$SVC_ACE" "$SVC_COMFY"; do   # ace-step prima: e' il piu' leggero
        if svc_active "$svc"; then
            log "spengo $svc"
            state_write true "stopping-$svc" "spengo $svc"
            sudo systemctl stop "$svc" >>"$LOG_FILE" 2>&1 || log "stop di $svc fallito"
            stopped=1
        fi
    done
    (( stopped == 0 )) && log "creator gia' spento"
    for svc in "$SVC_ACE" "$SVC_COMFY"; do
        svc_active "$svc" && { log "ABORT: $svc risulta ancora attivo"; return 1; }
    done
    return 0
}

start_creator() {
    if vllm_container_up; then
        log "ABORT: vllm ancora su, non accendo il creator"; return 1
    fi
    wait_mem "$MEM_FOR_CREATOR" || return 1
    log "avvio $SVC_COMFY"
    state_write true starting-comfyui "avvio ComfyUI"
    sudo systemctl start "$SVC_COMFY" >>"$LOG_FILE" 2>&1 || { log "ABORT: start comfyui fallito"; return 1; }
    wait_until comfyui-ready "$COMFY_READY_TIMEOUT" comfy_ready || return 1
    log "avvio $SVC_ACE"
    state_write true starting-acestep "avvio AceStep"
    sudo systemctl start "$SVC_ACE" >>"$LOG_FILE" 2>&1 || log "start ace-step fallito (ComfyUI resta su)"
    log "creator pronto"
}

# ── main ─────────────────────────────────────────────────────────────────────

cmd="${1:-status}"

if [[ "$cmd" == "status" ]]; then
    if exec 9>"$LOCK_FILE" 2>/dev/null; then LOCK_OK=1; else LOCK_OK=0; fi
    status_json
    exit 0
fi

case "$cmd" in
    llm|creator) ;;
    *) echo "uso: gb10-mode {status|llm|creator} [--json]" >&2; exit 2 ;;
esac

if ! exec 9>"$LOCK_FILE" 2>/dev/null; then
    log "ABORT: non riesco ad aprire il lock $LOCK_FILE"
    exit 4
fi
LOCK_OK=1
if ! flock -n 9; then
    log "un altro switch e' in corso, esco"
    echo '{"error":"switch already in progress"}' >&2
    exit 3
fi

current="$(detect_mode)"
log "=== switch richiesto: $cmd (modalita' attuale: $current) ==="

if [[ "$current" == "$cmd" ]]; then
    # idempotente, ma assicura che i pezzi della modalita' siano davvero su
    log "gia' in modalita' $cmd, verifico completezza"
fi

rc=0
if [[ "$cmd" == "creator" ]]; then
    stop_vllm     || rc=1
    (( rc == 0 )) && { start_creator || rc=1; }
else
    stop_creator  || rc=1
    (( rc == 0 )) && { start_vllm || rc=1; }
fi

if (( rc == 0 )); then
    state_write false done "modalita' $cmd attiva"
    log "=== switch a $cmd completato ==="
else
    state_write false failed "switch a $cmd FALLITO, vedi $LOG_FILE"
    log "=== switch a $cmd FALLITO ==="
fi

status_json
exit $rc
