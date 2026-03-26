#!/usr/bin/env bash
# OpenClaw exec secret provider — TPM2 + SOPS + age
# Password-auth unseal (no PCR policy)
set -euo pipefail

export TPM2TOOLS_TCTI='device:/dev/tpmrm0'
SECRETS_FILE="/home/jarvis/.openclaw/secrets/secrets.enc.json"
TPM_HANDLE="0x81000001"
TPM_AUTH_FILE="/run/user/$(id -u)/tpm-auth"

# Flush stale sessions
tpm2_flushcontext -s 2>/dev/null || true

# Read TPM password
if [ -f "$TPM_AUTH_FILE" ]; then
    TPM_PASS=$(cat "$TPM_AUTH_FILE")
elif [ -n "${TPM_AUTH_PASSWORD:-}" ]; then
    TPM_PASS="$TPM_AUTH_PASSWORD"
else
    jq -n '{protocolVersion: 1, values: {}, error: "TPM password not set"}'
    exit 0
fi

# Read request from stdin (OpenClaw exec protocol)
REQUEST=$(cat)
IDS=$(echo "$REQUEST" | jq -r '.ids[]' 2>/dev/null)

# Unseal age key
AGE_KEY=$(tpm2_unseal -c "$TPM_HANDLE" -p "$TPM_PASS" 2>/dev/null) || {
    jq -n '{protocolVersion: 1, values: {}, error: "TPM unseal failed"}'
    exit 0
}

# Decrypt secrets
DECRYPTED=$(echo "$AGE_KEY" | SOPS_AGE_KEY_FILE=/dev/stdin sops decrypt "$SECRETS_FILE" 2>/dev/null) || {
    jq -n '{protocolVersion: 1, values: {}, error: "SOPS decrypt failed"}'
    exit 0
}

# Build response
VALUES="{}"
for ID in $IDS; do
    VAL=$(echo "$DECRYPTED" | jq -r --arg k "$ID" '.[$k] // empty')
    if [ -n "$VAL" ]; then
        VALUES=$(echo "$VALUES" | jq --arg k "$ID" --arg v "$VAL" '. + {($k): $v}')
    else
        VALUES=$(echo "$VALUES" | jq --arg k "$ID" '. + {($k): {"error": "not_found"}}')
    fi
done

jq -n --argjson v "$VALUES" '{protocolVersion: 1, values: $v}'
