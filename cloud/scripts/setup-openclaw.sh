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
echo -e "${YELLOW}[1/10] Checking Node.js...${NC}"
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
echo -e "${YELLOW}[2/10] Installing OpenClaw...${NC}"
npm install -g openclaw@latest

echo "OpenClaw version: $(openclaw --version 2>/dev/null || echo 'installed')"

# =============================================================================
# Step 3: Setup directories
# =============================================================================
echo -e "${YELLOW}[3/10] Setting up directories...${NC}"

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
echo -e "${YELLOW}[4/10] Creating systemd service (OpenClaw)...${NC}"

BREW_PREFIX="/home/linuxbrew/.linuxbrew"

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
Environment=PATH=${BREW_PREFIX}/bin:${BREW_PREFIX}/sbin:/usr/local/bin:/usr/bin:/bin
Environment=CHROME_PATH=/usr/bin/google-chrome
Environment=PUPPETEER_EXECUTABLE_PATH=/usr/bin/google-chrome
EnvironmentFile=-/home/${JARVIS_USER}/.openclaw/openclaw.env
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
ReadWritePaths=/home/${JARVIS_USER}/.openclaw /home/${JARVIS_USER}/.npm /home/${JARVIS_USER}/.config /home/${JARVIS_USER}/.cache /tmp
PrivateTmp=false

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable openclaw
echo "Systemd service created and enabled"

# =============================================================================
# Step 5: Install browser-dom plugin
# =============================================================================
echo -e "${YELLOW}[5/10] Installing browser-dom plugin...${NC}"

PLUGIN_SOURCE="${JARVIS_DIR}/extensions/browser-dom"
PLUGIN_DEST="${OPENCLAW_HOME}/extensions/browser-dom"

if [ -d "$PLUGIN_SOURCE" ]; then
    # Remove old copy
    if [ -d "$PLUGIN_DEST" ]; then
        rm -rf "$PLUGIN_DEST"
    fi

    mkdir -p "$PLUGIN_DEST"
    cp -r "${PLUGIN_SOURCE}/src" "$PLUGIN_DEST/"
    cp "${PLUGIN_SOURCE}/index.ts" "$PLUGIN_DEST/"
    cp "${PLUGIN_SOURCE}/package.json" "$PLUGIN_DEST/"
    cp "${PLUGIN_SOURCE}/openclaw.plugin.json" "$PLUGIN_DEST/"
    cp "${PLUGIN_SOURCE}/tsconfig.json" "$PLUGIN_DEST/" 2>/dev/null || true

    # Install npm dependencies
    cd "$PLUGIN_DEST"
    npm install --omit=dev 2>/dev/null || npm install
    cd -

    # Fix ownership
    chown -R "${JARVIS_USER}:${JARVIS_USER}" "$PLUGIN_DEST"

    echo "browser-dom plugin installed: ${PLUGIN_DEST}"
else
    echo -e "${RED}Warning: browser-dom source not found at ${PLUGIN_SOURCE}${NC}"
    echo "Skipping browser-dom plugin installation"
fi

# =============================================================================
# Step 6: Create Chrome headless systemd service
# =============================================================================
echo -e "${YELLOW}[6/10] Creating Chrome headless systemd service...${NC}"

# Check if Google Chrome is installed
if command -v google-chrome &> /dev/null; then
    CHROME_USER_DATA="/home/${JARVIS_USER}/.openclaw/browser/openclaw/user-data"
    mkdir -p "$CHROME_USER_DATA"
    chown -R "${JARVIS_USER}:${JARVIS_USER}" "$CHROME_USER_DATA"

    # Use the service file from repo if available, otherwise inline it
    SERVICE_SOURCE="${JARVIS_DIR}/extensions/browser-dom/openclaw-chrome.service"
    if [ -f "$SERVICE_SOURCE" ]; then
        cp "$SERVICE_SOURCE" /etc/systemd/system/openclaw-chrome.service
        echo "Copied openclaw-chrome.service from repo"
    else
        cat > /etc/systemd/system/openclaw-chrome.service <<CHROMEEOF
[Unit]
Description=OpenClaw Headless Chrome (CDP :18800)
Documentation=https://github.com/AnirudhRanganathan/jarvis
After=network.target
Before=openclaw.service

