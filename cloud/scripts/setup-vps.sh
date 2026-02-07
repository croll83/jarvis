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
# NOTE: Tailscale gira come container Docker, NON sull'host.
#       OpenClaw gira bare-metal (Node.js), NON in Docker.

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

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
echo -e "${YELLOW}[1/7] Updating system...${NC}"
apt update && apt upgrade -y

# =============================================================================
# Install Docker
# =============================================================================
echo -e "${YELLOW}[2/7] Installing Docker...${NC}"
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
echo -e "${YELLOW}[3/7] Installing Node.js 22 + OpenClaw...${NC}"
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
# Install Nginx + Certbot
# =============================================================================
echo -e "${YELLOW}[4/7] Installing Nginx and Certbot...${NC}"
apt install -y nginx certbot python3-certbot-nginx

# Enable nginx
systemctl enable nginx
systemctl start nginx

# =============================================================================
# Create JARVIS User
# =============================================================================
echo -e "${YELLOW}[5/7] Creating jarvis user...${NC}"
if ! id "jarvis" &>/dev/null; then
    adduser --disabled-password --gecos "JARVIS System User" jarvis
    usermod -aG docker jarvis
    echo "User 'jarvis' created and added to docker group"
else
    echo "User 'jarvis' already exists"
fi

# =============================================================================
# Setup Directories
# =============================================================================
echo -e "${YELLOW}[6/7] Setting up directories...${NC}"
mkdir -p /opt/jarvis
mkdir -p /opt/jarvis/data
mkdir -p /opt/jarvis/config
mkdir -p /opt/jarvis/voice_models
mkdir -p /var/www/certbot

# OpenClaw directories
mkdir -p /home/jarvis/.openclaw/skills
mkdir -p /home/jarvis/.openclaw/workspace

chown -R jarvis:jarvis /opt/jarvis
chown -R jarvis:jarvis /home/jarvis/.openclaw

# Install useful tools
apt install -y htop curl wget git vim nano jq

# =============================================================================
# Configure firewall
# =============================================================================
echo -e "${YELLOW}[7/7] Configuring firewall (ufw)...${NC}"
apt install -y ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow http
ufw allow https
ufw allow 41641/udp  # Tailscale (usato dal container Docker)
echo "y" | ufw enable

# =============================================================================
# Setup swap (if not present)
# =============================================================================
if [ ! -f /swapfile ]; then
    echo -e "${YELLOW}Creating swap file...${NC}"
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
    echo "2GB swap file created"
else
    echo "Swap already configured"
fi

# =============================================================================
# Docker log rotation
# =============================================================================
echo -e "${YELLOW}Configuring Docker log rotation...${NC}"
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

# =============================================================================
# Create OpenClaw systemd service
# =============================================================================
echo -e "${YELLOW}Creating OpenClaw systemd service...${NC}"
cat > /etc/systemd/system/openclaw.service <<'SVCEOF'
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
ExecStart=/usr/bin/openclaw gateway run
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=openclaw

NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/jarvis/.openclaw
PrivateTmp=true

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
echo "Next steps:"
echo ""
echo -e "1. ${YELLOW}Clone repository:${NC}"
echo "   su - jarvis"
echo "   git clone https://github.com/croll83/jarvis.git /opt/jarvis"
echo ""
echo -e "2. ${YELLOW}Symlink JARVIS skill per OpenClaw:${NC}"
echo "   ln -s /opt/jarvis/jarvis-orchestrator/skill ~/.openclaw/skills/jarvis-orchestrator"
echo ""
echo -e "3. ${YELLOW}OpenClaw onboarding (identita, API key, Telegram):${NC}"
echo "   openclaw onboard"
echo ""
echo -e "4. ${YELLOW}Configure JARVIS environment:${NC}"
echo "   cd /opt/jarvis/cloud"
echo "   cp .env.example .env"
echo "   nano .env  # API keys + TAILSCALE_AUTHKEY + same OPENCLAW_GATEWAY_TOKEN"
echo ""
echo -e "5. ${YELLOW}Start OpenClaw + JARVIS:${NC}"
echo "   sudo systemctl start openclaw"
echo "   docker compose -f docker-compose.cloud.yml up -d"
echo ""
echo -e "6. ${YELLOW}Verify:${NC}"
echo "   curl http://localhost:18789/health   # OpenClaw"
echo "   curl http://localhost:5000/health    # Orchestrator"
echo "   docker exec jarvis_tailscale tailscale status"
echo ""
echo -e "7. ${YELLOW}Setup SSL (after DNS):${NC}"
echo "   sudo cp /opt/jarvis/cloud/nginx/jarvis.conf /etc/nginx/sites-available/"
echo "   sudo ln -s /etc/nginx/sites-available/jarvis.conf /etc/nginx/sites-enabled/"
echo "   sudo nginx -t && sudo systemctl reload nginx"
echo "   sudo certbot --nginx -d jarvis.yourdomain.com"
echo ""
echo -e "${GREEN}Enjoy JARVIS!${NC}"
