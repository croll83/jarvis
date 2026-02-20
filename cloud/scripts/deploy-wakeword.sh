#!/bin/bash
# =============================================================================
# JARVIS Wakeword Server — Deploy Script (LXC su Proxmox)
# =============================================================================
#
# Questo script:
#   1. Crea un LXC container su Proxmox (se non esiste)
#   2. Installa Docker + Tailscale nel LXC
#   3. Deploya jarvis-wakeword-server via Docker Compose
#
# Prerequisiti:
#   - Eseguire come root sul Proxmox HOST (non dentro un LXC/VM)
#   - Template Ubuntu/Debian scaricato (pveam)
#   - Connessione internet attiva
#
# Utilizzo:
#   sudo ./deploy-wakeword.sh
#
# Variabili d'ambiente opzionali (o risponde interattivamente):
#   WAKEWORD_CT_ID=210
#   WAKEWORD_HOSTNAME=jarvis-wakeword
#   WAKEWORD_IP=192.168.1.210/24
#   WAKEWORD_GW=192.168.1.1
#   WAKEWORD_BRIDGE=vmbr0
#   WAKEWORD_STORAGE=local-lvm
#   WAKEWORD_TEMPLATE=local:vztmpl/ubuntu-22.04-standard_22.04-1_amd64.tar.zst
#   TAILSCALE_AUTHKEY=tskey-auth-xxxxx
#   ORCHESTRATOR_WS_URL=ws://jarvis-pub.mintwork.it/ws/audio
#   DEVICE_API_TOKEN=xxxxx
#   WAKEWORD_THRESHOLD=0.5
#   MULTIROOM_COOLDOWN_S=5
#   JARVIS_REPO=https://github.com/croll83/jarvis.git
#
# =============================================================================

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

TOTAL_STEPS=7

echo -e "${GREEN}"
echo "=============================================="
echo "  JARVIS Wakeword Server — Deploy Script"
echo "=============================================="
echo -e "${NC}"

# Check root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Errore: eseguire come root sul Proxmox host${NC}"
    exit 1
fi

# Check running on Proxmox host (not inside container)
if [ -f /proc/1/environ ] && grep -q container=lxc /proc/1/environ 2>/dev/null; then
    echo -e "${RED}Errore: questo script va eseguito sul Proxmox HOST, non dentro un LXC${NC}"
    exit 1
fi

# =============================================================================
# Interactive config (with env var fallbacks)
# =============================================================================
prompt_var() {
    local var_name="$1"
    local prompt_text="$2"
    local default_val="$3"
    local current_val="${!var_name:-}"

    if [ -n "$current_val" ]; then
        echo -e "  ${CYAN}${prompt_text}:${NC} ${current_val} (da env)"
        return
    fi

    if [ -n "$default_val" ]; then
        read -rp "  ${prompt_text} [${default_val}]: " input
        eval "${var_name}=\"${input:-$default_val}\""
    else
        read -rp "  ${prompt_text}: " input
        if [ -z "$input" ]; then
            echo -e "${RED}  Valore obbligatorio!${NC}"
            exit 1
        fi
        eval "${var_name}=\"${input}\""
    fi
}

echo -e "${YELLOW}Configurazione LXC:${NC}"
prompt_var WAKEWORD_CT_ID      "Container ID"               "210"
prompt_var WAKEWORD_HOSTNAME   "Hostname"                   "jarvis-wakeword"
prompt_var WAKEWORD_IP         "IP (con CIDR, es. x.x.x.x/24)" ""
prompt_var WAKEWORD_GW         "Gateway"                    ""
prompt_var WAKEWORD_BRIDGE     "Bridge di rete"             "vmbr0"
prompt_var WAKEWORD_STORAGE    "Storage per rootfs"         "local-lvm"

# Trova template disponibile
if [ -z "${WAKEWORD_TEMPLATE:-}" ]; then
    echo ""
    echo -e "${CYAN}  Template disponibili:${NC}"
    pveam list local 2>/dev/null | grep -E "ubuntu|debian" | head -5 || true
    prompt_var WAKEWORD_TEMPLATE "Template (path completo)" "local:vztmpl/ubuntu-22.04-standard_22.04-1_amd64.tar.zst"
fi

echo ""
echo -e "${YELLOW}Configurazione Tailscale:${NC}"
prompt_var TAILSCALE_AUTHKEY   "Tailscale auth key (tskey-auth-...)" ""

echo ""
echo -e "${YELLOW}Configurazione Wakeword Server:${NC}"
prompt_var ORCHESTRATOR_WS_URL "Orchestrator WS URL"        "ws://jarvis-pub.mintwork.it/ws/audio"
prompt_var DEVICE_API_TOKEN    "Device API Token"            ""
prompt_var WAKEWORD_THRESHOLD  "Wake word threshold"         "0.5"
prompt_var MULTIROOM_COOLDOWN_S "Multi-room cooldown (sec)"  "5"
prompt_var JARVIS_REPO         "Git repo URL"                "https://github.com/croll83/jarvis.git"