[Service]
Type=simple
User=${JARVIS_USER}
Group=${JARVIS_USER}
Environment=HOME=/home/${JARVIS_USER}

# Remove stale singleton files from previous crashes
ExecStartPre=/bin/rm -f ${CHROME_USER_DATA}/SingletonLock
ExecStartPre=/bin/rm -f ${CHROME_USER_DATA}/SingletonCookie
ExecStartPre=/bin/rm -f ${CHROME_USER_DATA}/SingletonSocket

ExecStart=/usr/bin/google-chrome \\
    --headless=new \\
    --no-sandbox \\
    --disable-gpu \\
    --disable-dev-shm-usage \\
    --disable-software-rasterizer \\
    --disable-background-networking \\
    --disable-default-apps \\
    --disable-extensions \\
    --disable-sync \\
    --disable-translate \\
    --metrics-recording-only \\
    --no-first-run \\
    --mute-audio \\
    --remote-debugging-port=18800 \\
    --remote-debugging-address=127.0.0.1 \\
    --user-data-dir=${CHROME_USER_DATA} \\
    --window-size=1920,1080 \\
    about:blank

Restart=on-failure
RestartSec=5
TimeoutStopSec=10

# Resource limits
MemoryMax=1G
CPUQuota=150%

StandardOutput=journal
StandardError=journal
SyslogIdentifier=openclaw-chrome

[Install]
WantedBy=multi-user.target
CHROMEEOF
        echo "Created inline openclaw-chrome.service"
    fi

    systemctl daemon-reload
    systemctl enable openclaw-chrome
    echo "Chrome headless service created and enabled (CDP :18800)"
else
    echo -e "${RED}Warning: Google Chrome not found. Install it first:${NC}"
    echo "  wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"
    echo "  sudo apt install -y ./google-chrome-stable_current_amd64.deb"
fi

# =============================================================================
# Step 7: Install jq (needed for post-onboarding config)
# =============================================================================
echo -e "${YELLOW}[7/10] Checking jq...${NC}"
if ! command -v jq &> /dev/null; then
    apt-get install -y jq
    echo "jq installed"
else
    echo "jq already installed"
fi

# =============================================================================
# Step 8: Create post-onboarding helper script
# =============================================================================
echo -e "${YELLOW}[8/10] Creating post-onboarding config script...${NC}"

HELPER_SCRIPT="${JARVIS_DIR}/cloud/scripts/configure-openclaw-skill.sh"
cat > "$HELPER_SCRIPT" <<'SCRIPT'
#!/bin/bash
# =============================================================================
# Configure JARVIS skill env vars in OpenClaw
# =============================================================================
# Run this AFTER 'openclaw onboard' to inject JARVIS_ORCHESTRATOR_URL and
# OPENCLAW_GATEWAY_TOKEN into the skill.json file (NOT openclaw.json).
#
# skill.json env vars take precedence — OpenClaw reads them at skill load time
# and makes $JARVIS_ORCHESTRATOR_URL and $OPENCLAW_GATEWAY_TOKEN available
# to the agent when executing the skill.
#
# Usage (as jarvis user):
#   bash /opt/jarvis/cloud/scripts/configure-openclaw-skill.sh
#
# Optional (skip interactive prompt):
#   JARVIS_ORCHESTRATOR_URL=http://100.x.x.x:5000 bash configure-openclaw-skill.sh

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

OPENCLAW_CONFIG="$HOME/.openclaw/openclaw.json"
SKILL_JSON="$HOME/.openclaw/workspace/skills/jarvis-orchestrator/skill.json"

# Check openclaw.json exists (we read the gateway token from it)
if [ ! -f "$OPENCLAW_CONFIG" ]; then
    echo -e "${RED}Error: $OPENCLAW_CONFIG not found. Run 'openclaw onboard' first.${NC}"
    exit 1
fi

# Check skill.json exists (must be copied first)
if [ ! -f "$SKILL_JSON" ]; then
    echo -e "${RED}Error: $SKILL_JSON not found.${NC}"
    echo "Copy the skill first:"
    echo "  mkdir -p ~/.openclaw/workspace/skills/jarvis-orchestrator"
    echo "  cp /opt/jarvis/jarvis-orchestrator/skill/* ~/.openclaw/workspace/skills/jarvis-orchestrator/"
    exit 1
