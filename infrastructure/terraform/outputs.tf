# =============================================================================
# JARVIS — Terraform Outputs
# =============================================================================

output "deploy_type" {
  description = "Tipo di deploy utilizzato"
  value       = var.deploy_type
}

output "ct_id" {
  description = "Proxmox Container ID"
  value       = proxmox_virtual_environment_container.jarvis.vm_id
}

output "ipv4_address" {
  description = "Indirizzo IPv4 del container"
  value       = var.ip_address != "dhcp" ? split("/", var.ip_address)[0] : "controlla-proxmox-ui"
}

output "ssh_command" {
  description = "Comando SSH per connettersi"
  value       = var.ip_address != "dhcp" ? "ssh root@${split("/", var.ip_address)[0]}" : "ssh root@<controlla-proxmox-ui>"
}

output "ansible_host" {
  description = "Valore host per inventario Ansible"
  value       = var.ip_address != "dhcp" ? split("/", var.ip_address)[0] : null
}

output "next_steps" {
  description = "Prossimi passi dopo terraform apply"
  value       = <<-EOT

    ================================================================
    JARVIS Terraform — Infrastruttura creata!
    ================================================================

    Container ID:  ${proxmox_virtual_environment_container.jarvis.vm_id}
    Deploy Type:   ${var.deploy_type}
    IP:            ${var.ip_address != "dhcp" ? split("/", var.ip_address)[0] : "DHCP — vedi Proxmox UI"}
    %{if var.deploy_type == "lxc_gpu"}

    ⚠️  GPU: Se il provisioner SSH non ha funzionato,
    esegui manualmente sull'host Proxmox:
      bash configure-gpu.sh ${proxmox_virtual_environment_container.jarvis.vm_id}
    %{endif}
    %{if var.workstation_enabled}

    🖥️  VM Workstation:  ${var.workstation_hostname}
    VM ID:             ${proxmox_virtual_environment_vm.workstation[0].vm_id}
    IP:                ${var.workstation_ip_address != "dhcp" ? split("/", var.workstation_ip_address)[0] : "DHCP — vedi Proxmox UI"}

    La VM si avvia con l'ISO Ubuntu. Completa l'installazione da
    Proxmox Web UI (noVNC), poi segui WORKSTATION.md per il setup.
    %{endif}

    Prossimi passi:

    1. Configura l'inventario Ansible:
       cd ../ansible
       cp inventory/hosts.yml.example inventory/hosts.yml
       # IP: ${var.ip_address != "dhcp" ? split("/", var.ip_address)[0] : "da Proxmox UI"}

    2. Configura le variabili Ansible:
       cp group_vars/all.yml.example group_vars/all.yml
       nano group_vars/all.yml  # API keys, token HA, ecc.

    3. Esegui Ansible:
       ansible-playbook playbooks/site.yml
    %{if var.workstation_enabled}

    4. Installa Ubuntu Desktop dalla console noVNC Proxmox
    5. Segui WORKSTATION.md per configurazione software
    %{endif}

    ================================================================
  EOT
}

# -----------------------------------------------------------------------------
# Workstation Outputs
# -----------------------------------------------------------------------------
output "workstation_vm_id" {
  description = "Proxmox VM ID della workstation"
  value       = var.workstation_enabled ? proxmox_virtual_environment_vm.workstation[0].vm_id : null
}

output "workstation_ip" {
  description = "IP della workstation"
  value       = var.workstation_enabled ? (var.workstation_ip_address != "dhcp" ? split("/", var.workstation_ip_address)[0] : "controlla-proxmox-ui") : null
}