echo ""
echo -e "${GREEN}Riepilogo:${NC}"
echo "  LXC CT ID:        ${WAKEWORD_CT_ID}"
echo "  Hostname:          ${WAKEWORD_HOSTNAME}"
echo "  IP:                ${WAKEWORD_IP}"
echo "  Gateway:           ${WAKEWORD_GW}"
echo "  Bridge:            ${WAKEWORD_BRIDGE}"
echo "  Orchestrator URL:  ${ORCHESTRATOR_WS_URL}"
echo ""
read -rp "Procedere? [y/N]: " confirm
if [[ ! "$confirm" =~ ^[yY]$ ]]; then
    echo "Annullato."
    exit 0
fi

# =============================================================================
# STEP 1: Crea LXC Container
# =============================================================================
echo ""
echo -e "${YELLOW}[1/${TOTAL_STEPS}] Creazione LXC container ${WAKEWORD_CT_ID}...${NC}"

if pct status "${WAKEWORD_CT_ID}" &>/dev/null; then
    echo -e "  Container ${WAKEWORD_CT_ID} esiste gia. Skip creazione."
    # Assicurati che sia avviato
    pct start "${WAKEWORD_CT_ID}" 2>/dev/null || true
else
    pct create "${WAKEWORD_CT_ID}" "${WAKEWORD_TEMPLATE}" \
        --hostname "${WAKEWORD_HOSTNAME}" \
        --cores 1 \
        --memory 2048 \
        --swap 512 \
        --rootfs "${WAKEWORD_STORAGE}:10" \
        --net0 "name=eth0,bridge=${WAKEWORD_BRIDGE},ip=${WAKEWORD_IP},gw=${WAKEWORD_GW}" \
        --features nesting=1,keyctl=1 \
        --unprivileged 1 \
        --onboot 1 \
        --start 1

    echo -e "  ${GREEN}Container ${WAKEWORD_CT_ID} creato e avviato${NC}"

    # Aspetta che il container sia pronto
    echo "  Attendo avvio container..."
    sleep 5
fi

# Helper: esegui un comando dentro il LXC
lxc_exec() {
    pct exec "${WAKEWORD_CT_ID}" -- bash -c "$1"
}

# Attendi che la rete sia pronta
echo "  Verifico connettivita..."
for i in $(seq 1 15); do
    if lxc_exec "ping -c1 -W2 8.8.8.8 &>/dev/null"; then
        echo -e "  ${GREEN}Rete OK${NC}"
        break
    fi
    if [ "$i" -eq 15 ]; then
        echo -e "${RED}  Timeout rete! Verifica configurazione IP/gateway.${NC}"
        exit 1
    fi
    sleep 2
done

# =============================================================================
# STEP 2: Installa dipendenze nel LXC
# =============================================================================
echo ""
echo -e "${YELLOW}[2/${TOTAL_STEPS}] Aggiornamento sistema e installazione dipendenze...${NC}"

lxc_exec "apt update && apt upgrade -y -q"
lxc_exec "apt install -y -q curl git ca-certificates gnupg"

# =============================================================================
# STEP 3: Installa Docker
# =============================================================================
echo ""
echo -e "${YELLOW}[3/${TOTAL_STEPS}] Installazione Docker...${NC}"

lxc_exec '
if ! command -v docker &>/dev/null; then
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
    echo "Docker installato: $(docker --version)"
else
    echo "Docker gia installato: $(docker --version)"
fi
apt install -y -q docker-compose-plugin 2>/dev/null || true
'

# =============================================================================
# STEP 4: Installa e connetti Tailscale
# =============================================================================
echo ""
echo -e "${YELLOW}[4/${TOTAL_STEPS}] Installazione Tailscale...${NC}"

lxc_exec '
if ! command -v tailscale &>/dev/null; then
    curl -fsSL https://tailscale.com/install.sh | sh
    echo "Tailscale installato"
else
    echo "Tailscale gia installato"
fi
'

TS_HOSTNAME="${WAKEWORD_HOSTNAME}"
lxc_exec "
if tailscale status &>/dev/null 2>&1; then
    echo 'Tailscale gia connesso:'
    tailscale status | head -3
else
    echo 'Connessione Tailscale come ${TS_HOSTNAME}...'
    tailscale up --hostname=${TS_HOSTNAME} --authkey='${TAILSCALE_AUTHKEY}'
    echo 'Tailscale connesso!'
    tailscale ip -4
fi
"