fi

# Read gateway token from existing config
GW_TOKEN=$(jq -r '.gateway.auth.token // empty' "$OPENCLAW_CONFIG")
if [ -z "$GW_TOKEN" ]; then
    echo -e "${RED}Error: No gateway token found in config. Run 'openclaw onboard' first.${NC}"
    exit 1
fi

# --- Orchestrator URL: auto-detect or ask interactively ---
if [ -n "$JARVIS_ORCHESTRATOR_URL" ]; then
    # Explicit env var — use it directly (non-interactive mode)
    ORCH_URL="$JARVIS_ORCHESTRATOR_URL"
else
    # Try to auto-detect Tailscale IP of the orchestrator VM
    TAILSCALE_IP=""
    if command -v tailscale &> /dev/null; then
        TAILSCALE_IP=$(tailscale ip -4 2>/dev/null || true)
    fi

    echo ""
    echo -e "${YELLOW}━━━ JARVIS Orchestrator URL ━━━${NC}"
    echo ""
    echo "  Where is the JARVIS orchestrator running?"
    echo ""
    echo -e "  ${CYAN}1)${NC} Same machine (localhost:5000)"
    if [ -n "$TAILSCALE_IP" ]; then
        echo -e "  ${CYAN}2)${NC} This machine via Tailscale (${TAILSCALE_IP}:5000)"
    fi
    echo -e "  ${CYAN}3)${NC} Different machine (enter IP manually)"
    echo ""

    read -p "  Choose [1/2/3]: " choice

    case "$choice" in
        1)
            ORCH_URL="http://localhost:5000"
            ;;
        2)
            if [ -n "$TAILSCALE_IP" ]; then
                ORCH_URL="http://${TAILSCALE_IP}:5000"
            else
                echo -e "${RED}Tailscale not found. Enter the IP manually.${NC}"
                read -p "  Orchestrator IP (e.g. 100.100.74.71): " CUSTOM_IP
                ORCH_URL="http://${CUSTOM_IP}:5000"
            fi
            ;;
        3)
            read -p "  Orchestrator IP or hostname (e.g. 100.100.74.71): " CUSTOM_IP
            # Add http:// and :5000 if not already present
            if [[ "$CUSTOM_IP" == http* ]]; then
                ORCH_URL="$CUSTOM_IP"
            else
                ORCH_URL="http://${CUSTOM_IP}:5000"
            fi
            ;;
        *)
            echo -e "${RED}Invalid choice. Using localhost:5000${NC}"
            ORCH_URL="http://localhost:5000"
            ;;
    esac
fi

echo ""
echo -e "${YELLOW}Configuring jarvis-orchestrator skill...${NC}"
echo "  Gateway token: ${GW_TOKEN:0:8}..."
echo "  Orchestrator URL: ${ORCH_URL}"

# Verify connectivity
echo -n "  Testing connection... "
if curl -sf --connect-timeout 5 "${ORCH_URL}/health" > /dev/null 2>&1; then
    echo -e "${GREEN}✓ reachable${NC}"
else
    echo -e "${YELLOW}⚠ unreachable (orchestrator may not be running yet)${NC}"
fi

# Write credentials into skill.json (skill env vars take precedence over openclaw.json)
jq --arg token "$GW_TOKEN" --arg url "$ORCH_URL" '
  .env.OPENCLAW_GATEWAY_TOKEN = $token |
  .env.JARVIS_ORCHESTRATOR_URL = $url
' "$SKILL_JSON" > "${SKILL_JSON}.tmp" && mv "${SKILL_JSON}.tmp" "$SKILL_JSON"

echo ""
echo -e "${GREEN}Done! Credentials written to skill.json${NC}"
echo "  File: ${SKILL_JSON}"
echo ""
echo "Restart OpenClaw to apply:"
echo "  sudo systemctl restart openclaw"
SCRIPT

chmod +x "$HELPER_SCRIPT"
chown "${JARVIS_USER}:${JARVIS_USER}" "$HELPER_SCRIPT"
echo "Helper script created: ${HELPER_SCRIPT}"

