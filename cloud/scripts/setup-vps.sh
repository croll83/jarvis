#!/bin/bash
# =============================================================================
# JARVIS VPS Setup Script
# =============================================================================
# Run as root on a fresh VPS (Ubuntu 22.04/24.04 LTS recommended)
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/croll83/jarvis/main/cloud/scripts/setup-vps.sh | bash
#
# Or download and run:
#   wget https://raw.githubusercontent.com/croll83/jarvis/main/cloud/scripts/setup-vps.sh
#   chmod +x setup-vps.sh
#   sudo ./setup-vps.sh
#
# NOTE: Tailscale gira host-level (non in Docker) per connettere tutti i servizi.
#       OpenClaw gira bare-metal (Node.js), NON in Docker.

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

TOTAL_STEPS=12

echo -e "${GREEN}"
echo "=============================================="
echo "       JARVIS VPS Setup Script"
echo "=============================================="
echo -e "${NC}"

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Please run as root${NC}"
    exit 1
fi

# =============================================================================
# System Update
# =============================================================================
echo -e "${YELLOW}[1/${TOTAL_STEPS}] Updating system...${NC}"
apt update && apt upgrade -y

# =============================================================================
# Install Docker
# =============================================================================
echo -e "${YELLOW}[2/${TOTAL_STEPS}] Installing Docker...${NC}"
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
else
    echo "Docker already installed"
fi

# Install Docker Compose plugin
apt install -y docker-compose-plugin

# Verify installation
docker --version
docker compose version

# =============================================================================
# Install Node.js 22 + OpenClaw (bare-metal)
# =============================================================================
echo -e "${YELLOW}[3/${TOTAL_STEPS}] Installing Node.js 22 + OpenClaw...${NC}"
if command -v node &> /dev/null; then
    NODE_VERSION=$(node -v | cut -d'v' -f2 | cut -d'.' -f1)
    if [ "$NODE_VERSION" -ge 22 ]; then
        echo "Node.js $(node -v) already installed"
    else
        echo "Upgrading Node.js to 22..."
        curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
        apt-get install -y nodejs
    fi
else
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
    apt-get install -y nodejs
fi

npm install -g openclaw@latest
echo "Node: $(node -v), npm: $(npm -v)"
echo "OpenClaw: $(openclaw --version 2>/dev/null || echo 'installed')"

# =============================================================================
# Install Tailscale (host-level)
# =============================================================================
echo -e "${YELLOW}[4/${TOTAL_STEPS}] Installing and connecting Tailscale...${NC}"
if ! command -v tailscale &> /dev/null; then
    curl -fsSL https://tailscale.com/install.sh | sh
    echo "Tailscale installed"
fi

# Connetti Tailscale (se non già connesso)
if ! tailscale status &>/dev/null; then
    if [ -n "${TAILSCALE_AUTHKEY:-}" ]; then
        echo "Connecting Tailscale with auth key (headless)..."
        tailscale up --hostname=jarvis-cloud --authkey="$TAILSCALE_AUTHKEY"
        echo "Tailscale connected as jarvis-cloud"
    else
        echo ""
        echo -e "${YELLOW}IMPORTANTE: Tailscale non connesso.${NC}"
        echo "Opzione 1 (headless — consigliata per script):"
        echo "   export TAILSCALE_AUTHKEY=tskey-auth-xxxxx"
        echo "   # Genera da: https://login.tailscale.com/admin/settings/keys"
        echo "   # Poi rilancia questo script"
        echo ""
        echo "Opzione 2 (interattiva):"
        echo "   tailscale up --hostname=jarvis-cloud"
        echo "   # Apri il link nel browser per autenticare"
    fi
else
    echo "Tailscale already connected"
    tailscale status | head -3
fi

# =============================================================================
# Install Nginx + Certbot
# =============================================================================
echo -e "${YELLOW}[5/${TOTAL_STEPS}] Installing Nginx and Certbot...${NC}"
apt install -y nginx certbot python3-certbot-nginx

# Enable nginx
systemctl enable nginx
systemctl start nginx

# =============================================================================
# Create JARVIS User
# =============================================================================
echo -e "${YELLOW}[6/${TOTAL_STEPS}] Creating jarvis user...${NC}"
if ! id "jarvis" &>/dev/null; then
    adduser --disabled-password --gecos "JARVIS System User" jarvis
    usermod -aG docker jarvis
    # Sudo per jarvis (necessario per brew/openclaw)
    echo "jarvis ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/jarvis
    chmod 440 /etc/sudoers.d/jarvis
    echo "User 'jarvis' created and added to docker group"
