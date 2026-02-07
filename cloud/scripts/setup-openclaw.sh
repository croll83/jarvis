#!/bin/bash
# =============================================================================
# OpenClaw Setup Script (bare-metal)
# =============================================================================
# Installa OpenClaw come processo bare-metal e configura systemd.
# Funziona sia su VPS cloud che su VM locale dedicata.
#
# Prerequisiti:
#   - JARVIS repo gia clonato in /opt/jarvis
#
# Usage:
#   sudo bash /opt/jarvis/cloud/scripts/setup-openclaw.sh
#
# Dopo l'installazione:
#   1. su - jarvis && openclaw onboard   # Configura identita, API key, skill
#   2. sudo systemctl start openclaw
#   3. curl http://localhost:18789/health

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}"
echo "=============================================="
echo "       OpenClaw Setup (bare-metal)"
echo "=============================================="
echo -e "${NC}"

# Detect user
JARVIS_USER="${JARVIS_USER:-jarvis}"
JARVIS_DIR="${JARVIS_DIR:-/opt/jarvis}"

# =============================================================================
# Step 1: Install Node.js 22 (if not present)
# =============================================================================
echo -e "${YELLOW}[1/5] Checking Node.js...${NC}"
if command -v node &> /dev/null; then
    NODE_VERSION=$(node -v | cut -d'v' -f2 | cut -d'.' -f1)
    if [ "$NODE_VERSION" -ge 22 ]; then
        echo "Node.js $(node -v) already installed"
    else
        echo "Node.js $(node -v) found but need 22+, upgrading..."
        curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
        apt-get install -y nodejs
    fi
else
    echo "Installing Node.js 22..."
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
    apt-get install -y nodejs
fi

echo "Node: $(node -v), npm: $(npm -v)"

# =============================================================================
# Step 2: Install OpenClaw
# =============================================================================
echo -e "${YELLOW}[2/5] Installing OpenClaw...${NC}"
npm install -g openclaw@latest

echo "OpenClaw version: $(openclaw --version 2>/dev/null || echo 'installed')"

# =============================================================================
# Step 3: Setup directories
# =============================================================================
echo -e "${YELLOW}[3/5] Setting up directories...${NC}"

# OpenClaw config dir (owned by jarvis user)
OPENCLAW_HOME="/home/${JARVIS_USER}/.openclaw"
mkdir -p "${OPENCLAW_HOME}/skills"
mkdir -p "${OPENCLAW_HOME}/workspace"

# Symlink JARVIS skill into OpenClaw skills directory
SKILL_SOURCE="${JARVIS_DIR}/jarvis-orchestrator/skill"
SKILL_LINK="${OPENCLAW_HOME}/skills/jarvis-orchestrator"

if [ -L "$SKILL_LINK" ]; then
    rm "$SKILL_LINK"
fi
ln -s "$SKILL_SOURCE" "$SKILL_LINK"
echo "Skill symlinked: ${SKILL_LINK} -> ${SKILL_SOURCE}"

# Fix ownership
chown -R "${JARVIS_USER}:${JARVIS_USER}" "${OPENCLAW_HOME}"

# =============================================================================
# Step 4: Create systemd service
# =============================================================================
echo -e "${YELLOW}[4/5] Creating systemd service...${NC}"

cat > /etc/systemd/system/openclaw.service <<EOF
[Unit]
Description=OpenClaw AI Gateway
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=${JARVIS_USER}
Group=${JARVIS_USER}
Environment=NODE_ENV=production
Environment=HOME=/home/${JARVIS_USER}
ExecStart=$(which openclaw) gateway run
KillSignal=SIGINT
KillMode=mixed
TimeoutStopSec=15
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=openclaw

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/home/${JARVIS_USER}/.openclaw /home/${JARVIS_USER}/.npm /home/${JARVIS_USER}/.config
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable openclaw
echo "Systemd service created and enabled"

# =============================================================================
# Step 5: Summary
# =============================================================================
echo -e "${YELLOW}[5/5] Done!${NC}"

echo ""
echo -e "${GREEN}=============================================="
echo "       OpenClaw Setup Complete!"
echo "==============================================${NC}"
echo ""
echo "Next steps (run as ${JARVIS_USER}):"
echo ""
echo -e "1. ${YELLOW}Onboarding (configura identita e API key):${NC}"
echo "   su - ${JARVIS_USER}"
echo "   openclaw onboard"
echo ""
echo -e "2. ${YELLOW}Verifica la skill JARVIS:${NC}"
echo "   ls -la ~/.openclaw/skills/jarvis-orchestrator/"
echo ""
echo -e "3. ${YELLOW}Avvia OpenClaw:${NC}"
echo "   sudo systemctl start openclaw"
echo "   sudo systemctl status openclaw"
echo ""
echo -e "4. ${YELLOW}Verifica:${NC}"
echo "   curl http://localhost:18789/health"
echo ""
echo -e "5. ${YELLOW}Dashboard (da un browser nel tailnet):${NC}"
echo "   http://<tailscale-ip>:18789"
echo ""
echo -e "6. ${YELLOW}Logs:${NC}"
echo "   journalctl -u openclaw -f"
echo ""
echo -e "${GREEN}Enjoy OpenClaw + JARVIS!${NC}"