# =============================================================================
# Step 9: Create browser-dom config helper
# =============================================================================
echo -e "${YELLOW}[9/10] Creating browser-dom config helper...${NC}"

BROWSER_DOM_HELPER="${JARVIS_DIR}/cloud/scripts/configure-browser-dom.sh"
cat > "$BROWSER_DOM_HELPER" <<'BDSCRIPT'
#!/bin/bash
# =============================================================================
# Configure browser-dom plugin in OpenClaw config
# =============================================================================
# Run this AFTER 'openclaw onboard' to enable the browser-dom plugin.
#
# Usage (as jarvis user):
#   bash /opt/jarvis/cloud/scripts/configure-browser-dom.sh

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

OPENCLAW_CONFIG="$HOME/.openclaw/openclaw.json"

if [ ! -f "$OPENCLAW_CONFIG" ]; then
    echo -e "${RED}Error: $OPENCLAW_CONFIG not found. Run 'openclaw onboard' first.${NC}"
    exit 1
fi

echo -e "${YELLOW}Configuring browser-dom plugin...${NC}"

# Add browser-dom plugin config (must be under plugins.entries)
jq '
  .plugins.entries["browser-dom"] = {
    "enabled": true,
    "config": {
      "cdpUrl": "http://127.0.0.1:18800",
      "defaultTimeoutMs": 15000
    }
  }
' "$OPENCLAW_CONFIG" > "${OPENCLAW_CONFIG}.tmp" && mv "${OPENCLAW_CONFIG}.tmp" "$OPENCLAW_CONFIG"

echo -e "${GREEN}Done! browser-dom plugin configured in openclaw.json${NC}"
echo ""
echo "Start services:"
echo "  sudo systemctl start openclaw-chrome   # Chrome headless (CDP :18800)"
echo "  sudo systemctl restart openclaw         # Reload plugin"
echo ""
echo "Verify:"
echo "  curl -s http://127.0.0.1:18800/json/version   # Chrome CDP"
echo "  journalctl -u openclaw -f | grep browser-dom   # Plugin loaded"
BDSCRIPT

chmod +x "$BROWSER_DOM_HELPER"
chown "${JARVIS_USER}:${JARVIS_USER}" "$BROWSER_DOM_HELPER"
echo "browser-dom config helper created: ${BROWSER_DOM_HELPER}"

# =============================================================================
# Step 10: Nginx TLS reverse proxy (for non-loopback access)
# =============================================================================
echo -e "${YELLOW}[10/10] Setting up Nginx TLS reverse proxy for OpenClaw...${NC}"

# OpenClaw requires TLS for non-loopback connections.
# Nginx acts as TLS-terminating reverse proxy on the OpenClaw server itself,
# binding to the Tailscale IP so the orchestrator connects via wss://.

TAILSCALE_IP="${TAILSCALE_IP:-100.116.99.9}"
OPENCLAW_DOMAIN="openclaw.mintwork.it"
OPENCLAW_CERTS_DIR="/home/${JARVIS_USER}/.openclaw/certs"

# 10a: Install nginx + certbot + cloudflare DNS plugin
if ! command -v nginx &> /dev/null; then
    apt-get update
    apt-get install -y nginx
    systemctl enable nginx
    echo "Nginx installed"
else
    echo "Nginx already installed"
fi

apt-get install -y certbot python3-certbot-dns-cloudflare

# 10b: Cloudflare credentials
if [ -z "$CLOUDFLARE_API_TOKEN" ]; then
    echo -e "${YELLOW}CLOUDFLARE_API_TOKEN not set — skipping cert generation.${NC}"
    echo "Run this step manually later:"
    echo "  sudo CLOUDFLARE_API_TOKEN=<token> bash $0"
    echo ""
    echo "Or generate the cert manually:"
    echo "  sudo certbot certonly --dns-cloudflare \\"
    echo "    --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \\"
    echo "    -d ${OPENCLAW_DOMAIN} --email admin@mintwork.it --agree-tos"
else
    CLOUDFLARE_CREDS="/etc/letsencrypt/cloudflare.ini"
    cat > "$CLOUDFLARE_CREDS" <<CFCREDS
