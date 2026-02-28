# TPM-Backed Secret Management for OpenClaw

Hardware-backed secret storage using Intel fTPM → SOPS + age → OpenClaw exec provider.

**No secret ever touches the filesystem in plaintext.** The age decryption key is sealed inside the TPM and only released when the system's boot chain (PCR values) matches the sealed state.

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
│  │  (PCR policy) │    │  (in-memory)  │    │  secrets.enc  │  │
│  └──────────────┘    └──────────────┘    └───────┬───────┘  │
│                                                  │          │
│                                          ┌───────▼───────┐  │
│                                          │  OpenClaw      │  │
│                                          │  exec provider │  │
│                                          │  (JSON proto)  │  │
│                                          └───────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

## Security Model

| Layer | Protection | What it covers |
|-------|-----------|----------------|
| **TPM seal** | Age private key bound to PCR 0,1,2,3,7 | BIOS, bootloader, kernel integrity |
| **SOPS + age** | AES-256-GCM encryption of secrets file | All API keys, tokens, wallet keys |
| **File permissions** | `chmod 600` on all sensitive files | Defense in depth |
| **LXC isolation** | Unprivileged container, no root on host | Blast radius containment |
| **Tailscale** | WireGuard mesh, no public exposure | Network-level isolation |

**PCR registers used:**
- PCR 0: BIOS/UEFI firmware
- PCR 1: BIOS configuration
- PCR 2: Option ROMs
- PCR 3: Option ROM configuration
- PCR 7: Secure Boot state

**When re-sealing is needed:**
- BIOS/UEFI firmware update
- Kernel update (if measured in PCR)
- Secure Boot policy change
- **NOT needed for:** OpenClaw updates, Node.js updates, application changes

## File Layout

```
~/.openclaw/
├── secrets/
│   ├── secrets.enc.json          # SOPS-encrypted secrets (AES-256-GCM)
│   ├── tpm-secret-resolver.sh    # OpenClaw exec provider wrapper
│   ├── tpm-secrets-edit.sh       # CLI helper (list/get/set/delete secrets)
│   └── tpm/
│       ├── seal.pub              # TPM sealed object public part
│       ├── seal.priv             # TPM sealed object private part (encrypted by TPM)
│       └── pcr.policy            # PCR policy digest (sha256:0,1,2,3,7)
└── openclaw.json                 # Contains secretRef entries (no plaintext)
```

## Secrets Stored

| Key in SOPS | Purpose |
|-------------|---------|
| `anthropic_token` | Anthropic API (Claude models) |
| `google_api_key` | Google/Gemini API |
| `openrouter_api_key` | OpenRouter API |
| `xai_api_key` | xAI/Grok API |
| `goplaces_api_key` | GoPlaces skill |
| `nano_banana_api_key` | Nano Banana Pro skill |
| `whisper_api_key` | OpenAI Whisper API skill |
| `wallet_private_key` | Crypto wallet private key |

## TPM Persistent Handle

- **Handle:** `0x81000001`
- **Object type:** Sealed data (keyedhash)
- **Auth policy:** PCR sha256:0,1,2,3,7
- **Sealed data:** age secret key (74 bytes, AGE-SECRET-KEY-1...)
- **TCTI:** `device:/dev/tpmrm0`

## OpenClaw Configuration

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

Secret references in auth-profiles.json:
```json
{
  "profiles": {
    "anthropic:default": {
      "type": "token",
      "provider": "anthropic",
      "tokenRef": { "source": "exec", "provider": "tpm", "id": "anthropic_token" }
    },
    "google:default": {
      "type": "api_key",
      "provider": "google",
      "keyRef": { "source": "exec", "provider": "tpm", "id": "google_api_key" }
    }
  }
}
```

## Proxmox Host Configuration

### Udev rule (`/etc/udev/rules.d/99-tpm-lxc.rules`)
```
SUBSYSTEM=="tpmrm", MODE="0666"
```

### LXC 101 config additions (`/etc/pve/lxc/101.conf`)
```
lxc.cgroup2.devices.allow: c 252:65536 rwm
lxc.mount.entry: /dev/tpmrm0 dev/tpmrm0 none bind,create=file
```

## Operations

### Add/manage secrets (CLI helper)

```bash
# List all secrets
~/.openclaw/secrets/tpm-secrets-edit.sh list

# Add or update a secret
~/.openclaw/secrets/tpm-secrets-edit.sh set wallet_private_key "0x4a7f..."
~/.openclaw/secrets/tpm-secrets-edit.sh set hl_api_key "abc123"

# Read a secret
~/.openclaw/secrets/tpm-secrets-edit.sh get wallet_private_key

# Delete a secret
~/.openclaw/secrets/tpm-secrets-edit.sh delete old_key
```

### Add a new secret (manual)

```bash
# 1. Unseal age key
export TPM2TOOLS_TCTI='device:/dev/tpmrm0'
tpm2_startauthsession --policy-session -S /tmp/s.ctx
tpm2_policypcr -S /tmp/s.ctx -l sha256:0,1,2,3,7
AGE_KEY=$(tpm2_unseal -c 0x81000001 -p session:/tmp/s.ctx)
tpm2_flushcontext /tmp/s.ctx && rm /tmp/s.ctx

# 2. Decrypt, add key, re-encrypt
PLAIN=$(echo "$AGE_KEY" | SOPS_AGE_KEY_FILE=/dev/stdin sops decrypt secrets.enc.json)
echo "$PLAIN" | jq '. + {"new_key": "value"}' > /tmp/updated.json
echo "$AGE_KEY" | SOPS_AGE_KEY_FILE=/dev/stdin \
  sops encrypt --age age156g4lu8fq6ap5hamr5yuc38d3kh9dqyzcmgvyszkpz6a3lmkepjqapg005 \
  /tmp/updated.json > secrets.enc.json
shred -u /tmp/updated.json

# 3. Add secretRef in OpenClaw config
openclaw config set <field> '{"source":"exec","provider":"tpm","id":"new_key"}'
openclaw restart
```

