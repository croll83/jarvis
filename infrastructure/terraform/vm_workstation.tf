# =============================================================================
# JARVIS — VM Workstation (Ubuntu Desktop + GNOME)
# =============================================================================
# Vera VM KVM per uso desktop: Chrome reale + OpenClaw browser extension,
# IDE (Zed), Git, Node.js, Python 3, email, browsing.
#
# Accessibile via:
#   - RustDesk (dal Mac, Direct IP via Tailscale :21118)
#   - RDP nativo (GNOME Remote Desktop :3389 — da Remmina/Proxmox)
#   - noVNC dalla Proxmox Web UI (per installazione/troubleshooting)
#
# Display: VirtIO-GPU (espone /dev/dri/card0+renderD128 per GNOME).
# NON usa GPU dedicata — la GPU NVIDIA resta sull'host per LXC-JARVIS.
# Nota: VirtIO-GPU senza virgl (no 3D) — sufficiente per GNOME X11 + RustDesk.
#
# Prerequisiti:
#   1. ISO Ubuntu Desktop scaricata su Proxmox (local:iso/ubuntu-24.04.x-desktop-amd64.iso)
#   2. workstation_enabled = true in terraform.tfvars
#
# Post-install:
#   Segui WORKSTATION.md per la configurazione software (Chrome, Zed, nvm, ecc.)
# =============================================================================

resource "proxmox_virtual_environment_vm" "workstation" {
  count = var.workstation_enabled ? 1 : 0

  name        = var.workstation_hostname
  description = "JARVIS Workstation — Ubuntu Desktop + Chrome + OpenClaw browser extension"
  node_name   = var.proxmox_node
  vm_id       = var.workstation_vm_id > 0 ? var.workstation_vm_id : null
  tags        = ["jarvis", "workstation", "desktop"]

  on_boot    = true
  boot_order = ["scsi0", "ide2"]

  # BIOS — SeaBIOS (più semplice, non serve EFI disk)
  bios    = "seabios"
  machine = "q35"

  # Controller SCSI — virtio-scsi-single per supporto iothread sui dischi
  scsi_hardware = "virtio-scsi-single"

  # QEMU Guest Agent (abilitato dopo installazione Ubuntu + Ansible)
  # NOTA: enabled = false fino a quando qemu-guest-agent non è installato,
  # altrimenti Terraform va in timeout durante refresh/create.
  # Dopo aver eseguito Ansible (che installa il guest agent), cambia a true.
  agent {
    enabled = false
    type    = "virtio"
  }

  # CPU — host passthrough per massime performance
  cpu {
    cores   = var.workstation_cores
    sockets = 1
    type    = "host"
  }

  # RAM
  memory {
    dedicated = var.workstation_memory
  }

  # Disco principale — VirtIO SCSI per performance
  disk {
    datastore_id = var.datastore_id
    interface    = "scsi0"
    size         = var.workstation_disk_size
    iothread     = true
    discard      = "on"
    ssd          = true
  }

  # CDROM — ISO Ubuntu per installazione iniziale
  # Dopo l'installazione, rimuovi o cambia file_id a "cdrom"
  cdrom {
    file_id   = var.workstation_iso_file_id
    interface = "ide2"
  }

  # Rete — stessa bridge delle altre VM/LXC
  network_device {
    bridge = var.network_bridge
    model  = "virtio"
  }

  # Display — VirtIO-GPU per supporto DRI/EGL (necessario per GNOME Remote Desktop)
  # VirtIO-GPU espone /dev/dri/card0 nella VM, che GNOME Shell e gnome-remote-desktop
  # necessitano per il rendering. QXL non espone render nodes e causa
  # "surfaceless renderer without GPU" → handover RDP fallisce.
  vga {
    type   = "virtio-gpu"
    memory = 32
  }

  # Keyboard layout italiano
  keyboard_layout = "it"

  # Startup order — parte dopo LXC-JARVIS e LXC-OpenClaw
  startup {
    order      = "3"
    up_delay   = "30"
    down_delay = "15"
  }

  # Cloud-init (opzionale — Ubuntu Desktop non lo usa di default,
  # ma lo prepariamo per post-install automation)
  initialization {
    datastore_id = var.datastore_id

    ip_config {
      ipv4 {
        address = var.workstation_ip_address
        gateway = var.workstation_ip_address != "dhcp" ? var.gateway : null
      }
    }

    dns {
      servers = var.dns_servers
    }

    user_account {
      username = "jarvis"
      keys     = [var.ssh_public_key]
      password = var.workstation_password != "" ? var.workstation_password : null
    }
  }
}
