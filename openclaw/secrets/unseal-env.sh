#!/usr/bin/env bash
# Unseal env vars from TPM vault and write to EnvironmentFile (tmpfs)
# Called by systemd ExecStartPre before OpenClaw gateway starts
set -euo pipefail

export TPM2TOOLS_TCTI="device:/dev/tpmrm0"
SECRETS_FILE="/home/jarvis/.openclaw/secrets/secrets.enc.json"
ENV_FILE="/run/user/$(id -u)/openclaw-secrets.env"
TPM_HANDLE="0x81000001"
TPM_AUTH_FILE="/run/user/$(id -u)/tpm-auth"

# Flush stale sessions
tpm2_flushcontext -s 2>/dev/null || true
tpm2_flushcontext -t 2>/dev/null || true

# Read TPM password
if [ -f "$TPM_AUTH_FILE" ]; then
    TPM_PASS=$(cat "$TPM_AUTH_FILE")
elif [ -n "${TPM_AUTH_PASSWORD:-}" ]; then
    TPM_PASS="$TPM_AUTH_PASSWORD"
else
    echo "ERROR: No TPM password found. Set TPM_AUTH_PASSWORD or run: tpm-unlock" >&2
    exit 1
fi

# Unseal age key
AGE_KEY=$(tpm2_unseal -c "$TPM_HANDLE" -p "$TPM_PASS" 2>&1) || {
    echo "ERROR: TPM unseal failed: $AGE_KEY" >&2
    exit 1
}

# Decrypt secrets
DECRYPTED=$(echo "$AGE_KEY" | SOPS_AGE_KEY_FILE=/dev/stdin sops decrypt "$SECRETS_FILE" 2>&1) || {
    echo "ERROR: SOPS decrypt failed: $DECRYPTED" >&2
    exit 1
}

# Extract env vars needed for openclaw.json string field substitution
OPENCLAW_GATEWAY_TOKEN=$(echo "$DECRYPTED" | jq -r '.openclaw_gateway_token')
TELEGRAM_BOT_TOKEN=$(echo "$DECRYPTED" | jq -r '.telegram_bot_token')
ONTOLOGY_API_TOKEN=$(echo "$DECRYPTED" | jq -r '.ontology_api_token')

# Write EnvironmentFile with restrictive permissions on tmpfs
umask 077
printf 'OPENCLAW_GATEWAY_TOKEN=%s\nTELEGRAM_BOT_TOKEN=%s\nONTOLOGY_API_TOKEN=%s\n' \
    "$OPENCLAW_GATEWAY_TOKEN" "$TELEGRAM_BOT_TOKEN" "$ONTOLOGY_API_TOKEN" > "$ENV_FILE"

# Cleanup
unset AGE_KEY TPM_PASS DECRYPTED

echo "Env vars unsealed to $ENV_FILE"