### Re-seal after firmware/kernel update

```bash
export TPM2TOOLS_TCTI='device:/dev/tpmrm0'

# 1. Before the update: extract the age key (while PCRs still match)
tpm2_startauthsession --policy-session -S /tmp/s.ctx
tpm2_policypcr -S /tmp/s.ctx -l sha256:0,1,2,3,7
AGE_KEY=$(tpm2_unseal -c 0x81000001 -p session:/tmp/s.ctx)
tpm2_flushcontext /tmp/s.ctx && rm /tmp/s.ctx

# Save temporarily (delete after re-seal!)
echo "$AGE_KEY" > /tmp/age-key-backup.txt

# 2. Apply the firmware/kernel update, reboot

# 3. After reboot: re-seal with new PCR values
tpm2_createprimary -C o -g sha256 -G rsa -c /tmp/primary.ctx
tpm2_createpolicy --policy-pcr -l sha256:0,1,2,3,7 -L /tmp/new-pcr.policy
echo "$AGE_KEY" > /tmp/age-key-backup.txt  # if not saved before
tpm2_create -C /tmp/primary.ctx -g sha256 \
  -u ~/.openclaw/tpm/seal.pub -r ~/.openclaw/tpm/seal.priv \
  -L /tmp/new-pcr.policy -i /tmp/age-key-backup.txt
tpm2_load -C /tmp/primary.ctx \
  -u ~/.openclaw/tpm/seal.pub -r ~/.openclaw/tpm/seal.priv -c /tmp/seal.ctx
tpm2_evictcontrol -C o -c 0x81000001  # remove old
tpm2_evictcontrol -C o -c /tmp/seal.ctx 0x81000001  # persist new
cp /tmp/new-pcr.policy ~/.openclaw/tpm/pcr.policy

# 4. Cleanup
shred -u /tmp/age-key-backup.txt /tmp/primary.ctx /tmp/seal.ctx /tmp/new-pcr.policy

# 5. Verify
tpm2_startauthsession --policy-session -S /tmp/s.ctx
tpm2_policypcr -S /tmp/s.ctx -l sha256:0,1,2,3,7
tpm2_unseal -c 0x81000001 -p session:/tmp/s.ctx  # should print AGE-SECRET-KEY-...
tpm2_flushcontext /tmp/s.ctx && rm /tmp/s.ctx
```

### Verify secrets health

```bash
# Test TPM unseal
echo '{"protocolVersion":1,"provider":"tpm","ids":["anthropic_token"]}' | \
  ~/.openclaw/secrets/tpm-secret-resolver.sh

# OpenClaw audit (should show 0 plaintext after migration)
openclaw secrets audit --check

# ACP doctor (checks ACPX runtime health)
openclaw acp doctor
```

### Emergency recovery

If PCR values change unexpectedly (firmware update without re-sealing):

1. The age key **cannot be recovered from the TPM** — this is by design
2. **Recovery options:**
   - If `seal.pub` + `seal.priv` files exist: can only unseal if PCR values match
   - If you have a backup of the age key: re-encrypt secrets with new sealed key
   - If no backup exists: regenerate all API keys from provider dashboards

**Recommendation:** Keep an offline backup of the age secret key (printed on paper, hardware security key, or encrypted USB stored physically secure).

## Installation Summary

### On Proxmox host:
```bash
apt install tpm2-tools
echo 'SUBSYSTEM=="tpmrm", MODE="0666"' > /etc/udev/rules.d/99-tpm-lxc.rules
udevadm control --reload-rules && udevadm trigger
# Add to /etc/pve/lxc/101.conf:
#   lxc.cgroup2.devices.allow: c 252:65536 rwm
#   lxc.mount.entry: /dev/tpmrm0 dev/tpmrm0 none bind,create=file
pct reboot 101
```

### Inside LXC 101:
```bash
sudo apt install tpm2-tools jq
# Install age v1.2.1 and sops v3.9.4 from GitHub releases to /usr/local/bin/
# Generate age keypair, seal private key in TPM, delete plaintext
# Create secrets.enc.json with sops encrypt
# Deploy tpm-secret-resolver.sh wrapper
# Configure openclaw secrets provider
# Run: openclaw secrets configure --skip-provider-setup --apply
```

## Dependencies

| Package | Version | Location |
|---------|---------|----------|
| tpm2-tools | 5.7 (host) / 5.6 (LXC) | apt |
| age | 1.2.1 | /usr/local/bin/age |
| sops | 3.9.4 | /usr/local/bin/sops |
| jq | system | apt |

## Age Public Key

```
age156g4lu8fq6ap5hamr5yuc38d3kh9dqyzcmgvyszkpz6a3lmkepjqapg005
```

This is the **public** key — safe to store in git, documentation, and config files.
The corresponding private key exists **only inside the TPM**.
