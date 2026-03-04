#!/usr/bin/env bash
# Helper to add/update/list secrets in the TPM-backed SOPS vault
# Password-auth unseal (no PCR policy)
set -euo pipefail

export TPM2TOOLS_TCTI='device:/dev/tpmrm0'
SECRETS_FILE="/home/jarvis/.openclaw/secrets/secrets.enc.json"
AGE_PUB="age1ve068q0mkac6h9f3d0s8m0zstzjmhj4576zz676yzs9e8txmsv7s8qsfx6"
ACTION="${1:-help}"
TPM_HANDLE="0x81000001"
TPM_AUTH_FILE="/run/user/$(id -u)/tpm-auth"

unseal_age_key() {
    tpm2_flushcontext -s 2>/dev/null || true

    local pass=""
    if [ -f "$TPM_AUTH_FILE" ]; then
        pass=$(cat "$TPM_AUTH_FILE")
    elif [ -n "${TPM_AUTH_PASSWORD:-}" ]; then
        pass="$TPM_AUTH_PASSWORD"
    else
        echo "ERROR: No TPM password. Set TPM_AUTH_PASSWORD or run: tpm-unlock" >&2
        exit 1
    fi

    local key
    key=$(tpm2_unseal -c "$TPM_HANDLE" -p "$pass" 2>&1) || {
        echo "ERROR: TPM unseal failed: $key" >&2
        exit 1
    }
    echo "$key"
}

case "$ACTION" in
    list)
        AGE_KEY=$(unseal_age_key)
        echo "$AGE_KEY" | SOPS_AGE_KEY_FILE=/dev/stdin sops decrypt "$SECRETS_FILE" | jq -r 'keys[]'
        ;;
    get)
        [ -z "${2:-}" ] && echo "Usage: $0 get <key>" && exit 1
        AGE_KEY=$(unseal_age_key)
        echo "$AGE_KEY" | SOPS_AGE_KEY_FILE=/dev/stdin sops decrypt "$SECRETS_FILE" | jq -r --arg k "$2" '.[$k] // "KEY_NOT_FOUND"'
        ;;
    set)
        [ -z "${2:-}" ] || [ -z "${3:-}" ] && echo "Usage: $0 set <key> <value>" && exit 1
        AGE_KEY=$(unseal_age_key)
        PLAIN=$(echo "$AGE_KEY" | SOPS_AGE_KEY_FILE=/dev/stdin sops decrypt "$SECRETS_FILE")
        UPDATED=$(echo "$PLAIN" | jq --arg k "$2" --arg v "$3" '. + {($k): $v}')
        TMP="/tmp/sops-edit-$$.json"
        echo "$UPDATED" > "$TMP"
        echo "$AGE_KEY" | SOPS_AGE_KEY_FILE=/dev/stdin sops encrypt --age "$AGE_PUB" "$TMP" > "$SECRETS_FILE"
        shred -u "$TMP"
        echo "Secret '$2' saved."
        ;;
    delete)
        [ -z "${2:-}" ] && echo "Usage: $0 delete <key>" && exit 1
        AGE_KEY=$(unseal_age_key)
        PLAIN=$(echo "$AGE_KEY" | SOPS_AGE_KEY_FILE=/dev/stdin sops decrypt "$SECRETS_FILE")
        UPDATED=$(echo "$PLAIN" | jq --arg k "$2" 'del(.[$k])')
        TMP="/tmp/sops-edit-$$.json"
        echo "$UPDATED" > "$TMP"
        echo "$AGE_KEY" | SOPS_AGE_KEY_FILE=/dev/stdin sops encrypt --age "$AGE_PUB" "$TMP" > "$SECRETS_FILE"
        shred -u "$TMP"
        echo "Secret '$2' deleted."
        ;;
    *)
        echo "TPM-backed SOPS secret manager (password-auth, no PCR)"
        echo ""
        echo "Usage: $0 {list|get|set|delete} [key] [value]"
        echo ""
        echo "Before using, set TPM password:"
        echo "  export TPM_AUTH_PASSWORD='your-password'"
        echo "  OR"
        echo "  tpm-unlock   (writes to /run/user/\$(id -u)/tpm-auth)"
        echo ""
        echo "Examples:"
        echo "  $0 list"
        echo "  $0 set wallet_private_key 0x4a7f..."
        echo "  $0 get wallet_private_key"
        echo "  $0 delete old_key"
        ;;
esac