else
    echo "User 'jarvis' already exists"
    # Assicura che abbia accesso docker e sudo
    usermod -aG docker jarvis 2>/dev/null || true
    if [ ! -f /etc/sudoers.d/jarvis ]; then
        echo "jarvis ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/jarvis
        chmod 440 /etc/sudoers.d/jarvis
    fi
fi

# Abilita linger per jarvis: permette sessione D-Bus e systemd --user
# senza login attivo (necessario per OpenClaw gateway)
loginctl enable-linger jarvis 2>/dev/null || true

# Crea /run/user/<uid> per jarvis (necessario per systemctl --user)
# Normalmente creato da PAM al login, ma su VPS headless non avviene mai.
# tmpfiles.d lo ricrea automaticamente ad ogni boot.
JARVIS_UID=$(id -u jarvis)
mkdir -p "/run/user/${JARVIS_UID}"
chown jarvis:jarvis "/run/user/${JARVIS_UID}"
chmod 700 "/run/user/${JARVIS_UID}"

# Rendi persistente al reboot via tmpfiles.d
echo "d /run/user/${JARVIS_UID} 0700 jarvis jarvis -" > /etc/tmpfiles.d/jarvis-runtime.conf

# =============================================================================
# Setup Directories
# =============================================================================
echo -e "${YELLOW}[7/${TOTAL_STEPS}] Setting up directories...${NC}"
mkdir -p /opt/jarvis
mkdir -p /opt/jarvis/data
mkdir -p /opt/jarvis/config
mkdir -p /opt/jarvis/voice_models
mkdir -p /var/www/certbot

# OpenClaw directories
mkdir -p /home/jarvis/.openclaw/workspace/skills

chown -R jarvis:jarvis /opt/jarvis
chown -R jarvis:jarvis /home/jarvis/.openclaw

# Install useful tools
apt install -y htop curl wget git vim nano jq

# =============================================================================
# Install Google Chrome (for OpenClaw headless browser)
# =============================================================================
echo -e "${YELLOW}[8/${TOTAL_STEPS}] Installing Google Chrome (headless browser)...${NC}"
if ! command -v google-chrome &> /dev/null; then
    # Non usare snap: manda Chrome in sandbox e non funziona con OpenClaw.
    # Installiamo il .deb ufficiale direttamente da Google.
    wget -q -O /tmp/google-chrome-stable.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    apt install -y /tmp/google-chrome-stable.deb
    rm -f /tmp/google-chrome-stable.deb
    echo "Google Chrome $(google-chrome --version 2>/dev/null || echo 'installed')"
else
    echo "Google Chrome already installed: $(google-chrome --version 2>/dev/null)"
fi

# =============================================================================
# Install build-essential (required for native compilation, wacli, etc.)
# =============================================================================
echo -e "${YELLOW}[9/${TOTAL_STEPS}] Installing build-essential...${NC}"
apt install -y build-essential
echo "build-essential installed (gcc, g++, make)"

# =============================================================================
# Install Linuxbrew (as jarvis user)
# =============================================================================
echo -e "${YELLOW}[10/${TOTAL_STEPS}] Installing Linuxbrew...${NC}"
if [ -d "/home/linuxbrew/.linuxbrew" ]; then
    echo "Linuxbrew already installed"
else
    echo "Installing Homebrew for Linux..."
    # Homebrew richiede installazione come utente non-root
    sudo -u jarvis bash -c 'NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    echo "Linuxbrew installed"
fi

# Aggiungi brew al PATH di jarvis (persistente)
BREW_SHELLENV='eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"'
JARVIS_BASHRC="/home/jarvis/.bashrc"
if ! grep -q "linuxbrew" "$JARVIS_BASHRC" 2>/dev/null; then
    echo "" >> "$JARVIS_BASHRC"
    echo "# Linuxbrew" >> "$JARVIS_BASHRC"
    echo "$BREW_SHELLENV" >> "$JARVIS_BASHRC"
    chown jarvis:jarvis "$JARVIS_BASHRC"
    echo "Brew added to jarvis .bashrc"
fi

# =============================================================================
# Install OpenClaw Skill Dependencies (brew + npm, as jarvis user)
# =============================================================================
echo -e "${YELLOW}[11/${TOTAL_STEPS}] Installing OpenClaw skill dependencies...${NC}"

