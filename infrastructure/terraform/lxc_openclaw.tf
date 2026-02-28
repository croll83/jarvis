# =============================================================================
# JARVIS — LXC OpenClaw (Gateway + Chrome headless)
# =============================================================================
# LXC dedicato per OpenClaw: Node.js bare-metal, Chrome headless (CDP),
# browser-dom plugin, skill dependencies (Linuxbrew).
# Docker SI (per lab-server e futuri container), NO GPU.
# Nesting abilitato per Docker-in-LXC.
#
# Ansible playbook openclaw.yml configura il software al suo interno.
# =============================================================================

resource "proxmox_virtual_environment_container" "openclaw" {
  count = var.openclaw_enabled ? 1 : 0

  lifecycle {
    prevent_destroy = true
  }

  description   = "JARVIS OpenClaw — Gateway Gemini + Chrome CDP"
  node_name     = var.proxmox_node
  vm_id         = var.openclaw_ct_id > 0 ? var.openclaw_ct_id : null
  tags          = ["jarvis", "openclaw"]
  started       = true
  start_on_boot = true
  unprivileged  = true

  # Nesting per eventuali container interni (non strettamente necessario,
  # ma non fa male e mantiene coerenza con gli altri LXC)
  features {
    nesting = true
    keyctl  = true
  }

  operating_system {
    template_file_id = var.openclaw_template_file_id != "" ? var.openclaw_template_file_id : var.lxc_template_file_id
    type             = "ubuntu"
  }

  initialization {
    hostname = var.openclaw_hostname

    ip_config {
      ipv4 {
        address = var.openclaw_ip_address
        gateway = var.openclaw_ip_address != "dhcp" ? var.gateway : null
      }
    }

    dns {
      servers = var.dns_servers
    }

    user_account {
      keys     = [var.ssh_public_key]
      password = var.lxc_password != "" ? var.lxc_password : null
    }
  }

  cpu {
    cores = var.openclaw_cores
  }

  memory {
    dedicated = var.openclaw_memory
    swap      = 1024
  }

  disk {
    datastore_id = var.datastore_id
    size         = var.openclaw_disk_size
  }

  network_interface {
    name   = "eth0"
    bridge = var.network_bridge
  }
}
