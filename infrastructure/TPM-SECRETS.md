# TPM-Backed Secret Management for AI Agent

Hardware-backed secret storage using Intel fTPM + password auth → SOPS + age → AI Agent exec provider.

**No secret ever touches the filesystem in plaintext.** The age decryption key is sealed inside the TPM and only released when the correct password is provided.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Proxmox Host (pve-wagmi)                                   │
│  Intel i9-14900HX — fTPM 2.0 v1.38 (INTC)                  │
│                                                             │
│  /dev/tpmrm0 ─── bind mount ──► LXC 101 /dev/tpmrm0        │
│  (major 252, minor 65536)       (cgroup2 allow c 252:65536) │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  LXC 101 (jarvis-openclaw)                                  │
│                                                             │
│  ┌──────────────┐    ┌──────────────┐    ┌───────────────┐  │
│  │  TPM unseal   │───▶│  age decrypt  │───▶│  SOPS decrypt │  │
│  │  (password)   │    │  (in-memory)  │    │  secrets.enc  │  │
│  └──────────────┘    └──────────────┘    └───────┬───────┘  │
│                                                  │          │
│                                          ┌───────▼───────┐  │
│                                          │  AI Agent      │  │
│                                          │  exec provider │  │
│                                          │  (JSON proto)  │  │
│                                          └───────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

## Security Model

| Layer | Protection | What it covers |
|-------|-----------|----------------|
| **TPM seal (password)** | Age private key locked by password | Only released with correct password |
| **SOPS + age** | AES-256-GCM encryption of secrets file | All API keys, tokens, wallet keys |
| **File permissions** | `chmod 600` on all sensitive files | Defense in depth |
| **tmpfs password** | Password in `/run/user/1000/tpm-auth` | Cleared on reboot, never on disk |
| **LXC isolation** | Unprivileged container, no root on host | Blast radius containment |
| **Tailscale** | WireGuard mesh, no public exposure | Network-level isolation |

### Why password-auth instead of PCR policy

The original setup used PCR 0,1,2,3,7 policy. This was abandoned because:
- **PCR 0** contains hidden Intel Boot Guard/CSME measurements NOT in the UEFI event log
- These measurements can change after a power cycle, permanently locking out the sealed key
- PCR policy provides marginal security benefit for a homelab (protects only against live-USB boot with root access)
- Password-auth is equally secure against disk theft and remote attacks

### Threat analysis

| Scenario | Protected? |
|----------|-----------|
| Disk stolen (without miniPC) | **Yes** — age key is in TPM chip on motherboard |
| Physical access, no root | **Yes** — cannot access `/dev/tpmrm0` or run `tpm2_unseal` |
| Physical access + root | **Requires TPM password** — attacker must know the password |
| Remote attack with root | **Requires TPM password** — password only in tmpfs after manual unlock |
| Boot from live USB + root | **Requires TPM password** |

## TPM Persistent Handle

- **Handle:** `0x81000001`
- **Object type:** Sealed data (keyedhash)
- **Auth:** Password (set via `tpm-unlock` after each reboot)
- **Sealed data:** age secret key (74 bytes, `AGE-SECRET-KEY-1...`)
- **TCTI:** `device:/dev/tpmrm0`

## Age Public Key

```
age1ve068q0mkac6h9f3d0s8m0zstzjmhj4576zz676yzs9e8txmsv7s8qsfx6
```

This is the **public** key — safe to store in git, documentation, and config files.
The corresponding private key exists **only inside the TPM** (backup offline\!).

## File Layout

```
~/.openclaw/secrets/
├── secrets.enc.json          # SOPS-encrypted secrets (AES-256-GCM)
├── start-gateway.sh          # Main gateway startup (unseal → decrypt → exec node)
├── unseal-env.sh             # Systemd ExecStartPre (writes env file to tmpfs)
├── tpm-secret-resolver.sh    # AI Agent exec provider (JSON protocol)
├── tpm-secrets-edit.sh       # CLI helper (list/get/set/delete secrets)
├── tpm-unlock                # Post-reboot password setter (writes to tmpfs)
├── tpm-rotate-password       # Password rotation tool
└── tpm/
    ├── seal.pub              # TPM sealed object public part
    └── seal.priv             # TPM sealed object private part (encrypted by TPM)
```

## Secrets Stored (21 keys)