# Recupera IP Tailscale per il report finale
TS_IP=$(lxc_exec "tailscale ip -4 2>/dev/null" | tr -d '[:space:]')
echo -e "  ${GREEN}Tailscale IP: ${TS_IP}${NC}"

# =============================================================================
# STEP 5: Clone repo e copia codice
# =============================================================================
echo ""
echo -e "${YELLOW}[5/${TOTAL_STEPS}] Clone codice wakeword-server...${NC}"

lxc_exec "mkdir -p /opt/jarvis-wakeword"

lxc_exec "
if [ ! -d /opt/jarvis-wakeword/.git ]; then
    cd /opt/jarvis-wakeword
    git clone --depth 1 --filter=blob:none --sparse '${JARVIS_REPO}' .
    git sparse-checkout set jarvis/wakeword-server
    echo 'Repo clonato (sparse checkout)'
else
    cd /opt/jarvis-wakeword
    git pull --depth 1 || true
    echo 'Repo aggiornato'
fi
"

# =============================================================================
# STEP 6: Crea .env e avvia Docker Compose
# =============================================================================
echo ""
echo -e "${YELLOW}[6/${TOTAL_STEPS}] Configurazione e avvio wakeword-server...${NC}"

# Scrivi .env
lxc_exec "cat > /opt/jarvis-wakeword/jarvis/wakeword-server/.env << 'ENVEOF'
ORCHESTRATOR_WS_URL=${ORCHESTRATOR_WS_URL}
DEVICE_API_TOKEN=${DEVICE_API_TOKEN}
WAKEWORD_MODEL=hey_jarvis
WAKEWORD_THRESHOLD=${WAKEWORD_THRESHOLD}
MULTIROOM_COOLDOWN_S=${MULTIROOM_COOLDOWN_S}
ENVEOF
chmod 600 /opt/jarvis-wakeword/jarvis/wakeword-server/.env
"

# Build e avvia
lxc_exec "cd /opt/jarvis-wakeword/jarvis/wakeword-server && docker compose up -d --build"

# =============================================================================
# STEP 7: Health check
# =============================================================================
echo ""
echo -e "${YELLOW}[7/${TOTAL_STEPS}] Verifica health...${NC}"

HEALTHY=false
for i in $(seq 1 12); do
    if lxc_exec "curl -sf http://localhost:8200/health" >/dev/null 2>&1; then
        HEALTH_JSON=$(lxc_exec "curl -sf http://localhost:8200/health")
        echo -e "  ${GREEN}Wakeword server healthy!${NC}"
        echo "  $HEALTH_JSON"
        HEALTHY=true
        break
    fi
    echo "  Attendo avvio... (${i}/12)"
    sleep 5
done

if [ "$HEALTHY" != "true" ]; then
    echo -e "${RED}  Health check fallito! Controlla i log:${NC}"
    echo "    pct exec ${WAKEWORD_CT_ID} -- docker logs jarvis_wakeword"
    exit 1
fi

# =============================================================================
# REPORT FINALE
# =============================================================================
# Estrai IP LAN (senza CIDR)
LAN_IP=$(echo "${WAKEWORD_IP}" | cut -d'/' -f1)

echo ""
echo -e "${GREEN}=============================================="
echo "  JARVIS Wakeword Server — Deploy Completato!"
echo "==============================================${NC}"
echo ""
echo "  Container ID:     ${WAKEWORD_CT_ID}"
echo "  Hostname:          ${WAKEWORD_HOSTNAME}"
echo "  IP LAN:            ${LAN_IP}"
echo "  IP Tailscale:      ${TS_IP}"
echo "  Porta:             8200"
echo ""
echo "  Health Check:      http://${LAN_IP}:8200/health"
echo "  Devices API:       http://${LAN_IP}:8200/api/devices"
echo ""
echo -e "${YELLOW}Prossimi step:${NC}"
echo ""
echo "  1. ORCHESTRATOR (.env sul VPS cloud):"
echo "     Aggiungi questa riga al file .env dell'orchestrator:"
echo ""
echo "     WAKEWORD_SERVER_URLS={\"TUA_LOCATION_ID\": \"http://${TS_IP}:8200\"}"
echo ""
echo "     Poi: docker compose restart orchestrator"
echo ""
echo "  2. FIRMWARE AtomS3R (sdkconfig.local):"
echo "     Modifica/aggiungi queste righe:"
echo ""
echo "     CONFIG_JARVIS_WS_URL=\"ws://${LAN_IP}:8200/ws/audio\""
echo "     CONFIG_USE_LOCAL_WAKEWORD=n"
echo ""
echo "     Poi: ./build.sh && idf.py -p /dev/ttyUSB0 flash"
echo ""
echo -e "${CYAN}Logs:${NC}"
echo "  pct exec ${WAKEWORD_CT_ID} -- docker logs -f jarvis_wakeword"
echo ""
