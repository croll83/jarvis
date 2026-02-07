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
    JARVIS Terraform — LXC creato!
    ================================================================

    Container ID:  ${proxmox_virtual_environment_container.jarvis.vm_id}
    Deploy Type:   ${var.deploy_type}
    IP:            ${var.ip_address != "dhcp" ? split("/", var.ip_address)[0] : "DHCP — vedi Proxmox UI"}
    %{if var.deploy_type == "lxc_gpu"}

    ⚠️  GPU: Se il provisioner SSH non ha funzionato,
    esegui manualmente sull'host Proxmox:
      bash configure-gpu.sh ${proxmox_virtual_environment_container.jarvis.vm_id}
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

    ================================================================
  EOT
}
