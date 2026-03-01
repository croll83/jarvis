#!/usr/bin/env bash
# update-openclaw.sh — Aggiorna OpenClaw e ri-applica i patch:
#   1. Per-model thinkingDefault (revertita in PR #19195)
#   2. thinkingOverride dal plugin hook before_model_resolve
#
# Uso:
#   ./update-openclaw.sh              # aggiorna + patch
#   ./update-openclaw.sh --patch-only # solo patch (senza aggiornamento npm)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_ONLY=false
[[ "${1:-}" == "--patch-only" ]] && PATCH_ONLY=true

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

# --- Step 2: Trova TUTTI i file da patchare ---
echo ""
echo "=== Applicazione patch thinkingDefault + thinkingOverride ==="

# Patch 1-3: file con resolveThinkingDefault (model-selection-*.js, config-*.js, auth-*.js)
RESOLVE_FILES=$(grep -rl 'function resolveThinkingDefault' "$DIST_DIR" --include='*.js' 2>/dev/null || true)
# Patch 4: file con modelResolveOverride?.modelOverride (reply-*.js, pi-embedded-*.js)
HOOK_FILES=$(grep -rl 'modelResolveOverride?.modelOverride' "$DIST_DIR" --include='*.js' 2>/dev/null || true)
# Patch 5: file con "let resolvedThinkLevel = thinkOnce" (reply-*.js, pi-embedded-*.js)
THINK_FILES=$(grep -rl 'let resolvedThinkLevel = thinkOnce' "$DIST_DIR" --include='*.js' 2>/dev/null || true)

# Unisci tutti i file unici
ALL_FILES=$(echo -e "${RESOLVE_FILES}\n${HOOK_FILES}\n${THINK_FILES}" | sort -u | grep -v '^$')

if [[ -z "$ALL_FILES" ]]; then
  echo "ERROR: Nessun file patchabile trovato nel bundle."
  exit 1
fi

echo "  File trovati: $(echo "$ALL_FILES" | wc -l)"

PATCHED=0
SKIPPED=0
ERRORS=0

for FILE in $ALL_FILES; do
  BASENAME=$(basename "$FILE")
  echo ""
  echo "  [$BASENAME] Patching..."
  if perl "$SCRIPT_DIR/patch-thinking.pl" "$FILE"; then
    PATCHED=$((PATCHED + 1))
  else
    ERRORS=$((ERRORS + 1))
  fi
done

echo ""
echo "=== Risultato: $PATCHED file processati, $ERRORS errori ==="

if [[ $ERRORS -gt 0 ]]; then
  echo "ATTENZIONE: alcuni file non sono stati patchati correttamente."
  exit 1
fi

# --- Step 3: Verifica ---
echo ""
echo "=== Verifica patch ==="
for FILE in $ALL_FILES; do
  BASENAME=$(basename "$FILE")
  C1=$(grep -c 'per-model thinkingDefault patch' "$FILE" 2>/dev/null || echo 0)
  C2=$(grep -c 'hook thinkingOverride patch' "$FILE" 2>/dev/null || echo 0)
  echo "  [$BASENAME] per-model: $C1, hook-override: $C2"
done

echo ""
echo "Patch completato. Riavvia il gateway con: systemctl --user restart openclaw-gateway"
