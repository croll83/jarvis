#!/usr/bin/env bash
# =============================================================================
# OPENCLAW MIGRATION IMPORT — VPS → LXC Proxmox
# =============================================================================
# Importa l'archivio esportato dal VPS sul nuovo LXC OpenClaw.
# Eseguire come utente jarvis sul LXC target.
#
# Prerequisiti:
#   - Ansible common.yml + openclaw.yml gia' eseguiti (Node.js, Chrome, Docker)
#   - Archivio openclaw-migration-*.tar.gz presente in /tmp/
#   - Tailscale connesso (per raggiungere VPS orchestrator + ontology)
#
# Uso:
#   bash migrate-import-lxc.sh [path-to-archive]
# =============================================================================
set -euo pipefail

ARCHIVE="${1:-$(ls -t /tmp/openclaw-migration-*.tar.gz 2>/dev/null | head -1)}"
EXPORT_DIR="/tmp/openclaw-migration"
OPENCLAW_HOME="$HOME/.openclaw"
VPS_TAILSCALE_IP="100.100.74.71"

# Colori
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; }

# ─────────────────────────────────────────────────────────────────────────────
# VALIDAZIONE
# ─────────────────────────────────────────────────────────────────────────────
if [ -z "$ARCHIVE" ] || [ ! -f "$ARCHIVE" ]; then
    err "Archivio non trovato. Uso: $0 /tmp/openclaw-migration-*.tar.gz"
    exit 1
fi

echo "================================================================"
echo "  OpenClaw Migration Import — VPS → LXC"
echo "================================================================"
echo "Archivio: $ARCHIVE"
echo "Target:   $OPENCLAW_HOME"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# 1. ESTRAI ARCHIVIO
# ─────────────────────────────────────────────────────────────────────────────
log "[1/7] Estrazione archivio..."
rm -rf "$EXPORT_DIR"
tar xzf "$ARCHIVE" -C /tmp/

if [ ! -d "$EXPORT_DIR" ]; then
    err "Directory $EXPORT_DIR non trovata dopo estrazione"
    exit 1
fi

# ─────────────────────────────────────────────────────────────────────────────
# 2. RIPRISTINA ~/.openclaw/ (config, agents, cron, credentials, etc.)
# ─────────────────────────────────────────────────────────────────────────────
log "[2/7] Ripristino configurazione OpenClaw..."

# Backup config esistente (se presente da openclaw onboard)
if [ -f "$OPENCLAW_HOME/openclaw.json" ]; then
    cp "$OPENCLAW_HOME/openclaw.json" "$OPENCLAW_HOME/openclaw.json.pre-migration"
fi

rsync -a --exclude='workspace/' "$EXPORT_DIR/openclaw-config/" "$OPENCLAW_HOME/"
log "  Config, agents, cron, credentials, extensions, memory importati"

# ─────────────────────────────────────────────────────────────────────────────
# 3. RIPRISTINA WORKSPACE
# ─────────────────────────────────────────────────────────────────────────────
log "[3/7] Ripristino workspace..."
rsync -a "$EXPORT_DIR/workspace/" "$OPENCLAW_HOME/workspace/"

# Installa dipendenze npm workspace
if [ -f "$OPENCLAW_HOME/workspace/package.json" ]; then
    (cd "$OPENCLAW_HOME/workspace" && npm install --production 2>/dev/null) || warn "npm install workspace fallito (non critico)"
fi

# Installa dipendenze npm per ogni skill con package.json
for skill_dir in "$OPENCLAW_HOME/workspace/skills"/*/; do
    if [ -f "$skill_dir/package.json" ]; then
        skill_name=$(basename "$skill_dir")
        (cd "$skill_dir" && npm install --production 2>/dev/null) && \
            log "  Skill $skill_name: npm install OK" || \
            warn "  Skill $skill_name: npm install fallito (non critico)"
    fi
done

# ─────────────────────────────────────────────────────────────────────────────
# 4. INSTALLA DIPENDENZE EXTENSIONS
# ─────────────────────────────────────────────────────────────────────────────
log "[4/7] Installazione dipendenze extensions..."
for ext_dir in "$OPENCLAW_HOME/extensions"/*/; do
    if [ -f "$ext_dir/package.json" ]; then
        ext_name=$(basename "$ext_dir")
        (cd "$ext_dir" && npm install --production 2>/dev/null) && \
            log "  Extension $ext_name: npm install OK" || \
            warn "  Extension $ext_name: npm install fallito"
    fi
done

# ─────────────────────────────────────────────────────────────────────────────
# 5. RE-ENCRYPT SECRETS
# ─────────────────────────────────────────────────────────────────────────────
log "[5/7] Re-encrypt secrets con machine-id locale..."
NEW_MID=$(cat /etc/machine-id)
SECRETS_DIR="$OPENCLAW_HOME/workspace/.secrets"
mkdir -p "$SECRETS_DIR"

