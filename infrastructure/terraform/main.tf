# =============================================================================
# JARVIS — Provider Configuration
# =============================================================================

provider "proxmox" {
  endpoint = var.proxmox_endpoint
  insecure = var.proxmox_insecure

  # Autenticazione: username + password di root@pam.
  # Necessario perché Proxmox richiede root@pam (non API token)
  # per device passthrough e feature flags (keyctl, fuse) sui container LXC.
  username = var.proxmox_username
  password = var.proxmox_password

  ssh {
    agent = true
  }
}
