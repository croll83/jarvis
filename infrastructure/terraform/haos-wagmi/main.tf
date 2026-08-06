# =============================================================================
# HAOS-WAGMI — Provider Configuration
# =============================================================================
# Punta al nodo pve-wagmi (NON pve-albani). Vedi terraform.tfvars.

provider "proxmox" {
  endpoint = var.proxmox_endpoint
  insecure = var.proxmox_insecure

  # root@pam con password: coerente col root-module JARVIS, e necessario
  # per import disco (download + import_from) e gestione EFI/OVMF.
  username = var.proxmox_username
  password = var.proxmox_password

  # L'import del disco qcow2 (import_from) richiede accesso SSH all'host.
  ssh {
    agent    = true
    username = "root"
  }
}
