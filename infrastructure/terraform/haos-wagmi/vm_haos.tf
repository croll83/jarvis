# =============================================================================
# HAOS-WAGMI — VM Home Assistant OS (KVM)
# =============================================================================
# Replica "a vuoto" della VM HAOS di Albani20:
#   - HA OS 17.3 (OVA generic x86-64), UEFI/OVMF
#   - 2 vCPU host, 8 GB RAM, disco 64 GB su local-lvm
#   - 1 NIC virtio su vmbr0 (DHCP, come Albani: 192.168.1.18/24 → qui DHCP)
#
# NON include integrazioni, device, dashboard o automazioni: quelle (la
# "entity map") sono escluse per scelta. Add-on, HACS e il memory-service
# vengono installati DOPO, dallo script ../scripts/haos-replicate/.
#
# Flusso:
#   1. download_file: scarica e decomprime il qcow2 HAOS su 'local' (iso)
#   2. proxmox_virtual_environment_vm: crea la VM importando quel disco
#
# NB: l'import del disco (import_from) richiede che il provider possa fare
# SSH come root sull'host pve-wagmi (vedi blocco ssh in main.tf).

locals {
  haos_url     = replace(var.haos_image_url, "{ver}", var.haos_version)
  image_name   = "haos_ova-${var.haos_version}.qcow2"
  import_volid = "${var.image_datastore_id}:import/${local.image_name}"
}

# -----------------------------------------------------------------------------
# Staging immagine HAOS
# -----------------------------------------------------------------------------
# Il provider bpg NON sa decomprimere xz (solo gz/lzo/zst/bz2) e HAOS è
# distribuito solo come .qcow2.xz. Inoltre l'host pve-wagmi è isolato (niente
# egress internet/DNS). Quindi: scarichiamo+decomprimiamo l'immagine sulla
# macchina che esegue terraform e la copiamo via scp nello storage 'import' di
# 'local' (già abilitato: content iso,vztmpl,backup,import). Idempotente:
# se l'immagine è già sull'host, non fa nulla.
resource "terraform_data" "haos_image" {
  triggers_replace = [var.haos_version, var.proxmox_host_ssh]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      HOST="${var.proxmox_host_ssh}"
      VOLDIR="/var/lib/vz/import"
      REMOTE="$VOLDIR/${local.image_name}"
      SSH="ssh -o StrictHostKeyChecking=accept-new root@$HOST"
      if $SSH "test -f $REMOTE"; then
        echo "[haos_image] già presente: $REMOTE"; exit 0
      fi
      CACHE="${path.module}/.image"; mkdir -p "$CACHE"
      LOCAL="$CACHE/${local.image_name}"
      if [ ! -f "$LOCAL" ]; then
        echo "[haos_image] download ${local.haos_url}"
        curl -fL --retry 3 -o "$CACHE/haos.qcow2.xz" "${local.haos_url}"
        unxz -f "$CACHE/haos.qcow2.xz"
        mv "$CACHE/haos.qcow2" "$LOCAL"
      fi
      $SSH "mkdir -p $VOLDIR"
      echo "[haos_image] scp -> $HOST:$REMOTE"
      scp -o StrictHostKeyChecking=accept-new "$LOCAL" "root@$HOST:$REMOTE"
      $SSH "qemu-img info $REMOTE | head -3"
    EOT
  }
}

resource "proxmox_virtual_environment_vm" "haos" {
  name        = var.vm_name
  description = "Home Assistant OS ${var.haos_version} — replica stack Albani20 (Wagmi). Add-on/HACS via scripts/haos-replicate."
  node_name   = var.proxmox_node
  vm_id       = var.vm_id > 0 ? var.vm_id : null
  tags        = ["jarvis", "haos", "wagmi"]

  # Avvio automatico all'accensione dell'host pve-wagmi (autostart).
  on_boot = true
  # Avvio subito dopo terraform apply: separato dall'autostart (vedi var).
  started = var.start_on_create

  # HAOS OVA è UEFI: BIOS OVMF + EFI disk. (Diverso dalla workstation SeaBIOS.)
  bios    = "ovmf"
  machine = "q35"

  # virtio-scsi-single per iothread sul disco
  scsi_hardware = "virtio-scsi-single"

  # QEMU Guest Agent — HAOS lo include (os-agent), così Proxmox vede l'IP.
  agent {
    enabled = true
    type    = "virtio"
  }

  cpu {
    cores   = var.vm_cores
    sockets = 1
    type    = "host"
  }

  memory {
    dedicated = var.vm_memory
  }

  # EFI vars per OVMF. HAOS non usa Secure Boot → niente chiavi pre-enrolled.
  efi_disk {
    datastore_id      = var.datastore_id
    file_format       = "raw"
    type              = "4m"
    pre_enrolled_keys = false
  }

  # Disco di sistema: importato dal qcow2 HAOS, poi ridimensionato a vm_disk_size.
  depends_on = [terraform_data.haos_image]
  disk {
    datastore_id = var.datastore_id
    interface    = "scsi0"
    import_from  = local.import_volid
    size         = var.vm_disk_size
    iothread     = true
    discard      = "on"
    ssd          = true
  }

  network_device {
    bridge      = var.network_bridge
    model       = "virtio"
    mac_address = var.mac_address != "" ? var.mac_address : null
    vlan_id     = var.vlan_id > 0 ? var.vlan_id : null
  }

  # Console standard: la TUI di HAOS funziona su vga std.
  vga {
    type = "std"
  }

  # HAOS gestisce la rete da sé (DHCP): nessun cloud-init.
  # Ordine d'avvio dopo le VM/CT core dell'host.
  startup {
    order      = "4"
    up_delay   = "30"
    down_delay = "30"
  }

  # Il disco viene importato una volta sola: ignora drift su import_from
  # per evitare ricreazioni accidentali della VM dopo update dell'immagine.
  lifecycle {
    ignore_changes = [
      disk[0].import_from,
    ]
  }
}