dns_cloudflare_api_token = ${CLOUDFLARE_API_TOKEN}
CFCREDS
    chmod 600 "$CLOUDFLARE_CREDS"

    # 10c: Generate Let's Encrypt cert for openclaw.mintwork.it
    CERTBOT_EMAIL="${CERTBOT_EMAIL:-admin@mintwork.it}"
    echo "Generating TLS certificate for ${OPENCLAW_DOMAIN}..."
    certbot certonly \
        --dns-cloudflare \
        --dns-cloudflare-credentials "$CLOUDFLARE_CREDS" \
        --dns-cloudflare-propagation-seconds 30 \
        -d "$OPENCLAW_DOMAIN" \
        --email "$CERTBOT_EMAIL" \
        --agree-tos \
        --non-interactive

    echo "Certificate generated for ${OPENCLAW_DOMAIN}"
fi

# 10d: Create cert directory and deploy hook for auto-renewal
mkdir -p "$OPENCLAW_CERTS_DIR"
chown "${JARVIS_USER}:${JARVIS_USER}" "$OPENCLAW_CERTS_DIR"

DEPLOY_HOOK="/etc/letsencrypt/renewal-hooks/deploy/openclaw-nginx.sh"
cat > "$DEPLOY_HOOK" <<HOOK
#!/bin/bash
# Certbot deploy hook: copy renewed certs to OpenClaw dir and reload nginx
DOMAIN="${OPENCLAW_DOMAIN}"
CERT_SRC="/etc/letsencrypt/live/\${DOMAIN}"
CERT_DST="${OPENCLAW_CERTS_DIR}"

