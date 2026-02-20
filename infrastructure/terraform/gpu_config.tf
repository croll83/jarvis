# =============================================================================
# JARVIS — GPU cgroup Configuration (post-create)
# =============================================================================
# Il provider bpg/proxmox non supporta l'injection di righe raw nel conf LXC.
# Per la GPU NVIDIA servono le righe lxc.cgroup2.devices.allow nel conf.
#
# Questo file genera uno script da eseguire sull'host Proxmox.
# Se SSH verso l'host è disponibile, lo esegue automaticamente.
# Altrimenti lo script può essere eseguito manualmente.
#
# Prerequisiti:
#   - Driver NVIDIA installato sull'host Proxmox
#   - Device /dev/nvidia* presenti
# =============================================================================

# Script da eseguire sull'host Proxmox per configurare cgroup GPU
resource "local_file" "gpu_config_script" {
  count    = var.jarvis_enabled && var.deploy_type == "lxc_gpu" ? 1 : 0
  filename = "${path.module}/configure-gpu.sh"

  content = <<-SCRIPT
    #!/bin/bash
    # =============================================================================
    # JARVIS — Configura cgroup GPU per LXC
    # =============================================================================
    # Generato automaticamente da Terraform.
    #
    # Esegui SULL'HOST PROXMOX:
    #   bash configure-gpu.sh
    #
    # Oppure da remoto:
    #   scp configure-gpu.sh root@proxmox-host:~/
    #   ssh root@proxmox-host "bash ~/configure-gpu.sh"
    # =============================================================================

    set -e

    CT_ID="${proxmox_virtual_environment_container.jarvis[0].vm_id}"
    CONF="/etc/pve/lxc/$CT_ID.conf"

    echo "📋 Configurazione GPU cgroup per CT $CT_ID"
    echo ""

    # Verifica che il file conf esista
    if [ ! -f "$CONF" ]; then
      echo "❌ File conf non trovato: $CONF"
      echo "   Assicurati di essere sull'host Proxmox e che il CT esista."
      exit 1
    fi

    # Verifica device NVIDIA sull'host
    if [ ! -c /dev/nvidia0 ]; then
      echo "❌ /dev/nvidia0 non trovato sull'host!"
      echo "   Installa i driver NVIDIA sull'host Proxmox prima."
      echo "   Guida: infrastructure/PROXMOX.md sezione 'Driver NVIDIA su Proxmox Host'"
      exit 1
    fi

    # Verifica se già configurato
    if grep -q "lxc.cgroup2.devices.allow" "$CONF" 2>/dev/null; then
      echo "⚠️  Righe cgroup già presenti in $CONF:"
      grep "lxc.cgroup" "$CONF"
      echo ""
      echo "Per riconfigurare, rimuovi le righe e riesegui lo script."
      exit 0
    fi

    # Aggiungi device cgroup
    echo "" >> "$CONF"
    echo "# NVIDIA GPU device access (aggiunto da Terraform/JARVIS)" >> "$CONF"
    %{for major in var.gpu_cgroup_device_majors~}
    echo "lxc.cgroup2.devices.allow: c ${major}:* rwm" >> "$CONF"
    %{endfor~}

    echo "✅ Righe cgroup aggiunte a $CONF:"
    grep "lxc.cgroup" "$CONF"

    # Riavvia container per applicare
    echo ""
    echo "🔄 Riavvio CT $CT_ID..."
    pct stop "$CT_ID" 2>/dev/null || true
    sleep 2
    pct start "$CT_ID"
    sleep 3

    # Verifica device dentro il container
    echo ""
    echo "🔍 Verifica device dentro il container:"
    pct exec "$CT_ID" -- ls -la /dev/nvidia* 2>/dev/null || echo "⚠️  Device non ancora visibili, potrebbe servire un reboot dell'host"

    echo ""
    echo "✅ Configurazione GPU completata per CT $CT_ID"
    echo ""
    echo "Prossimo passo: esegui Ansible per installare NVIDIA Container Toolkit"
    echo "  cd infrastructure/ansible && ansible-playbook playbooks/site.yml"
  SCRIPT

  file_permission = "0755"
}
