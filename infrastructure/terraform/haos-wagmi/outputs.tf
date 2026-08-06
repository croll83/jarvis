# =============================================================================
# HAOS-WAGMI — Outputs
# =============================================================================

output "vm_id" {
  description = "Proxmox VM ID della HAOS Wagmi"
  value       = proxmox_virtual_environment_vm.haos.vm_id
}

output "vm_name" {
  description = "Nome della VM"
  value       = proxmox_virtual_environment_vm.haos.name
}

output "mac_address" {
  description = "MAC della NIC — usalo per la prenotazione DHCP / IP fisso sul router."
  value       = proxmox_virtual_environment_vm.haos.network_device[0].mac_address
}

output "ipv4_addresses" {
  description = "IP rilevati dal guest agent (disponibili solo a VM avviata e HAOS bootato)."
  value       = try(proxmox_virtual_environment_vm.haos.ipv4_addresses, [])
}

output "alexa_ct_id" {
  description = "CT ID del container alexa-skill (cloudflared)"
  value       = var.alexa_enabled ? proxmox_virtual_environment_container.alexa[0].vm_id : null
}

output "next_steps" {
  description = "Cosa fare dopo terraform apply"
  value       = <<-EOT

    ================================================================
    HAOS-WAGMI — VM creata (VM ${proxmox_virtual_environment_vm.haos.vm_id} su ${var.proxmox_node})
    ================================================================
    MAC NIC: ${proxmox_virtual_environment_vm.haos.network_device[0].mac_address}

    1. Avvia la VM (se start_on_create = false):
         ssh root@100.99.14.73 "qm start ${proxmox_virtual_environment_vm.haos.vm_id}"
       oppure dalla Proxmox UI.

    2. Apri la console (noVNC) e attendi il boot di HAOS. Trova l'IP:
         - Proxmox UI → Summary (guest agent), oppure
         - http://homeassistant.local:8123  (onboarding)

    3. (Consigliato) Fissa l'IP: prenotazione DHCP sul router col MAC sopra.

    4. Completa l'onboarding HA (crea utente admin) e genera un
       Long-Lived Token: Profilo → Token di lunga durata.

    5. Replica add-on / HACS / memory-service:
         cd ../../scripts/haos-replicate
         cp .env.example .env   # incolla URL + token della NUOVA istanza
         ./replicate.sh

    ================================================================
  EOT
}
