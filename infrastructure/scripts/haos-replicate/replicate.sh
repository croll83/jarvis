#!/usr/bin/env bash
# =============================================================================
# HAOS replicate — wrapper
# =============================================================================
# 1. carica .env
# 2. (opzionale) copia l'add-on locale ha_memory_service sull'HAOS via SSH
# 3. esegue replicate.py
#
# Uso:
#   cp .env.example .env && nano .env
#   ./replicate.sh            # esegue
#   ./replicate.sh --dry-run  # mostra cosa farebbe, senza scrivere nulla
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

if [[ -f .env ]]; then
  set -a; # shellcheck disable=SC1091
  source .env; set +a
else
  echo "!! manca .env — copia .env.example e compila" >&2; exit 1
fi

: "${HASS_URL:?HASS_URL non impostato}"
: "${HASS_TOKEN:?HASS_TOKEN non impostato}"

# repo root = .../jarvis  (tre livelli sopra: scripts/haos-replicate -> infrastructure -> repo)
REPO_ROOT="$(cd ../../.. && pwd)"
ADDON_SRC="$REPO_ROOT/ha_memory_service"

DRY=""
for a in "$@"; do [[ "$a" == "--dry-run" ]] && DRY="--dry-run"; done

# --- copia add-on locale (se SSH_TARGET impostato) ---
# SSH_TARGET es: root@192.168.1.X -p 22  oppure passa per l'add-on SSH (porta 22)
if [[ -n "${SSH_TARGET:-}" ]]; then
  if [[ -d "$ADDON_SRC" ]]; then
    echo "[LOCAL] copio $ADDON_SRC -> $SSH_TARGET:/addons/jarvis_ha_memory"
    if [[ -z "$DRY" ]]; then
      # shellcheck disable=SC2086
      ssh ${SSH_OPTS:-} $SSH_TARGET "mkdir -p /addons/jarvis_ha_memory"
      # shellcheck disable=SC2086
      rsync -az --delete ${SSH_OPTS:+-e "ssh $SSH_OPTS"} \
        --exclude '.git' "$ADDON_SRC"/ "$SSH_TARGET":/addons/jarvis_ha_memory/ \
        || scp -r "$ADDON_SRC"/* "$SSH_TARGET":/addons/jarvis_ha_memory/
    else
      echo "[dry] (copia saltata)"
    fi
  else
    echo "!! $ADDON_SRC non trovato — salto la copia dell'add-on locale" >&2
  fi
else
  echo "[i] SSH_TARGET non impostato: l'add-on locale JARVIS HA Memory NON verrà copiato."
  echo "    Copialo a mano in /addons/jarvis_ha_memory oppure imposta SSH_TARGET in .env."
fi

# --- dipendenze python ---
python3 -c 'import yaml, websockets' 2>/dev/null || {
  echo "[i] installo dipendenze python (pyyaml, websockets)…"
  pip3 install --quiet pyyaml websockets
}

exec python3 replicate.py "$@"