# Tutte le installazioni brew/npm vengono eseguite come utente jarvis
# con brew nel PATH via eval shellenv.
sudo -u jarvis bash -c '
    eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"

    echo "  Installing brew taps and packages..."

    # steipete tap (codexbar, gogcli, gifgrep, goplaces, wacli)
    brew tap steipete/tap 2>/dev/null || true

    # gcc (needed by some brew packages)
    echo "  Installing gcc..."
    brew install gcc 2>/dev/null || true
    brew postinstall gcc 2>/dev/null || true
    # Crea symlink gcc -> gcc-15 (o la versione installata)
    GCC_VERSION=$(ls /home/linuxbrew/.linuxbrew/bin/gcc-* 2>/dev/null | grep -oP "gcc-\K[0-9]+" | sort -n | tail -1)
    if [ -n "$GCC_VERSION" ] && [ ! -e "/home/linuxbrew/.linuxbrew/bin/gcc" ]; then
        ln -sf "/home/linuxbrew/.linuxbrew/bin/gcc-${GCC_VERSION}" "/home/linuxbrew/.linuxbrew/bin/gcc"
        ln -sf "/home/linuxbrew/.linuxbrew/bin/g++-${GCC_VERSION}" "/home/linuxbrew/.linuxbrew/bin/g++"
        echo "  Symlinked gcc -> gcc-${GCC_VERSION}"
    fi

    # Skill tools
    echo "  Installing codexbar..."
    brew install steipete/tap/codexbar 2>/dev/null || true

    echo "  Installing ffmpeg..."
    brew install ffmpeg-full 2>/dev/null || true

    echo "  Installing gogcli..."
    brew install steipete/tap/gogcli 2>/dev/null || true

    echo "  Installing gifgrep..."
    brew install steipete/tap/gifgrep 2>/dev/null || true

    echo "  Installing goplaces..."
    brew install steipete/tap/goplaces 2>/dev/null || true

    echo "  Installing wacli..."
    brew install steipete/tap/wacli 2>/dev/null || true

    # npm global packages
    echo "  Installing summarize (npm)..."
    npm i -g @steipete/summarize 2>/dev/null || true

    echo "  Skill dependencies installed!"
'

# Create openclaw.env (if not exists) — loaded by systemd EnvironmentFile
if [ ! -f /home/jarvis/.openclaw/openclaw.env ]; then
    if [ -f /opt/jarvis/cloud/openclaw.env.example ]; then
        cp /opt/jarvis/cloud/openclaw.env.example /home/jarvis/.openclaw/openclaw.env
    else
        # Crea un env minimo se il repo non è ancora clonato
        cat > /home/jarvis/.openclaw/openclaw.env <<'ENVEOF'
# OpenClaw env vars — compila dopo il setup
# gogcli
GOG_KEYRING_PASSWORD=
GOG_ACCOUNT=
ENVEOF
    fi
    chown jarvis:jarvis /home/jarvis/.openclaw/openclaw.env
    chmod 600 /home/jarvis/.openclaw/openclaw.env
    echo "openclaw.env created in ~/.openclaw/ (edit with your values)"
fi

# Crea directory per gogcli credentials (da copiare manualmente)
mkdir -p /home/jarvis/.config/gog
chown -R jarvis:jarvis /home/jarvis/.config/gog

# =============================================================================
# Configure firewall + swap + services
# =============================================================================
echo -e "${YELLOW}[12/${TOTAL_STEPS}] Configuring firewall, swap, and services...${NC}"

# --- UFW Firewall ---
apt install -y ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow http
ufw allow https
ufw allow 41641/udp  # Tailscale NAT traversal
echo "y" | ufw enable

# --- Swap (2 GB) ---
if [ ! -f /swapfile ]; then
    echo "Creating swap file..."
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
    echo "2GB swap file created"
else
    echo "Swap already configured"
fi

# --- Docker log rotation ---
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'DAEMON_EOF'
{
    "log-driver": "json-file",
    "log-opts": {
        "max-size": "10m",
        "max-file": "3"
    }
}
DAEMON_EOF
systemctl restart docker

# --- OpenClaw systemd service ---
echo "Creating OpenClaw systemd service..."
OPENCLAW_BIN=$(which openclaw 2>/dev/null || echo "/usr/bin/openclaw")

# Brew paths per OpenClaw (headless browser, skill tools)
BREW_PREFIX="/home/linuxbrew/.linuxbrew"

