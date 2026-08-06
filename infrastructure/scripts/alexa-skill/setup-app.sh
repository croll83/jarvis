#!/usr/bin/env bash
# =============================================================================
# Music Assistant Alexa Skill — app Docker (gira nel CT alexa-skill)
# =============================================================================
# Allineato all'upstream attuale (alams154/music-assistant-alexa-skill-prototype):
#   - secret APP_USERNAME / APP_PASSWORD (file app_username.txt / app_password.txt)
#     → proteggono la UI web e /setup con basic auth
#   - volume ./ask_data:/root/.ask → persiste le credenziali ASK CLI tra i restart
#   - LOCALE per la lingua skill (it-IT per Echo italiani)
#   - niente SKILL_ID: lo gestisce il flusso /setup
#
# Eseguire DENTRO al container alexa-skill (Debian 12 con internet):
#   pct exec <ctid> -- bash /opt/alexa-skill/setup-app.sh
# Parametri via env (vedi default sotto).
#
# Idempotente: se 'ma-alexa-skill' è già Up aggiorna la config (no rebuild).
# =============================================================================
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/alexa-skill}"
MA_HOSTNAME="${MA_HOSTNAME:?es. http://192.168.68.97:8097 (IP HAOS Wagmi : porta stream MA)}"
SKILL_HOSTNAME="${SKILL_HOSTNAME:-https://wagmialexa.mintwork.it}"
LOCALE="${LOCALE:-it-IT}"
AWS_REGION="${AWS_REGION:-eu-west-1}"
TZ_VAL="${TZ_VAL:-Europe/Rome}"
APP_USERNAME="${APP_USERNAME:-alexamass}"
APP_PASSWORD="${APP_PASSWORD:?password basic-auth della UI/skill (file app_password.txt)}"

echo "[app] MA_HOSTNAME=$MA_HOSTNAME  SKILL_HOSTNAME=$SKILL_HOSTNAME  LOCALE=$LOCALE"

if ! command -v docker >/dev/null; then
  echo "[app] installo docker…"
  curl -fsSL https://get.docker.com | sh
fi

mkdir -p "$APP_DIR/secrets" "$APP_DIR/ask_data"
cat > "$APP_DIR/docker-compose.yml" <<YML
services:
  music-assistant-skill:
    build: https://github.com/alams154/music-assistant-alexa-skill-prototype.git
    container_name: ma-alexa-skill
    restart: unless-stopped
    environment:
      - SKILL_HOSTNAME=${SKILL_HOSTNAME}
      - MA_HOSTNAME=${MA_HOSTNAME}
      - APP_USERNAME=/run/secrets/APP_USERNAME
      - APP_PASSWORD=/run/secrets/APP_PASSWORD
      - PORT=5000
      - DEBUG_PORT=5678
      - LOCALE=${LOCALE}
      - AWS_DEFAULT_REGION=${AWS_REGION}
      - TZ=${TZ_VAL}
    secrets:
      - APP_USERNAME
      - APP_PASSWORD
    ports:
      - "5000:5000"
      - "5678:5678"
    volumes:
      - ./ask_data:/root/.ask
secrets:
  APP_USERNAME:
    file: ./secrets/app_username.txt
  APP_PASSWORD:
    file: ./secrets/app_password.txt
YML
printf '%s' "$APP_USERNAME" > "$APP_DIR/secrets/app_username.txt"
printf '%s' "$APP_PASSWORD" > "$APP_DIR/secrets/app_password.txt"
chmod 600 "$APP_DIR/secrets/"*.txt

cd "$APP_DIR"
if docker ps --filter name=ma-alexa-skill --format '{{.Status}}' | grep -q Up; then
  echo "[app] aggiorno config e riavvio…"
  docker compose up -d --remove-orphans
else
  echo "[app] build & up…"
  docker compose up -d --build --remove-orphans
fi
docker ps --filter name=ma-alexa-skill --format '{{.Names}} | {{.Status}} | {{.Ports}}'
echo "[app] UI/setup: ${SKILL_HOSTNAME}/setup  (basic auth: ${APP_USERNAME} / <app_password>)"
