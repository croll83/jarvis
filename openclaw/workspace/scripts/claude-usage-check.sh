#!/bin/bash
# Claude.ai Usage Check
# Fetches usage stats via browser evaluate (Cloudflare blocks direct curl)
# Requires: openclaw browser running with profile=openclaw, logged into claude.ai
#
# Env vars (from TPM):
#   CLAUDE_SSID      - __ssid cookie
#   CLAUDE_ORG_ID    - organization uuid
#   CLAUDE_DEVICE_ID - anthropic-device-id cookie
#
# Usage: ./claude-usage-check.sh [--json]

set -euo pipefail

ORG_ID="${CLAUDE_ORG_ID:?CLAUDE_ORG_ID not set}"

# Use openclaw CLI to evaluate JS in browser context
# The browser must have an active claude.ai tab/session
USAGE_JSON=$(openclaw browser eval --profile openclaw \
  --url "https://claude.ai/settings/usage" \
  --js "async () => { const r = await fetch('https://claude.ai/api/organizations/${ORG_ID}/usage', {credentials: 'include'}); return r.json(); }" 2>/dev/null || true)

if [ -z "$USAGE_JSON" ] || echo "$USAGE_JSON" | grep -q '"type":"error"'; then
  echo "ERROR: Could not fetch Claude usage. Is browser logged in?" >&2
  exit 1
fi

if [ "${1:-}" = "--json" ]; then
  echo "$USAGE_JSON" | jq '.'
  exit 0
fi

# Human-readable output
echo "$USAGE_JSON" | jq -r '
  "📊 Claude.ai Usage Stats",
  "━━━━━━━━━━━━━━━━━━━━━",
  "",
  "Sessione corrente (5h):",
  "  \(.five_hour.utilization)% utilizzato — reset: \(.five_hour.resets_at // "N/A")",
  "",
  "Settimanale (tutti i modelli):",
  "  \(.seven_day.utilization)% utilizzato — reset: \(.seven_day.resets_at // "N/A")",
  "",
  "Settimanale Sonnet:",
  "  \(if .seven_day_sonnet then "\(.seven_day_sonnet.utilization)% utilizzato — reset: \(.seven_day_sonnet.resets_at)" else "N/A" end)",
  "",
  "Settimanale Opus:",
  "  \(if .seven_day_opus then "\(.seven_day_opus.utilization)% utilizzato" else "N/A" end)",
  "",
  "Extra usage: \(if .extra_usage.is_enabled then "attivo (\(.extra_usage.used_credits // 0) crediti usati)" else "disattivato" end)"
'
