# =============================================================================
# JARVIS — Terraform Outputs
# =============================================================================

# -----------------------------------------------------------------------------
# LXC-JARVIS
# -----------------------------------------------------------------------------
output "deploy_type" {
  description = "Tipo di deploy utilizzato"
  value       = var.deploy_type
}

output "ct_id" {
  description = "Proxmox Container ID (LXC-JARVIS)"
  value       = var.jarvis_enabled ? proxmox_virtual_environment_container.jarvis[0].vm_id : null
}

output "ipv4_address" {
  description = "Indirizzo IPv4 di LXC-JARVIS"
  value       = var.jarvis_enabled ? (var.ip_address != "dhcp" ? split("/", var.ip_address)[0] : "controlla-proxmox-ui") : null
}

output "ssh_command" {
  description = "Comando SSH per connettersi a LXC-JARVIS"
  value       = var.jarvis_enabled && var.ip_address != "dhcp" ? "ssh root@${split("/", var.ip_address)[0]}" : null
}

output "ansible_host" {
  description = "Valore host per inventario Ansible (LXC-JARVIS)"
  value       = var.jarvis_enabled && var.ip_address != "dhcp" ? split("/", var.ip_address)[0] : null
}

# -----------------------------------------------------------------------------
# LXC-OpenClaw
# -----------------------------------------------------------------------------
output "openclaw_ct_id" {
  description = "Proxmox Container ID (LXC-OpenClaw)"
  value       = var.openclaw_enabled ? proxmox_virtual_environment_container.openclaw[0].vm_id : null
}

output "openclaw_ip" {
  description = "IP di LXC-OpenClaw"
  value       = var.openclaw_enabled ? (var.openclaw_ip_address != "dhcp" ? split("/", var.openclaw_ip_address)[0] : "controlla-proxmox-ui") : null
}

# -----------------------------------------------------------------------------
# VM Workstation
# -----------------------------------------------------------------------------
output "workstation_vm_id" {
  description = "Proxmox VM ID della workstation"
  value       = var.workstation_enabled ? proxmox_virtual_environment_vm.workstation[0].vm_id : null
}

output "workstation_ip" {
  description = "IP della workstation"
  value       = var.workstation_enabled ? (var.workstation_ip_address != "dhcp" ? split("/", var.workstation_ip_address)[0] : "controlla-proxmox-ui") : null
}

# -----------------------------------------------------------------------------
# Riepilogo
# -----------------------------------------------------------------------------
output "next_steps" {
  description = "Prossimi passi dopo terraform apply"
  value       = <<-EOT

    ================================================================
    JARVIS Terraform — Infrastruttura creata!
    ================================================================
    %{if var.jarvis_enabled}

    📦 LXC-JARVIS:     CT ${proxmox_virtual_environment_container.jarvis[0].vm_id}
       Deploy Type:    ${var.deploy_type}
       IP:             ${var.ip_address != "dhcp" ? split("/", var.ip_address)[0] : "DHCP — vedi Proxmox UI"}
    %{if var.deploy_type == "lxc_gpu"}
       ⚠️  GPU: esegui sull'host Proxmox:
       bash configure-gpu.sh
    %{endif}
    %{endif}
    %{if var.openclaw_enabled}

    📦 LXC-OpenClaw:   CT ${proxmox_virtual_environment_container.openclaw[0].vm_id}
       IP:             ${var.openclaw_ip_address != "dhcp" ? split("/", var.openclaw_ip_address)[0] : "DHCP — vedi Proxmox UI"}
    %{endif}
    %{if var.workstation_enabled}

    🖥️  VM Workstation:  ${var.workstation_hostname} (VM ${proxmox_virtual_environment_vm.workstation[0].vm_id})
       IP:             ${var.workstation_ip_address != "dhcp" ? split("/", var.workstation_ip_address)[0] : "DHCP — vedi Proxmox UI"}

       La VM si avvia con l'ISO Ubuntu. Completa l'installazione da
       Proxmox Web UI (noVNC), poi:
         ansible-playbook playbooks/workstation.yml
    %{endif}

    Prossimi passi:
    %{if var.jarvis_enabled && var.deploy_type == "lxc_gpu"}
    1. Configura GPU cgroup (sull'host Proxmox):
       bash configure-gpu.sh
    %{endif}
    %{if var.jarvis_enabled || var.openclaw_enabled}

    2. Configura Ansible:
       cd ../ansible
       cp inventory/hosts.yml.example inventory/hosts.yml
       cp group_vars/all.yml.example group_vars/all.yml
       nano group_vars/all.yml  # API keys, token HA, ecc.

    3. Esegui Ansible (solo i componenti abilitati):
    %{if var.jarvis_enabled}
       ansible-playbook playbooks/site.yml --tags common,nvidia,jarvis,verify
    %{endif}
    %{if var.openclaw_enabled}
       ansible-playbook playbooks/site.yml --tags openclaw
    %{endif}
    %{endif}
    %{if var.workstation_enabled}

    4. Installa Ubuntu Desktop dalla console noVNC Proxmox
    5. ansible-playbook playbooks/workstation.yml
    %{endif}

    ================================================================
  EOT
}