cp "\${CERT_SRC}/fullchain.pem" "\${CERT_DST}/fullchain.pem"
cp "\${CERT_SRC}/privkey.pem"   "\${CERT_DST}/privkey.pem"
chown ${JARVIS_USER}:${JARVIS_USER} "\${CERT_DST}"/*.pem
chmod 640 "\${CERT_DST}"/*.pem
systemctl reload nginx
HOOK
chmod +x "$DEPLOY_HOOK"
echo "Certbot deploy hook created: ${DEPLOY_HOOK}"

# Run the deploy hook once now to copy initial certs
if [ -d "/etc/letsencrypt/live/${OPENCLAW_DOMAIN}" ]; then
    bash "$DEPLOY_HOOK"
    echo "Initial certs copied to ${OPENCLAW_CERTS_DIR}"
fi

# 10e: Create nginx config for OpenClaw TLS proxy
cat > /etc/nginx/sites-available/openclaw-tls.conf <<NGINXCONF
# =============================================================================
# OpenClaw TLS Reverse Proxy (on the OpenClaw server itself)
# =============================================================================
# Binds to Tailscale IP — non-loopback clients connect via TLS.
# OpenClaw listens on ws://127.0.0.1:18789 (loopback only).

# API/WebSocket on port 18789 (TLS)
server {
    listen ${TAILSCALE_IP}:18789 ssl http2;
    server_name ${OPENCLAW_DOMAIN};

    ssl_certificate     ${OPENCLAW_CERTS_DIR}/fullchain.pem;
    ssl_certificate_key ${OPENCLAW_CERTS_DIR}/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;

    # Proxy to OpenClaw on loopback
    location / {
        proxy_pass http://127.0.0.1:18789;
        proxy_http_version 1.1;

        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # WebSocket support
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection 'upgrade';

        # Timeouts (OpenClaw LLM calls can be slow)
        proxy_connect_timeout 60s;
        proxy_send_timeout 300s;
        proxy_read_timeout 300s;

        # Disable buffering for streaming
        proxy_buffering off;
        proxy_cache off;

        client_max_body_size 10M;
    }
}

# Dashboard on port 443 (TLS)
server {
    listen ${TAILSCALE_IP}:443 ssl http2;
    server_name ${OPENCLAW_DOMAIN};

    ssl_certificate     ${OPENCLAW_CERTS_DIR}/fullchain.pem;
    ssl_certificate_key ${OPENCLAW_CERTS_DIR}/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;

    # Same backend — dashboard is served on the same port
    location / {
        proxy_pass http://127.0.0.1:18789;
        proxy_http_version 1.1;

        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection 'upgrade';

        proxy_connect_timeout 60s;
        proxy_send_timeout 300s;
        proxy_read_timeout 300s;

        proxy_buffering off;
        proxy_cache off;

        client_max_body_size 10M;
    }
}
NGINXCONF

ln -sf /etc/nginx/sites-available/openclaw-tls.conf /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default

echo "Nginx TLS config created: /etc/nginx/sites-available/openclaw-tls.conf"

# 10f: Add dashboard origin to OpenClaw allowedOrigins
OPENCLAW_CONFIG="/home/${JARVIS_USER}/.openclaw/openclaw.json"
if [ -f "$OPENCLAW_CONFIG" ] && command -v jq &> /dev/null; then
    # Add https://openclaw.mintwork.it to controlUi.allowedOrigins if not already present
    ORIGIN="https://${OPENCLAW_DOMAIN}"
    HAS_ORIGIN=$(jq --arg o "$ORIGIN" '
        .gateway.controlUi.allowedOrigins // [] | index($o)
    ' "$OPENCLAW_CONFIG")

    if [ "$HAS_ORIGIN" = "null" ]; then
        jq --arg o "$ORIGIN" '
            .gateway.controlUi.allowedOrigins = (
                (.gateway.controlUi.allowedOrigins // []) + [$o]
            )
        ' "$OPENCLAW_CONFIG" > "${OPENCLAW_CONFIG}.tmp" && mv "${OPENCLAW_CONFIG}.tmp" "$OPENCLAW_CONFIG"
        chown "${JARVIS_USER}:${JARVIS_USER}" "$OPENCLAW_CONFIG"
        echo "Added ${ORIGIN} to controlUi.allowedOrigins"
    else
        echo "${ORIGIN} already in controlUi.allowedOrigins"
    fi
else
    echo -e "${YELLOW}openclaw.json not found — run 'openclaw onboard' first, then add manually:${NC}"
    echo "  jq '.gateway.controlUi.allowedOrigins += [\"https://${OPENCLAW_DOMAIN}\"]' ~/.openclaw/openclaw.json > /tmp/oc.json && mv /tmp/oc.json ~/.openclaw/openclaw.json"
fi

# 10g: Test and reload nginx
echo "Testing nginx configuration..."
nginx -t
systemctl reload nginx
echo "Nginx TLS proxy active on ${TAILSCALE_IP}:18789 and ${TAILSCALE_IP}:443"

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
echo -e "3. ${YELLOW}Configura browser-dom (dopo onboarding):${NC}"
echo "   bash /opt/jarvis/cloud/scripts/configure-browser-dom.sh"
echo ""
echo -e "4. ${YELLOW}Avvia i servizi:${NC}"
echo "   sudo systemctl start openclaw-chrome   # Chrome headless"
echo "   sudo systemctl start openclaw           # OpenClaw gateway"
echo "   sudo systemctl status openclaw-chrome openclaw nginx"
echo ""
echo -e "5. ${YELLOW}Verifica:${NC}"
echo "   curl -s http://127.0.0.1:18800/json/version    # Chrome CDP"
echo "   curl http://localhost:18789/health               # OpenClaw (loopback)"
echo "   curl -k https://openclaw.mintwork.it:18789/health  # OpenClaw (TLS)"
echo "   ls -la ~/.openclaw/workspace/skills/jarvis-orchestrator/"
echo ""
echo -e "6. ${YELLOW}Dashboard (TLS):${NC}"
echo "   https://openclaw.mintwork.it"
echo ""
echo -e "7. ${YELLOW}TLS architecture:${NC}"
echo "   OpenClaw: ws://127.0.0.1:18789     (loopback only)"
echo "   Nginx:    wss://openclaw.mintwork.it:18789  (API/WS via Tailscale)"
echo "             https://openclaw.mintwork.it      (Dashboard via Tailscale)"
echo "   Cert:     Let's Encrypt (auto-renewal via certbot + Cloudflare DNS)"
echo ""
echo -e "8. ${YELLOW}Logs:${NC}"
echo "   journalctl -u openclaw-chrome -f   # Chrome headless"
echo "   journalctl -u openclaw -f          # OpenClaw gateway"
echo "   tail -f /var/log/nginx/error.log   # Nginx TLS proxy"
echo ""
echo -e "${GREEN}Enjoy OpenClaw + JARVIS!${NC}"
