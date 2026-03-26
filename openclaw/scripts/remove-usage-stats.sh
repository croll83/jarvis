#!/bin/bash
# Rimuove il tree "usageStats" da un file JSON
# Usage: bash remove-usage-stats.sh [file.json]
# Default: ~/.openclaw/agents/main/agent/auth-profiles.json

set -e

FILE="${1:-$HOME/.openclaw/agents/main/agent/auth-profiles.json}"

if [ ! -f "$FILE" ]; then
    echo "Errore: $FILE non trovato"
    exit 1
fi

jq 'del(.usageStats)' "$FILE" > "${FILE}.tmp" && mv "${FILE}.tmp" "$FILE"

echo "Rimosso .usageStats da $FILE"

FILE="${1:-$HOME/.openclaw/agents/trader/agent/auth-profiles.json}"

if [ ! -f "$FILE" ]; then
    echo "Errore: $FILE non trovato"
    exit 1
fi

jq 'del(.usageStats)' "$FILE" > "${FILE}.tmp" && mv "${FILE}.tmp" "$FILE"

echo "Rimosso .usageStats da $FILE"

