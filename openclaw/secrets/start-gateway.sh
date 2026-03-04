#!/bin/bash
set -euo pipefail

# --- Config ---
export TPM2TOOLS_TCTI="device:/dev/tpmrm0"
SECRETS_FILE="/home/jarvis/.openclaw/secrets/secrets.enc.json"
TPM_HANDLE="0x81000001"
TPM_AUTH_FILE="/run/user/$(id -u)/tpm-auth"

# --- Pre-flight: flush stale TPM sessions ---
tpm2_flushcontext -s 2>/dev/null || true
tpm2_flushcontext -t 2>/dev/null || true

# --- Read TPM password ---
if [ -f "$TPM_AUTH_FILE" ]; then
    TPM_PASS=$(cat "$TPM_AUTH_FILE")
elif [ -n "${TPM_AUTH_PASSWORD:-}" ]; then
    TPM_PASS="$TPM_AUTH_PASSWORD"
else
    echo "ERROR: No TPM password found. Set TPM_AUTH_PASSWORD or run: tpm-unlock" >&2
    exit 1
fi

# --- Unseal age key from TPM (password auth, no PCR policy) ---
AGE_KEY=$(tpm2_unseal -c "$TPM_HANDLE" -p "$TPM_PASS" 2>&1) || {
    echo "ERROR: TPM unseal failed: $AGE_KEY" >&2
    exit 1
}

if ! echo "$AGE_KEY" | grep -q "^AGE-SECRET-KEY-"; then
    echo "ERROR: TPM returned invalid data (not an age key)" >&2
    exit 1
fi

# --- Decrypt vault ---
DECRYPTED=$(echo "$AGE_KEY" | SOPS_AGE_KEY_FILE=/dev/stdin sops decrypt "$SECRETS_FILE" 2>&1) || {
    echo "ERROR: SOPS decrypt failed: $DECRYPTED" >&2
    exit 1
}

# --- Collect SecretRef exec:tpm IDs (handled by OpenClaw, skip export) ---
SECRETREF_IDS=""
for ap in ~/.openclaw/agents/*/agent/auth-profiles.json; do
    [ -f "$ap" ] || continue
    SECRETREF_IDS="${SECRETREF_IDS}
$(jq -r '[ (.profiles // {} | to_entries[].value.keyRef  | select(type == "object" and .source == "exec" and .provider == "tpm") | .id), (.profiles // {} | to_entries[].value.tokenRef | select(type == "object" and .source == "exec" and .provider == "tpm") | .id) ] | unique | .[]' "$ap" 2>/dev/null || true)"
done
SECRETREF_IDS=$(echo "$SECRETREF_IDS" | sort -u | sed '/^$/d')

# --- Export all vault keys NOT managed by SecretRef ---
for key in $(echo "$DECRYPTED" | jq -r 'keys[]'); do
    if ! echo "$SECRETREF_IDS" | grep -qxF "$key"; then
        val=$(echo "$DECRYPTED" | jq -r --arg k "$key" '.[$k]')
        upper=$(echo "$key" | tr '[:lower:]' '[:upper:]')
        export "$upper=$val"
    fi
done

# --- GROQ_API_KEY for media understanding (audio transcription) ---
export GROQ_API_KEY=$(echo "$DECRYPTED" | jq -r .whisper_api_key)

# --- Clear sensitive vars ---
unset AGE_KEY TPM_PASS

# --- Start gateway ---
exec /usr/local/bin/node /home/jarvis/.nvm/versions/node/v22.22.0/lib/node_modules/openclaw/dist/index.js gateway --port 18789
