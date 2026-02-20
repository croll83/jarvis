#!/bin/bash
# =============================================================================
# JARVIS Wakeword Server — Deploy Script (LXC su Proxmox)
# =============================================================================
#
# Questo script:
#   1. Crea un LXC container su Proxmox (se non esiste)
#   2. Configura DNS e TUN device per Tailscale
#   3. Installa Docker + Tailscale nel LXC
#   4. Deploya jarvis-wakeword-server via Docker Compose
#
# Prerequisiti:
#   - Eseguire come root sul Proxmox HOST (non dentro un LXC/VM)
#   - Template Ubuntu/Debian scaricato (pveam download local ...)
#   - Connessione internet attiva
#
# Utilizzo:
#   bash deploy-wakeword.sh          (interattivo)
#
# Variabili d'ambiente opzionali (o risponde interattivamente):
#   WAKEWORD_CT_ID=210
#   WAKEWORD_HOSTNAME=jarvis-wakeword
#   WAKEWORD_IP=192.168.1.210/24
#   WAKEWORD_GW=192.168.1.1
#   WAKEWORD_BRIDGE=vmbr0
#   WAKEWORD_STORAGE=local-lvm
#   WAKEWORD_TEMPLATE=local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst
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

# Check pct command available (we are on Proxmox host)
if ! command -v pct &>/dev/null; then
    # Try with full path
    if [ -x /usr/sbin/pct ]; then
        export PATH="/usr/sbin:$PATH"
    else
        echo -e "${RED}Errore: comando 'pct' non trovato.${NC}"
        echo -e "${RED}Questo script va eseguito sul Proxmox HOST, non dentro un LXC/VM.${NC}"
        exit 1
    fi
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

# Trova template disponibile — auto-detect se non specificato
if [ -z "${WAKEWORD_TEMPLATE:-}" ]; then
    echo ""
    echo -e "${CYAN}  Template disponibili:${NC}"
    AVAILABLE=$(pveam list local 2>/dev/null | grep -E "ubuntu|debian" | awk '{print $1}' || true)
    if [ -n "$AVAILABLE" ]; then
        echo "$AVAILABLE" | head -5 | while read -r t; do echo "    $t"; done
        # Pick first debian/ubuntu as default
        AUTO_TEMPLATE=$(echo "$AVAILABLE" | head -1)
        prompt_var WAKEWORD_TEMPLATE "Template (path completo)" "${AUTO_TEMPLATE}"
    else
        echo -e "  ${YELLOW}Nessun template trovato! Scarica prima un template:${NC}"
        echo "    pveam available | grep -E 'debian-12|ubuntu-22'"
        echo "    pveam download local <nome-template>"
        exit 1
    fi
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
echo "  Template:          ${WAKEWORD_TEMPLATE}"
echo "  Orchestrator URL:  ${ORCHESTRATOR_WS_URL}"
echo ""
read -rp "Procedere? [y/N]: " confirm
if [[ ! "$confirm" =~ ^[yY]$ ]]; then
    echo "Annullato."
    exit 0
fi

# Helper: esegui un comando dentro il LXC
lxc_exec() {
    pct exec "${WAKEWORD_CT_ID}" -- bash -c "$1"
}

# =============================================================================
# STEP 1: Crea LXC Container
# =============================================================================
echo ""
echo -e "${YELLOW}[1/${TOTAL_STEPS}] Creazione LXC container ${WAKEWORD_CT_ID}...${NC}"

if pct status "${WAKEWORD_CT_ID}" &>/dev/null; then
    echo -e "  Container ${WAKEWORD_CT_ID} esiste gia. Skip creazione."
    # Assicurati che sia fermato per poter applicare config
    pct stop "${WAKEWORD_CT_ID}" 2>/dev/null || true
    sleep 2
else
    pct create "${WAKEWORD_CT_ID}" "${WAKEWORD_TEMPLATE}" \
        --hostname "${WAKEWORD_HOSTNAME}" \
        --cores 1 \
        --memory 2048 \
        --swap 512 \
        --rootfs "${WAKEWORD_STORAGE}:10" \
        --net0 "name=eth0,bridge=${WAKEWORD_BRIDGE},ip=${WAKEWORD_IP},gw=${WAKEWORD_GW}" \
        --nameserver "8.8.8.8 1.1.1.1" \
        --features nesting=1,keyctl=1 \
        --unprivileged 1 \
        --onboot 1

    echo -e "  ${GREEN}Container ${WAKEWORD_CT_ID} creato${NC}"
