# =============================================================================
# ALEXA-SKILL — LXC Cloudflare Tunnel (replica di CT 200 su pve-albani)
# =============================================================================
# Container Debian 12 il cui UNICO scopo è eseguire cloudflared in "token mode"
# (`cloudflared service install <token>`), esponendo un hostname pubblico usato
# dalla skill AlexaMediaPlayer. L'ingress (hostname -> backend) è gestito nella
# dashboard Cloudflare Zero Trust, NON in un file locale.
#
# Albani (riferimento): CT 200, Debian 12, 1 core / 512 MB / 8 GB, vmbr0 DHCP,
# privilegiato con features nesting+keyctl, cloudflared 2026.x via apt.
#
# ⚠️ TOKEN: NON riusare quello di Albani. Crea un tunnel NUOVO nello stesso
#    account Cloudflare, instradalo verso l'HA di Wagmi, e incolla il token in
#    var.cloudflare_tunnel_token (terraform.tfvars). Vedi README.

resource "proxmox_virtual_environment_container" "alexa" {
  count = var.alexa_enabled ? 1 : 0

  node_name   = var.proxmox_node
  vm_id       = var.alexa_ct_id > 0 ? var.alexa_ct_id : null
  description = "Alexa skill — Cloudflare tunnel (cloudflared). Replica CT200 Albani."
  tags        = ["jarvis", "alexa", "cloudflared", "wagmi"]

  # Albani è privilegiato con nesting+keyctl (cloudflared gira nativo).
  unprivileged = false

  features {
    nesting = true
    keyctl  = true
  }

  started       = var.alexa_started
  start_on_boot = true

  cpu {
    cores = 1
  }

  memory {
    dedicated = 512
    swap      = 512
  }

  disk {
    datastore_id = var.datastore_id
    size         = 8
  }

  operating_system {
    template_file_id = var.alexa_template_file_id
    type             = "debian"
  }

  network_interface {
    name   = "eth0"
    bridge = var.network_bridge
  }

  initialization {
    hostname = var.alexa_hostname

    ip_config {
      ipv4 {
        address = "dhcp"
      }
    }

    dns {
      servers = var.dns_servers
    }

    # Password root opzionale per console/login; il provisioning usa pct exec.
    user_account {
      password = var.alexa_root_password != "" ? var.alexa_root_password : null
      keys     = var.ssh_public_key != "" ? [var.ssh_public_key] : null
    }
  }

  startup {
    order    = "3"
    up_delay = "30"
  }
}

# -----------------------------------------------------------------------------
# Provisioning cloudflared dentro al CT (via pct exec dall'host)
# -----------------------------------------------------------------------------
# Il container ha internet (vmbr0/DHCP), quindi installa cloudflared dal repo
# apt ufficiale Cloudflare e registra il servizio col token (token mode).
# Idempotente: se cloudflared è già attivo con questo token, non rifà nulla.
resource "terraform_data" "alexa_cloudflared" {
  count = var.alexa_enabled ? 1 : 0

  triggers_replace = [
    var.cloudflare_tunnel_token,
    proxmox_virtual_environment_container.alexa[0].vm_id,
  ]

  depends_on = [proxmox_virtual_environment_container.alexa]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      HOST="${var.proxmox_host_ssh}"
      CTID="${var.alexa_ct_id}"
      TOKEN="${var.cloudflare_tunnel_token}"
      SSH="ssh -o StrictHostKeyChecking=accept-new root@$HOST"
      echo "[alexa] attendo rete nel CT $CTID…"
      $SSH "pct exec $CTID -- bash -lc 'for i in \$(seq 1 30); do getent hosts pkg.cloudflare.com >/dev/null && exit 0; sleep 2; done; exit 1'"
      echo "[alexa] installo cloudflared…"
      $SSH "pct exec $CTID -- bash -lc '
        set -e
        export DEBIAN_FRONTEND=noninteractive
        if ! command -v cloudflared >/dev/null; then
          apt-get update -qq
          apt-get install -y -qq curl gnupg
          mkdir -p --mode=0755 /usr/share/keyrings
          curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg > /usr/share/keyrings/cloudflare-main.gpg
          echo \"deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared bookworm main\" > /etc/apt/sources.list.d/cloudflared.list
          apt-get update -qq
          apt-get install -y -qq cloudflared
        fi
      '"
      if [ -z "$TOKEN" ]; then
        echo "[alexa] NESSUN token fornito: cloudflared installato ma tunnel NON registrato."
        echo "[alexa] Crea il tunnel nuovo, metti il token in terraform.tfvars e ri-applica."
        exit 0
      fi
      echo "[alexa] registro il servizio col token…"
      $SSH "pct exec $CTID -- bash -lc '
        set -e
        systemctl stop cloudflared 2>/dev/null || true
        cloudflared service uninstall 2>/dev/null || true
        cloudflared service install $TOKEN
        # Override del unit: TimeoutStartSec=0 (il Type=notify a 15s uccideva
        # cloudflared prima che registrasse le connessioni) + protocollo http2
        # come fallback se QUIC/UDP-7844 è filtrato. Contenuto in base64 per
        # evitare problemi di escaping/newline nel quoting annidato.
        mkdir -p /etc/systemd/system/cloudflared.service.d
        echo W1NlcnZpY2VdCkVudmlyb25tZW50PVRVTk5FTF9QUk9UT0NPTD1odHRwMgpUaW1lb3V0U3RhcnRTZWM9MAo= | base64 -d > /etc/systemd/system/cloudflared.service.d/override.conf
        systemctl daemon-reload
        systemctl enable --now cloudflared
        sleep 10
        systemctl is-active cloudflared
      '"
      echo "[alexa] cloudflared attivo."
    EOT
  }
}