cat > /etc/systemd/system/openclaw.service <<SVCEOF
[Unit]
Description=OpenClaw AI Gateway
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=jarvis
Group=jarvis
Environment=NODE_ENV=production
Environment=HOME=/home/jarvis
Environment=PATH=${BREW_PREFIX}/bin:${BREW_PREFIX}/sbin:/usr/local/bin:/usr/bin:/bin
Environment=CHROME_PATH=/usr/bin/google-chrome
Environment=PUPPETEER_EXECUTABLE_PATH=/usr/bin/google-chrome
EnvironmentFile=-/home/jarvis/.openclaw/openclaw.env
KillSignal=SIGINT
KillMode=mixed
TimeoutStopSec=15
ExecStart=${OPENCLAW_BIN} gateway run
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=openclaw

NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/home/jarvis/.openclaw /home/jarvis/.npm /home/jarvis/.config /home/jarvis/.cache /tmp
PrivateTmp=false

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable openclaw

# =============================================================================
# Summary
# =============================================================================
echo ""
echo -e "${GREEN}=============================================="
echo "       Setup Complete!"
echo "==============================================${NC}"
echo ""
echo "Installed components:"
echo "  - Docker + Compose"
echo "  - Node.js 22 + OpenClaw"
echo "  - Tailscale (host-level)"
echo "  - Nginx + Certbot"
echo "  - Google Chrome (headless browser)"
echo "  - Linuxbrew + skill dependencies"
echo "    (codexbar, ffmpeg, gcc, gogcli, gifgrep, goplaces, wacli, summarize)"
echo ""
echo "Next steps:"
echo ""
echo -e "1. ${YELLOW}Verifica Tailscale (se non connesso nello step 4):${NC}"
echo "   tailscale status   # se non connesso:"
echo "   tailscale up --hostname=jarvis-cloud"
echo ""
echo -e "2. ${YELLOW}Clone repository:${NC}"
echo "   su - jarvis"
echo "   git clone https://github.com/croll83/jarvis.git /opt/jarvis"
echo ""
echo -e "3. ${YELLOW}Setup OpenClaw + JARVIS skill:${NC}"
echo "   sudo bash /opt/jarvis/cloud/scripts/setup-openclaw.sh"
echo ""
echo -e "4. ${YELLOW}OpenClaw onboarding (identita, API key, Telegram):${NC}"
echo "   openclaw onboard"
echo ""
echo -e "5. ${YELLOW}Configura skill JARVIS (dopo onboarding):${NC}"
echo "   bash /opt/jarvis/cloud/scripts/configure-openclaw-skill.sh"
echo ""
echo -e "6. ${YELLOW}Configura OpenClaw env vars (gogcli, etc.):${NC}"
echo "   nano ~/.openclaw/openclaw.env  # GOG_KEYRING_PASSWORD, GOG_ACCOUNT"
echo "   # Copia credenziali gogcli:"
echo "   scp -r ~/.config/gog/credentials.json jarvis@<vps>:~/.config/gog/"
echo "   scp -r ~/.config/gog/keyring/ jarvis@<vps>:~/.config/gog/"
echo ""
echo -e "7. ${YELLOW}Configure JARVIS environment:${NC}"
echo "   cd /opt/jarvis/cloud"
echo "   cp .env.example .env"
echo "   nano .env  # API keys + same OPENCLAW_GATEWAY_TOKEN"
echo ""
echo -e "8. ${YELLOW}Start OpenClaw + JARVIS:${NC}"
echo "   sudo systemctl start openclaw"
echo "   cd /opt/jarvis/cloud"
echo "   docker compose -f docker-compose.cloud.yml up -d"
echo ""
echo -e "9. ${YELLOW}Verify:${NC}"
echo "   curl http://\$(tailscale ip -4):18789/health   # OpenClaw"
echo "   curl http://localhost:5000/health              # Orchestrator"
echo "   tailscale status                               # Tailscale"
echo "   google-chrome --version                        # Chrome"
echo "   su - jarvis -c 'brew --version'                # Brew"
echo ""
echo -e "10. ${YELLOW}Setup SSL (after DNS):${NC}"
echo "   sudo cp /opt/jarvis/cloud/nginx/jarvis.conf /etc/nginx/sites-available/"
echo "   sudo ln -s /etc/nginx/sites-available/jarvis.conf /etc/nginx/sites-enabled/"
echo "   sudo nginx -t && sudo systemctl reload nginx"
echo "   sudo certbot --nginx -d jarvis.yourdomain.com"
echo ""
echo -e "${GREEN}Enjoy JARVIS!${NC}"