| Key in SOPS | Purpose |
|-------------|---------|
| `anthropic_token` | Anthropic API (Claude models) |
| `google_api_key` | Google/Gemini API |
| `openrouter_api_key` | OpenRouter API |
| `xai_api_key` | xAI/Grok API |
| `goplaces_api_key` | GoPlaces skill |
| `nano_banana_api_key` | Nano Banana Pro skill |
| `whisper_api_key` | Whisper/Groq transcription (also exported as GROQ_API_KEY) |
| `wallet_private_key` | Crypto wallet private key (also used as POLYMARKET_PRIVATE_KEY) |
| `JARVIS_WALLET` | Wallet address |
| `HYPERLIQUID_ADDRESS` | Hyperliquid trading address |
| `openclaw_gateway_token` | AI Agent gateway auth token |
| `telegram_bot_token` | Telegram bot token |
| `ontology_api_token` | Ontology API token |
| `TWITTER_AUTH_TOKEN` | Twitter/X auth cookie |
| `TWITTER_CT0` | Twitter/X ct0 cookie |
| `DELIVEROO_CONSUMER_AUTH_TOKEN` | Deliveroo auth |
| `DELIVEROO_ROO_GUID` | Deliveroo cookie |
| `DELIVEROO_ROO_SESSION_GUID` | Deliveroo session cookie |
| `DELIVEROO_ROO_STICKY_GUID` | Deliveroo sticky cookie |
| `DELIVEROO_USER_DATA` | Deliveroo user data |
| `GOG_KEYRING_PASSWORD` | GOG keyring password |

## Operations

### After reboot: unlock TPM

```bash
tpm-unlock
# or with password directly:
tpm-unlock "your-password"
# This writes the password to /run/user/1000/tpm-auth (tmpfs, cleared on reboot)
```

### Start gateway

```bash
systemctl --user start openclaw-gateway
# or manually:
~/.openclaw/secrets/start-gateway.sh
```

### Manage secrets

```bash
tpm-secrets-edit list                    # List all secret keys
tpm-secrets-edit get wallet_private_key  # Read a secret
tpm-secrets-edit set new_key "value"     # Add or update
tpm-secrets-edit delete old_key          # Remove
```

### Rotate TPM password

```bash
tpm-rotate-password "old-password" "new-password"
# or interactively:
tpm-rotate-password
```

### Backup age key (IMPORTANT)

```bash
tpm2_unseal -c 0x81000001 -p "your-password"
# Save the AGE-SECRET-KEY-... offline (paper, encrypted USB)
# If the TPM dies or is cleared, this is the ONLY way to recover secrets
```

## AI Agent Configuration

```json
{
  "secrets": {
    "providers": {
      "tpm": {
        "source": "exec",
        "command": "/home/jarvis/.openclaw/secrets/tpm-secret-resolver.sh",
        "jsonOnly": true
      }
    },
    "defaults": {
      "exec": "tpm"
    }
  }
}
```

## Proxmox Host Configuration

### Udev rule (`/etc/udev/rules.d/99-tpm-lxc.rules`)
```
SUBSYSTEM=="tpmrm", MODE="0666"
```

### LXC 101 config (`/etc/pve/lxc/101.conf`)
```
lxc.cgroup2.devices.allow: c 252:65536 rwm
lxc.mount.entry: /dev/tpmrm0 dev/tpmrm0 none bind,create=file
```

## Dependencies

| Package | Version | Location |
|---------|---------|----------|
| tpm2-tools | 5.7 (host) / 5.6 (LXC) | apt |
| age | 1.2.1 | /usr/local/bin/age |
| sops | 3.9.4 | /usr/local/bin/sops |
| jq | system | apt |

## Emergency Recovery

If the TPM is cleared or the motherboard dies:

1. Restore the age secret key from offline backup
2. Re-seal it in the TPM:
   ```bash
   export TPM2TOOLS_TCTI="device:/dev/tpmrm0"
   tpm2_createprimary -C o -c /tmp/primary.ctx
   echo -n "AGE-SECRET-KEY-..." > /tmp/age.key
   tpm2_create -C /tmp/primary.ctx -i /tmp/age.key \
     -u ~/.openclaw/secrets/tpm/seal.pub \
     -r ~/.openclaw/secrets/tpm/seal.priv \
     -p "your-new-password"
   tpm2_load -C /tmp/primary.ctx \
     -u ~/.openclaw/secrets/tpm/seal.pub \
     -r ~/.openclaw/secrets/tpm/seal.priv \
     -c /tmp/seal.ctx
   tpm2_evictcontrol -C o -c /tmp/seal.ctx 0x81000001
   shred -u /tmp/age.key /tmp/primary.ctx /tmp/seal.ctx
   ```
3. If no backup exists: regenerate all 21 API keys from provider dashboards