fi

# --- Configura TUN device per Tailscale (necessario in LXC unprivileged) ---
LXC_CONF="/etc/pve/lxc/${WAKEWORD_CT_ID}.conf"

echo "  Configuro TUN device per Tailscale..."

# Rimuovi vecchie entry TUN se presenti (idempotente)
sed -i '/lxc.cgroup2.devices.allow.*10:200/d' "$LXC_CONF"
sed -i '/lxc.mount.entry.*dev\/net\/tun/d' "$LXC_CONF"

# Aggiungi TUN device config
echo "lxc.cgroup2.devices.allow: c 10:200 rwm" >> "$LXC_CONF"
echo "lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file" >> "$LXC_CONF"

# Assicurati che il nameserver sia configurato (fix per container esistenti)
if ! grep -q "^nameserver:" "$LXC_CONF"; then
    echo "nameserver: 8.8.8.8 1.1.1.1" >> "$LXC_CONF"
fi

echo -e "  ${GREEN}TUN device e DNS configurati${NC}"

# --- Avvia il container ---
echo "  Avvio container..."
pct start "${WAKEWORD_CT_ID}"
sleep 5

# Attendi che la rete sia pronta (testa DNS, non solo ping)
echo "  Verifico connettivita e DNS..."
for i in $(seq 1 20); do
    if lxc_exec "ping -c1 -W2 8.8.8.8 &>/dev/null" 2>/dev/null; then
        # Test anche DNS
        if lxc_exec "getent hosts deb.debian.org &>/dev/null || getent hosts archive.ubuntu.com &>/dev/null" 2>/dev/null; then
            echo -e "  ${GREEN}Rete e DNS OK${NC}"
            break
        else
            echo "  Rete OK, DNS non ancora pronto... (${i}/20)"
        fi
    else
        echo "  Attendo rete... (${i}/20)"
    fi
    if [ "$i" -eq 20 ]; then
        echo -e "${RED}  Timeout rete/DNS! Verifica configurazione IP/gateway/nameserver.${NC}"
        echo -e "${RED}  Controlla: pct config ${WAKEWORD_CT_ID}${NC}"
        exit 1
    fi
    sleep 3
done

# =============================================================================
# STEP 2: Installa dipendenze nel LXC
# =============================================================================
echo ""
echo -e "${YELLOW}[2/${TOTAL_STEPS}] Aggiornamento sistema e installazione dipendenze...${NC}"

lxc_exec "apt-get update -qq && apt-get upgrade -y -qq"
lxc_exec "apt-get install -y -qq curl git ca-certificates gnupg lsb-release"

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
apt-get install -y -qq docker-compose-plugin 2>/dev/null || true
'

# =============================================================================
# STEP 4: Installa e connetti Tailscale
# =============================================================================
echo ""
echo -e "${YELLOW}[4/${TOTAL_STEPS}] Installazione e connessione Tailscale...${NC}"

# Verifica che TUN sia disponibile
if ! lxc_exec "ls /dev/net/tun &>/dev/null" 2>/dev/null; then
    echo -e "  ${YELLOW}WARN: /dev/net/tun non trovato, Tailscale usera userspace networking${NC}"
fi

# Installa Tailscale
lxc_exec '
if ! command -v tailscale &>/dev/null; then
    curl -fsSL https://tailscale.com/install.sh | sh
    echo "Tailscale installato"
else
    echo "Tailscale gia installato"
fi
'

# Avvia tailscaled e attendi che sia pronto
echo "  Avvio tailscaled..."
lxc_exec '
systemctl enable tailscaled 2>/dev/null || true
systemctl start tailscaled 2>/dev/null || true
'

# Attendi che tailscaled sia effettivamente pronto
TS_READY=false
for i in $(seq 1 15); do
    if lxc_exec "tailscale status &>/dev/null 2>&1" 2>/dev/null; then
        TS_READY=true
        break
    fi
    # Prova ad avviare di nuovo se non è partito
    if [ "$i" -eq 3 ] || [ "$i" -eq 8 ]; then
        lxc_exec "systemctl restart tailscaled 2>/dev/null || true"
    fi
    echo "  Attendo tailscaled... (${i}/15)"
    sleep 2
done

