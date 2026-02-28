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

    # -----------------------------------------------------------------
    # FASE 1: Device cgroup
    # -----------------------------------------------------------------
    if ! grep -q "lxc.cgroup2.devices.allow" "$CONF" 2>/dev/null; then
      echo "" >> "$CONF"
      echo "# NVIDIA GPU device access (aggiunto da Terraform/JARVIS)" >> "$CONF"
      %{for major in var.gpu_cgroup_device_majors~}
      echo "lxc.cgroup2.devices.allow: c ${major}:* rwm" >> "$CONF"
      %{endfor~}
      echo "✅ Righe cgroup aggiunte"
    else
      echo "⚠️  Righe cgroup già presenti, skip"
    fi

    # -----------------------------------------------------------------
    # FASE 2: Bind mount librerie NVIDIA dall'host nel container
    # -----------------------------------------------------------------
    # Il container ha i device /dev/nvidia* ma NON le librerie userspace.
    # Docker/nvidia-container-toolkit ha bisogno di libnvidia-ml.so, libcuda.so, ecc.
    echo ""
    echo "📚 Configurazione bind mount librerie NVIDIA..."

    if grep -q "lxc.mount.entry.*libnvidia" "$CONF" 2>/dev/null; then
      echo "⚠️  Bind mount librerie già presenti, skip"
    else
      echo "" >> "$CONF"
      echo "# NVIDIA userspace libraries bind mount (aggiunto da configure-gpu.sh)" >> "$CONF"

      # Trova tutte le librerie NVIDIA sul host
      NVIDIA_LIBS=$(find /usr/lib/x86_64-linux-gnu -maxdepth 1 \( \
        -name "libnvidia-*.so*" -o \
        -name "libcuda*.so*" -o \
        -name "libnvcuvid.so*" -o \
        -name "libnvoptix.so*" -o \
        -name "libGLX_nvidia.so*" -o \
        -name "libEGL_nvidia.so*" -o \
        -name "libGLESv2_nvidia.so*" -o \
        -name "libvdpau_nvidia.so*" \
      \) 2>/dev/null | sort)

      LIB_COUNT=0
      for lib in $NVIDIA_LIBS; do
        relative_path="$${lib#/}"
        echo "lxc.mount.entry: $lib $relative_path none bind,optional,create=file" >> "$CONF"
        LIB_COUNT=$((LIB_COUNT + 1))
      done

      # Monta anche nvidia-smi e nvidia-debugdump se presenti
      for bin in /usr/bin/nvidia-smi /usr/bin/nvidia-debugdump /usr/bin/nvidia-persistenced; do
        if [ -f "$bin" ]; then
          relative_path="$${bin#/}"
          echo "lxc.mount.entry: $bin $relative_path none bind,optional,create=file" >> "$CONF"
          LIB_COUNT=$((LIB_COUNT + 1))
        fi
      done

      # Monta firmware nvidia se presente
      if [ -d "/usr/lib/firmware/nvidia" ]; then
        echo "lxc.mount.entry: /usr/lib/firmware/nvidia usr/lib/firmware/nvidia none bind,optional,create=dir" >> "$CONF"
      fi

      echo "✅ $LIB_COUNT bind mount aggiunti"
    fi

    # -----------------------------------------------------------------
    # FASE 3: Riavvia e verifica
    # -----------------------------------------------------------------
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

    # Verifica librerie dentro il container
    echo ""
    echo "🔍 Verifica librerie NVIDIA dentro il container:"
    pct exec "$CT_ID" -- ls /usr/lib/x86_64-linux-gnu/libnvidia-ml.so* 2>/dev/null && echo "✅ libnvidia-ml.so presente" || echo "❌ libnvidia-ml.so NON trovata!"

    # Prova nvidia-smi dentro il container
    echo ""
    echo "🔍 Test nvidia-smi dentro il container:"
    pct exec "$CT_ID" -- nvidia-smi 2>/dev/null || echo "⚠️  nvidia-smi non funziona (normale: servirà Docker con nvidia runtime)"

    echo ""
    echo "✅ Configurazione GPU completata per CT $CT_ID"
    echo ""
    echo "Prossimo passo: esegui Ansible per installare NVIDIA Container Toolkit"
    echo "  cd infrastructure/ansible && ansible-playbook playbooks/nvidia.yml --limit jarvis"
  SCRIPT

  file_permission = "0755"
}
