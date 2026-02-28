# =============================================================================
# JARVIS — LXC Container
# =============================================================================
# Un singolo resource per entrambi i deploy type.
# La GPU è gestita via device_passthrough (solo per lxc_gpu).
#
# Prerequisiti per lxc_gpu:
#   1. Driver NVIDIA installato sull'HOST Proxmox
#   2. Device /dev/nvidia* presenti sull'host
#   3. Dopo terraform apply, eseguire lo script configure-gpu.sh
#      per aggiungere le righe cgroup2 al conf LXC
# =============================================================================

resource "proxmox_virtual_environment_container" "jarvis" {
  count = var.jarvis_enabled ? 1 : 0

  lifecycle {
    prevent_destroy = true
  }

  description   = var.deploy_type == "lxc_gpu" ? "JARVIS AI — LXC con GPU NVIDIA" : "JARVIS AI — LXC (API mode)"
  node_name     = var.proxmox_node
  vm_id         = var.ct_id > 0 ? var.ct_id : null
  tags          = var.deploy_type == "lxc_gpu" ? ["jarvis", "gpu", "ai"] : ["jarvis", "api"]
  started       = true
  start_on_boot = true
  unprivileged  = true

  # Docker in LXC richiede nesting + keyctl
  features {
    nesting = true
    keyctl  = true
  }

  # Template OS
  operating_system {
    template_file_id = var.lxc_template_file_id
    type             = "ubuntu"
  }

  # Inizializzazione
  initialization {
    hostname = var.hostname

    ip_config {
      ipv4 {
        address = var.ip_address
        gateway = var.ip_address != "dhcp" ? var.gateway : null
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

  # CPU
  cpu {
    cores = var.lxc_cores
  }

  # RAM
  memory {
    dedicated = var.lxc_memory
    swap      = var.lxc_swap
  }

  # Disco root
  disk {
    datastore_id = var.datastore_id
    size         = var.lxc_disk_size
  }

  # Rete
  network_interface {
    name   = "eth0"
    bridge = var.network_bridge
  }

  # GPU Device Passthrough (solo per lxc_gpu)
  # Monta i device NVIDIA dall'host nel container
  dynamic "device_passthrough" {
    for_each = var.deploy_type == "lxc_gpu" ? var.gpu_devices : []
    content {
      path = device_passthrough.value
      mode = "0666"
    }
  }
}
