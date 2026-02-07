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
# NOTE: Tailscale NON viene installato sull'host.
#       Gira come container Docker dentro il compose (vedi docker-compose.cloud.yml).
#       Il VPS si raggiunge direttamente via IP pubblico + SSH.

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
echo -e "${YELLOW}[1/6] Updating system...${NC}"
apt update && apt upgrade -y

# =============================================================================
# Install Docker
# =============================================================================
echo -e "${YELLOW}[2/6] Installing Docker...${NC}"
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
# Install Nginx + Certbot
# =============================================================================
echo -e "${YELLOW}[3/6] Installing Nginx and Certbot...${NC}"
apt install -y nginx certbot python3-certbot-nginx

# Enable nginx
systemctl enable nginx
systemctl start nginx

# =============================================================================
# Create JARVIS User
# =============================================================================
echo -e "${YELLOW}[4/6] Creating jarvis user...${NC}"
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
echo -e "${YELLOW}[5/6] Setting up directories...${NC}"
mkdir -p /opt/jarvis
mkdir -p /opt/jarvis/data
mkdir -p /opt/jarvis/voice_models
mkdir -p /var/www/certbot

chown -R jarvis:jarvis /opt/jarvis

# =============================================================================
# Install useful tools
# =============================================================================
apt install -y htop curl wget git vim nano jq

# =============================================================================
# Configure firewall
# =============================================================================
echo -e "${YELLOW}[6/6] Configuring firewall (ufw)...${NC}"
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
echo -e "2. ${YELLOW}Configure environment:${NC}"
echo "   cd /opt/jarvis/cloud"
echo "   cp .env.example .env"
echo "   nano .env  # Add your API keys + TAILSCALE_AUTHKEY"
echo ""
echo -e "3. ${YELLOW}Start JARVIS:${NC}"
echo "   docker compose -f docker-compose.cloud.yml up -d"
echo ""
echo -e "4. ${YELLOW}Verify Tailscale (runs as container):${NC}"
echo "   docker exec jarvis_tailscale tailscale status"
echo ""
echo -e "5. ${YELLOW}Setup SSL (after DNS is configured):${NC}"
echo "   sudo cp nginx/jarvis.conf /etc/nginx/sites-available/"
echo "   sudo ln -s /etc/nginx/sites-available/jarvis.conf /etc/nginx/sites-enabled/"
echo "   sudo nginx -t && sudo systemctl reload nginx"
echo "   sudo certbot --nginx -d jarvis.tuodominio.it"
echo ""
echo -e "${GREEN}Enjoy JARVIS!${NC}"
