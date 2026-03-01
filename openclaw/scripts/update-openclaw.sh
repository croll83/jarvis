#!/usr/bin/env bash
# update-openclaw.sh — Aggiorna OpenClaw e ri-applica il patch per-model thinkingDefault.
#
# Il patch re-introduce la feature revertita in PR #19195 (commit 3211280be):
#   - Aggiunge thinkingDefault allo schema Zod dei model entry (.strict())
#   - Modifica resolveThinkingDefault() per leggere il per-model override
#
# Uso:
#   ./update-openclaw.sh              # aggiorna + patch
#   ./update-openclaw.sh --patch-only # solo patch (senza aggiornamento npm)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_ONLY=false
[[ "${1:-}" == "--patch-only" ]] && PATCH_ONLY=true

MARKER='per-model thinkingDefault patch'

OPENCLAW_DIR="$(npm root -g)/openclaw"
DIST_DIR="$OPENCLAW_DIR/dist"

if [[ ! -d "$DIST_DIR" ]]; then
  echo "ERROR: OpenClaw non trovato in $OPENCLAW_DIR"
  exit 1
fi

# --- Step 1: Aggiornamento npm (se richiesto) ---
if [[ "$PATCH_ONLY" == false ]]; then
  echo "=== Aggiornamento OpenClaw ==="
  CURRENT=$(node -e "console.log(require('$OPENCLAW_DIR/package.json').version)")
  echo "Versione attuale: $CURRENT"
  npm update -g openclaw
  NEW=$(node -e "console.log(require('$OPENCLAW_DIR/package.json').version)")
  echo "Versione dopo update: $NEW"
  if [[ "$CURRENT" == "$NEW" ]]; then
    echo "Nessun aggiornamento disponibile."
  fi
fi

# --- Step 2: Applica patch ---
echo ""
echo "=== Applicazione patch per-model thinkingDefault ==="

TARGET_FILES=$(grep -rl 'function resolveThinkingDefault' "$DIST_DIR" --include='*.js' 2>/dev/null || true)

if [[ -z "$TARGET_FILES" ]]; then
  echo "ERROR: resolveThinkingDefault non trovato nel bundle."
  echo "Il codice di OpenClaw potrebbe essere cambiato significativamente."
  exit 1
fi

PATCHED=0
SKIPPED=0
ERRORS=0

for FILE in $TARGET_FILES; do
  BASENAME=$(basename "$FILE")

  if grep -q "$MARKER" "$FILE" 2>/dev/null; then
    echo "  [$BASENAME] Già patchato, skip."
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  echo "  [$BASENAME] Patching..."
  if perl "$SCRIPT_DIR/patch-thinking.pl" "$FILE"; then
    PATCHED=$((PATCHED + 1))
  else
    ERRORS=$((ERRORS + 1))
  fi
done

echo ""
echo "=== Risultato: $PATCHED file patchati, $SKIPPED già patchati, $ERRORS errori ==="

if [[ $ERRORS -gt 0 ]]; then
  echo "ATTENZIONE: alcuni file non sono stati patchati correttamente."
  exit 1
fi

if [[ $PATCHED -eq 0 && $SKIPPED -eq 0 ]]; then
  echo "ATTENZIONE: nessun file patchato!"
  exit 1
fi

# --- Step 3: Verifica ---
echo ""
echo "=== Verifica patch ==="
for FILE in $TARGET_FILES; do
  BASENAME=$(basename "$FILE")
  COUNT=$(grep -c "$MARKER" "$FILE" 2>/dev/null || echo 0)
  echo "  [$BASENAME] marker trovati: $COUNT"
done

echo ""
echo "Patch completato. Riavvia il gateway con: systemctl --user restart openclaw-gateway"
