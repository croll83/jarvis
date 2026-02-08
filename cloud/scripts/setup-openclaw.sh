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
echo -e "${YELLOW}[1/6] Checking Node.js...${NC}"
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
echo -e "${YELLOW}[2/6] Installing OpenClaw...${NC}"
npm install -g openclaw@latest

echo "OpenClaw version: $(openclaw --version 2>/dev/null || echo 'installed')"

# =============================================================================
# Step 3: Setup directories
# =============================================================================
echo -e "${YELLOW}[3/6] Setting up directories...${NC}"

# OpenClaw config dir (owned by jarvis user)
OPENCLAW_HOME="/home/${JARVIS_USER}/.openclaw"
mkdir -p "${OPENCLAW_HOME}/workspace/skills"

# Copy JARVIS skill into OpenClaw skills directory
# (we copy instead of symlink to avoid ELOOP issues with OpenClaw's file watcher)
SKILL_SOURCE="${JARVIS_DIR}/jarvis-orchestrator/skill"
SKILL_DEST="${OPENCLAW_HOME}/workspace/skills/jarvis-orchestrator"

# Remove old symlink or directory
if [ -L "$SKILL_DEST" ]; then
    rm "$SKILL_DEST"
elif [ -d "$SKILL_DEST" ]; then
    rm -rf "$SKILL_DEST"
fi

mkdir -p "$SKILL_DEST"
cp "${SKILL_SOURCE}/SKILL.md" "$SKILL_DEST/"
cp "${SKILL_SOURCE}/skill.json" "$SKILL_DEST/"
echo "Skill copied: ${SKILL_SOURCE} -> ${SKILL_DEST}"

# Fix ownership
chown -R "${JARVIS_USER}:${JARVIS_USER}" "${OPENCLAW_HOME}"

# =============================================================================
# Step 4: Create systemd service
# =============================================================================
echo -e "${YELLOW}[4/6] Creating systemd service...${NC}"

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
KillSignal=SIGTERM
KillMode=control-group
TimeoutStopSec=30
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
# Step 5: Install jq (needed for post-onboarding config)
# =============================================================================
echo -e "${YELLOW}[5/6] Checking jq...${NC}"
if ! command -v jq &> /dev/null; then
    apt-get install -y jq
    echo "jq installed"
else
    echo "jq already installed"
fi

# =============================================================================
# Step 6: Create post-onboarding helper script
# =============================================================================
echo -e "${YELLOW}[6/6] Creating post-onboarding config script...${NC}"

HELPER_SCRIPT="${JARVIS_DIR}/cloud/scripts/configure-openclaw-skill.sh"
cat > "$HELPER_SCRIPT" <<'SCRIPT'
#!/bin/bash
# =============================================================================
# Configure JARVIS skill env vars in OpenClaw
# =============================================================================
# Run this AFTER 'openclaw onboard' to inject JARVIS_ORCHESTRATOR_URL and
# OPENCLAW_GATEWAY_TOKEN into the OpenClaw config.
#
# Usage (as jarvis user):
#   bash /opt/jarvis/cloud/scripts/configure-openclaw-skill.sh
#
# Optional:
#   JARVIS_ORCHESTRATOR_URL=http://custom:5000 bash configure-openclaw-skill.sh

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

OPENCLAW_CONFIG="$HOME/.openclaw/openclaw.json"

# Check openclaw.json exists
if [ ! -f "$OPENCLAW_CONFIG" ]; then
    echo -e "${RED}Error: $OPENCLAW_CONFIG not found. Run 'openclaw onboard' first.${NC}"
    exit 1
fi

# Read gateway token from existing config
GW_TOKEN=$(jq -r '.gateway.auth.token // empty' "$OPENCLAW_CONFIG")
if [ -z "$GW_TOKEN" ]; then
    echo -e "${RED}Error: No gateway token found in config. Run 'openclaw onboard' first.${NC}"
    exit 1
fi

# Orchestrator URL (default: http://localhost:5000)
ORCH_URL="${JARVIS_ORCHESTRATOR_URL:-http://localhost:5000}"

echo -e "${YELLOW}Configuring jarvis-orchestrator skill...${NC}"
echo "  Gateway token: ${GW_TOKEN:0:8}..."
echo "  Orchestrator URL: ${ORCH_URL}"

# Inject skill env config into openclaw.json
jq --arg token "$GW_TOKEN" --arg url "$ORCH_URL" '
  .skills.entries["jarvis-orchestrator"] = {
    "env": {
      "OPENCLAW_GATEWAY_TOKEN": $token,
      "JARVIS_ORCHESTRATOR_URL": $url
    }
  }
' "$OPENCLAW_CONFIG" > "${OPENCLAW_CONFIG}.tmp" && mv "${OPENCLAW_CONFIG}.tmp" "$OPENCLAW_CONFIG"

echo -e "${GREEN}Done! jarvis-orchestrator skill configured in openclaw.json${NC}"
echo ""
echo "Restart OpenClaw to apply:"
echo "  sudo systemctl restart openclaw"
SCRIPT

chmod +x "$HELPER_SCRIPT"
chown "${JARVIS_USER}:${JARVIS_USER}" "$HELPER_SCRIPT"
echo "Helper script created: ${HELPER_SCRIPT}"

# =============================================================================
# Summary
# =============================================================================
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
echo -e "2. ${YELLOW}Configura la skill JARVIS (dopo onboarding):${NC}"
echo "   bash /opt/jarvis/cloud/scripts/configure-openclaw-skill.sh"
echo ""
echo -e "3. ${YELLOW}Avvia OpenClaw:${NC}"
echo "   sudo systemctl start openclaw"
echo "   sudo systemctl status openclaw"
echo ""
echo -e "4. ${YELLOW}Verifica la skill:${NC}"
echo "   ls -la ~/.openclaw/workspace/skills/jarvis-orchestrator/"
echo ""
echo -e "5. ${YELLOW}Dashboard (da un browser nel tailnet):${NC}"
echo "   http://<tailscale-ip>:18789"
echo ""
echo -e "6. ${YELLOW}Logs:${NC}"
echo "   journalctl -u openclaw -f"
echo ""
echo -e "${GREEN}Enjoy OpenClaw + JARVIS!${NC}"
