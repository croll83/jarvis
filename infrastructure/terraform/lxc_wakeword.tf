# =============================================================================
# JARVIS — LXC Wakeword Server (1 per casa)
# =============================================================================
# Lightweight LXC container running jarvis-wakeword-server.
# Deployed on the SAME Proxmox host as the AtomS3R devices' LAN.
# No GPU needed — openWakeWord runs on CPU (~80ms/inference).
#
# Tailscale installato dentro il container per raggiungibilita
# dall'orchestrator VPS remoto (push config, trigger_listen).
# I device AtomS3R lo raggiungono via IP LAN (stessa rete WiFi).
#
# Esempio: 2 case → 2 container wakeword, ognuno sulla LAN locale.
#
# Deploy alternativo (consigliato): cloud/scripts/deploy-wakeword.sh
# =============================================================================

variable "wakeword_instances" {
  description = "Map of wakeword-server instances (one per casa)"
  type = map(object({
    ct_id              = number
    ip_address         = string
    hostname           = string
    node_name          = string
    tailscale_authkey  = optional(string, "")
  }))
  default = {}
  # Example:
  # wakeword_instances = {
  #   "casa1" = {
  #     ct_id              = 210
  #     ip_address         = "192.168.1.210/24"
  #     hostname           = "jarvis-wakeword-casa1"
  #     node_name          = "pve-casa1"
  #     tailscale_authkey  = "tskey-auth-xxxxx"
  #   }
  # }
}

resource "proxmox_virtual_environment_container" "wakeword" {
  for_each = var.wakeword_instances

  lifecycle {
    prevent_destroy = true
  }

  description   = "JARVIS Wakeword Server — ${each.key}"
  node_name     = each.value.node_name
  vm_id         = each.value.ct_id
  tags          = ["jarvis", "wakeword", each.key]
  started       = true
  start_on_boot = true
  unprivileged  = true

  features {
    nesting = true
    keyctl  = true
  }

  operating_system {
    template_file_id = var.lxc_template_file_id
    type             = "ubuntu"
  }

  initialization {
    hostname = each.value.hostname

    ip_config {
      ipv4 {
        address = each.value.ip_address
        gateway = var.gateway
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
    cores = 1
  }

  memory {
    dedicated = 2048
    swap      = 512
  }

  disk {
    datastore_id = var.datastore_id
    size         = 10
  }

  network_interface {
    name   = "eth0"
    bridge = var.network_bridge
  }
}
