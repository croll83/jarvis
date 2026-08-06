# =============================================================================
# HAOS-WAGMI — Terraform Version Constraints
# =============================================================================
# Modulo self-contained: crea una VM Home Assistant OS su un nodo Proxmox
# DEDICATO (pve-wagmi, 100.99.14.73), separato dal root-module che gestisce
# pve-albani. Tenuto isolato perché i due host non sono in cluster: ognuno
# espone la propria API e va gestito con un provider/state separati.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = ">= 0.66.0"
    }
  }
}