if [ "$TS_READY" != "true" ]; then
    echo -e "  ${YELLOW}tailscaled non parte con systemd, provo userspace mode...${NC}"
    lxc_exec "nohup tailscaled --state=/var/lib/tailscale/tailscaled.state --tun=userspace-networking &>/var/log/tailscaled.log &"
    sleep 3

    # Crea un servizio systemd per userspace mode (persistenza al reboot)
    lxc_exec 'cat > /etc/systemd/system/tailscaled-userspace.service << EOF
[Unit]
Description=Tailscale daemon (userspace networking)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/sbin/tailscaled --state=/var/lib/tailscale/tailscaled.state --tun=userspace-networking
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable tailscaled-userspace
'
    echo -e "  ${GREEN}tailscaled avviato in userspace mode${NC}"
fi

# Connetti Tailscale
TS_HOSTNAME="${WAKEWORD_HOSTNAME}"
TS_AUTHKEY="${TAILSCALE_AUTHKEY}"

# Verifica se già connesso
ALREADY_CONNECTED=false
if lxc_exec "tailscale status 2>/dev/null | grep -q '100\\.'" 2>/dev/null; then
    ALREADY_CONNECTED=true
    echo -e "  ${GREEN}Tailscale gia connesso${NC}"
fi

if [ "$ALREADY_CONNECTED" != "true" ]; then
    echo "  Connessione Tailscale come ${TS_HOSTNAME}..."
    lxc_exec "tailscale up --hostname='${TS_HOSTNAME}' --authkey='${TS_AUTHKEY}'"

    # Verifica connessione
    sleep 3
    if lxc_exec "tailscale status &>/dev/null 2>&1" 2>/dev/null; then
        echo -e "  ${GREEN}Tailscale connesso!${NC}"
    else
        echo -e "${RED}  Tailscale connessione fallita! Controlla l'auth key.${NC}"
        echo "  Debug: pct exec ${WAKEWORD_CT_ID} -- tailscale status"
        exit 1
    fi
fi

# Recupera IP Tailscale per il report finale
TS_IP=$(lxc_exec "tailscale ip -4 2>/dev/null" | tr -d '[:space:]')
if [ -z "$TS_IP" ]; then
    echo -e "${RED}  Impossibile ottenere IP Tailscale!${NC}"
    echo "  Debug: pct exec ${WAKEWORD_CT_ID} -- tailscale status"
    exit 1
fi
echo -e "  ${GREEN}Tailscale IP: ${TS_IP}${NC}"

# =============================================================================
# STEP 5: Clone repo e copia codice
# =============================================================================
echo ""
echo -e "${YELLOW}[5/${TOTAL_STEPS}] Clone codice wakeword-server...${NC}"

lxc_exec "mkdir -p /opt/jarvis-wakeword"

REPO_URL="${JARVIS_REPO}"
lxc_exec "
if [ ! -d /opt/jarvis-wakeword/.git ]; then
    git clone --depth 1 '${REPO_URL}' /opt/jarvis-wakeword
    echo 'Repo clonato'
else
    cd /opt/jarvis-wakeword
    git pull --depth 1 || true
    echo 'Repo aggiornato'
fi

# Verifica che la cartella wakeword-server esista
if [ ! -f /opt/jarvis-wakeword/wakeword-server/docker-compose.yml ]; then
    echo 'ERRORE: wakeword-server/ non trovato nel repo!'
    exit 1
fi
echo 'wakeword-server trovato'
"

# =============================================================================
# STEP 6: Crea .env e avvia Docker Compose
# =============================================================================
echo ""
echo -e "${YELLOW}[6/${TOTAL_STEPS}] Configurazione e avvio wakeword-server...${NC}"

# Scrivi .env
lxc_exec "cat > /opt/jarvis-wakeword/wakeword-server/.env << 'ENVEOF'
ORCHESTRATOR_WS_URL=${ORCHESTRATOR_WS_URL}
DEVICE_API_TOKEN=${DEVICE_API_TOKEN}
WAKEWORD_MODEL=hey_jarvis
WAKEWORD_THRESHOLD=${WAKEWORD_THRESHOLD}
MULTIROOM_COOLDOWN_S=${MULTIROOM_COOLDOWN_S}
ENVEOF
chmod 600 /opt/jarvis-wakeword/wakeword-server/.env
"

# Build e avvia
lxc_exec "cd /opt/jarvis-wakeword/wakeword-server && docker compose up -d --build"

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