# HL Wallet
if [ -f "$EXPORT_DIR/wallet-decrypted/hl-wallet.txt" ]; then
    openssl enc -aes-256-cbc -pbkdf2 \
        -in "$EXPORT_DIR/wallet-decrypted/hl-wallet.txt" \
        -out "$SECRETS_DIR/hl-wallet.enc" \
        -pass "pass:${NEW_MID}" 2>/dev/null && \
        log "  hl-wallet.enc re-criptato" || \
        err "  hl-wallet.enc re-encrypt FALLITO"
elif [ -f "$EXPORT_DIR/wallet-decrypted/hl-wallet.enc" ]; then
    warn "  hl-wallet era gia' criptato nell'export (decrypt fallito sul VPS)"
    cp "$EXPORT_DIR/wallet-decrypted/hl-wallet.enc" "$SECRETS_DIR/"
fi

# Twitter creds
if [ -f "$EXPORT_DIR/wallet-decrypted/twitter-creds.txt" ]; then
    openssl enc -aes-256-cbc -pbkdf2 \
        -in "$EXPORT_DIR/wallet-decrypted/twitter-creds.txt" \
        -out "$SECRETS_DIR/twitter-creds.enc" \
        -pass "pass:${NEW_MID}" 2>/dev/null && \
        log "  twitter-creds.enc re-criptato" || \
        err "  twitter-creds.enc re-encrypt FALLITO"
elif [ -f "$EXPORT_DIR/wallet-decrypted/twitter-creds.enc" ]; then
    cp "$EXPORT_DIR/wallet-decrypted/twitter-creds.enc" "$SECRETS_DIR/"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 6. AGGIORNA IP IN openclaw.json
# ─────────────────────────────────────────────────────────────────────────────
log "[6/7] Aggiornamento configurazione IP..."

NEW_TS_IP=$(tailscale ip -4 2>/dev/null || echo "")
if [ -z "$NEW_TS_IP" ]; then
    warn "Tailscale non connesso — aggiorna manualmente gli IP in openclaw.json"
    warn "Esegui: sudo tailscale up --hostname=jarvis-openclaw"
    warn "Poi: tailscale ip -4"
else
    log "  Tailscale IP locale: $NEW_TS_IP"

    # Aggiorna trustedProxies: aggiungi nuovo IP, mantieni VPS IP
    # L'orchestrator sul VPS (100.100.74.71) deve restare trusted
    if command -v jq &>/dev/null; then
        # Aggiorna ontology URL: da localhost a VPS via Tailscale
        jq --arg vps "$VPS_TAILSCALE_IP" \
           '.skills.entries["ontology-remote"].env.ONTOLOGY_URL = "http://\($vps):8100"' \
           "$OPENCLAW_HOME/openclaw.json" > "$OPENCLAW_HOME/openclaw.json.tmp" && \
           mv "$OPENCLAW_HOME/openclaw.json.tmp" "$OPENCLAW_HOME/openclaw.json"

        # Aggiorna trustedProxies con entrambi gli IP
        jq --arg new_ip "$NEW_TS_IP" --arg vps "$VPS_TAILSCALE_IP" \
           '.gateway.trustedProxies = ["127.0.0.1", $vps, $new_ip]' \
           "$OPENCLAW_HOME/openclaw.json" > "$OPENCLAW_HOME/openclaw.json.tmp" && \
           mv "$OPENCLAW_HOME/openclaw.json.tmp" "$OPENCLAW_HOME/openclaw.json"

        log "  openclaw.json aggiornato (ontology URL, trustedProxies)"
    else
        warn "jq non installato — aggiorna manualmente openclaw.json"
    fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# 7. PULIZIA CRON TEMP FILES
# ─────────────────────────────────────────────────────────────────────────────
log "[7/7] Pulizia file temporanei cron..."
find "$OPENCLAW_HOME/cron/" -name "*.tmp" -delete 2>/dev/null || true

# ─────────────────────────────────────────────────────────────────────────────
# RIEPILOGO
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "================================================================"
echo -e "${GREEN}Import completato!${NC}"
echo "================================================================"
echo ""
echo "Prossimi passi:"
echo "  1. Connetti Tailscale (se non fatto):"
echo "       sudo tailscale up --hostname=jarvis-openclaw"
echo ""
echo "  2. Build e avvia lab-server Docker:"
echo "       cd ~/.openclaw/workspace/projects/openclaw-lab-server"
echo "       docker compose up -d --build"
echo ""
echo "  3. Avvia OpenClaw gateway:"
echo "       openclaw gateway start"
echo ""
echo "  4. Verifica:"
echo "       openclaw gateway status"
echo "       openclaw health"
echo "       curl http://127.0.0.1:18800/json/version   # Chrome CDP"
echo "       curl http://$VPS_TAILSCALE_IP:5000/health   # Orchestrator VPS"
echo "       openclaw cron list                           # Cron jobs"
echo ""
if [ -n "$NEW_TS_IP" ]; then
    echo "  5. Aggiorna .env sul VPS:"
    echo "       OPENCLAW_URL=https://openclaw.mintwork.it:18789"
    echo "       OPENCLAW_WS_URL=wss://openclaw.mintwork.it:18789"
    echo "       Poi: docker compose restart jarvis_orchestrator"
fi
echo ""
echo "================================================================"

# Cleanup
rm -rf "$EXPORT_DIR"
