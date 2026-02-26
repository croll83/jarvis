#!/bin/bash
# =============================================================================
# Nginx + SSL Setup Script (certbot DNS Cloudflare)
# =============================================================================
# Installa Nginx, configura il vhost per jarvis.mintwork.it e genera il
# certificato SSL tramite Cloudflare DNS challenge.
#
# NOTA: openclaw.mintwork.it ha il proprio certificato TLS gestito dal nginx
# sul server OpenClaw (vedi setup-openclaw.sh Step 10). Il VPS non genera
# piu' il cert per openclaw.mintwork.it.
#
# Prerequisiti:
#   - JARVIS repo clonato in /opt/jarvis
#   - Un Cloudflare API token con permesso "Zone:DNS:Edit"
#     (crealo su https://dash.cloudflare.com/profile/api-tokens)
#   - Record A su Cloudflare:
#     jarvis.mintwork.it -> <tailscale-ip>
#
# Usage:
#   sudo CLOUDFLARE_API_TOKEN=<token> bash /opt/jarvis/cloud/scripts/setup-nginx.sh
#
# Oppure con variabile d'ambiente gia esportata:
#   export CLOUDFLARE_API_TOKEN=<token>
#   sudo -E bash /opt/jarvis/cloud/scripts/setup-nginx.sh

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}"
echo "=============================================="
echo "       Nginx + SSL Setup (Cloudflare DNS)"
echo "=============================================="
echo -e "${NC}"

# Check root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Please run as root${NC}"
    exit 1
fi

# Check Cloudflare token
if [ -z "$CLOUDFLARE_API_TOKEN" ]; then
    echo -e "${RED}Error: CLOUDFLARE_API_TOKEN not set.${NC}"
    echo ""
    echo "Create an API token at https://dash.cloudflare.com/profile/api-tokens"
    echo "with 'Zone:DNS:Edit' permission, then run:"
    echo ""
    echo "  sudo CLOUDFLARE_API_TOKEN=<token> bash $0"
    exit 1
fi

JARVIS_DIR="${JARVIS_DIR:-/opt/jarvis}"
DOMAIN_JARVIS="jarvis.mintwork.it"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-admin@mintwork.it}"

# =============================================================================
# Step 1: Install Nginx
# =============================================================================
echo -e "${YELLOW}[1/5] Installing Nginx...${NC}"
if ! command -v nginx &> /dev/null; then
    apt-get update
    apt-get install -y nginx
    systemctl enable nginx
    echo "Nginx installed"
else
    echo "Nginx already installed"
fi

# =============================================================================
# Step 2: Install Certbot with Cloudflare plugin
# =============================================================================
echo -e "${YELLOW}[2/5] Installing Certbot + Cloudflare plugin...${NC}"
apt-get install -y certbot python3-certbot-nginx python3-certbot-dns-cloudflare

# Create Cloudflare credentials file
CLOUDFLARE_CREDS="/etc/letsencrypt/cloudflare.ini"
cat > "$CLOUDFLARE_CREDS" <<CREDS
dns_cloudflare_api_token = ${CLOUDFLARE_API_TOKEN}
CREDS
chmod 600 "$CLOUDFLARE_CREDS"
echo "Cloudflare credentials saved to ${CLOUDFLARE_CREDS}"

# =============================================================================
# Step 3: Copy Nginx configs
# =============================================================================
echo -e "${YELLOW}[3/5] Configuring Nginx vhosts...${NC}"

# Remove default site
rm -f /etc/nginx/sites-enabled/default

# Copy configs (solo jarvis; openclaw e' OPZIONALE — vedi openclaw.conf header)
cp "${JARVIS_DIR}/cloud/nginx/jarvis.conf" /etc/nginx/sites-available/

# Enable sites
ln -sf /etc/nginx/sites-available/jarvis.conf /etc/nginx/sites-enabled/

# OPZIONALE: abilita il proxy VPS verso la dashboard OpenClaw
# Se vuoi esporre la dashboard anche dal VPS, decommenta le righe seguenti
# e genera un certificato separato per openclaw.mintwork.it sul VPS.
# cp "${JARVIS_DIR}/cloud/nginx/openclaw.conf" /etc/nginx/sites-available/
# ln -sf /etc/nginx/sites-available/openclaw.conf /etc/nginx/sites-enabled/

echo "Vhost configured: ${DOMAIN_JARVIS}"

# =============================================================================
# Step 4: Generate SSL certificates (DNS challenge)
# =============================================================================
echo -e "${YELLOW}[4/5] Generating SSL certificates via Cloudflare DNS...${NC}"
echo "This may take 30-60 seconds per domain for DNS propagation..."

# Certificate for jarvis.mintwork.it only
# NOTA: openclaw.mintwork.it ha il proprio cert Let's Encrypt gestito
# dal nginx sul server OpenClaw (certbot + Cloudflare DNS plugin).
certbot certonly \
    --dns-cloudflare \
    --dns-cloudflare-credentials "$CLOUDFLARE_CREDS" \
    --dns-cloudflare-propagation-seconds 30 \
    -d "$DOMAIN_JARVIS" \
    --email "$CERTBOT_EMAIL" \
    --agree-tos \
    --non-interactive

CERT_DIR="/etc/letsencrypt/live/${DOMAIN_JARVIS}"

echo "SSL certificate generated for ${DOMAIN_JARVIS}"

# =============================================================================
# Step 5: Test and reload Nginx
# =============================================================================
echo -e "${YELLOW}[5/5] Testing and reloading Nginx...${NC}"

nginx -t
systemctl reload nginx

echo "Nginx reloaded"

# =============================================================================
# Summary
# =============================================================================
echo ""
echo -e "${GREEN}=============================================="
echo "       Nginx + SSL Setup Complete!"
echo "==============================================${NC}"
echo ""
echo "VPS site configured:"
echo "  - https://${DOMAIN_JARVIS}  -> Orchestrator (127.0.0.1:5000)"
echo ""
echo "SSL certificate:"
echo "  - ${CERT_DIR}/"
echo "  - Auto-renewal via certbot timer"
echo ""
echo "OpenClaw TLS (separato, sul server OpenClaw):"
echo "  - https://openclaw.mintwork.it:18789  (API/WebSocket)"
echo "  - https://openclaw.mintwork.it        (Dashboard)"
echo "  - Cert gestito da setup-openclaw.sh Step 10"
echo ""
echo "Verify:"
echo "  curl -k https://${DOMAIN_JARVIS}/health"
echo ""
echo "Cert renewal test:"
echo "  sudo certbot renew --dry-run"
echo ""
echo -e "${GREEN}Done!${NC}"
