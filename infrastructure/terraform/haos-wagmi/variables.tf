# =============================================================================
# HAOS-WAGMI — Variables
# =============================================================================

# -----------------------------------------------------------------------------
# Proxmox Connection (pve-wagmi)
# -----------------------------------------------------------------------------
variable "proxmox_endpoint" {
  description = "URL API del nodo pve-wagmi (es. https://100.99.14.73:8006/)"
  type        = string
  default     = "https://100.99.14.73:8006/"
}

variable "proxmox_username" {
  description = "Username Proxmox (root@pam — serve per import disco e OVMF)"
  type        = string
  default     = "root@pam"
}

variable "proxmox_password" {
  description = "Password di root@pam su pve-wagmi"
  type        = string
  sensitive   = true
}

variable "proxmox_insecure" {
  description = "Ignora verifica TLS (certificato self-signed Proxmox)"
  type        = bool
  default     = true
}

variable "proxmox_node" {
  description = "Nome del nodo Proxmox di destinazione"
  type        = string
  default     = "pve-wagmi"
}

variable "proxmox_host_ssh" {
  description = "Host/IP SSH del nodo Proxmox (per staging immagine e import disco). Usa root + chiave in agent."
  type        = string
  default     = "100.99.14.73"
}

# -----------------------------------------------------------------------------
# HAOS image
# -----------------------------------------------------------------------------
# Allinea la versione a quella di Albani (17.3) per una replica 1:1.
# La OVA è un qcow2 compresso .xz scaricato dalle release ufficiali.
variable "haos_version" {
  description = "Versione Home Assistant OS (OVA generic x86-64)"
  type        = string
  default     = "17.3"
}

variable "haos_image_url" {
  description = "URL del qcow2.xz HAOS. {ver} viene sostituito con haos_version."
  type        = string
  default     = "https://github.com/home-assistant/operating-system/releases/download/{ver}/haos_ova-{ver}.qcow2.xz"
}

variable "image_datastore_id" {
  description = "Storage Proxmox dove scaricare l'immagine (deve avere content 'iso'). Su pve-wagmi: 'local'."
  type        = string
  default     = "local"
}

# -----------------------------------------------------------------------------
# VM hardware (replica delle specifiche di Albani20)
# -----------------------------------------------------------------------------
variable "vm_id" {
  description = "Proxmox VM ID (0 = auto-assign). Su pve-wagmi 100/101/200/201 sono occupati."
  type        = number
  default     = 210
}

variable "vm_name" {
  description = "Nome della VM"
  type        = string
  default     = "haos-wagmi"
}

variable "vm_cores" {
  description = "vCPU. Albani usa 2."
  type        = number
  default     = 2
}

variable "vm_memory" {
  description = "RAM in MB. Albani usa 8 GB."
  type        = number
  default     = 8192
}

variable "vm_disk_size" {
  description = "Disco in GB. Albani ha ~62 GB → default 64."
  type        = number
  default     = 64
}

variable "datastore_id" {
  description = "Storage Proxmox per il disco della VM"
  type        = string
  default     = "local-lvm"
}

# -----------------------------------------------------------------------------
# Network
# -----------------------------------------------------------------------------
variable "network_bridge" {
  description = "Bridge di rete Proxmox"
  type        = string
  default     = "vmbr0"
}

variable "vlan_id" {
  description = "VLAN tag (0 = nessuna)"
  type        = number
  default     = 0
}

# HAOS prende l'IP in DHCP dal suo OS (non usa cloud-init). Per un IP fisso,
# imposta la prenotazione DHCP sul router usando il MAC della VM (in output).
variable "mac_address" {
  description = "MAC address fisso per la NIC (vuoto = generato da Proxmox). Utile per prenotazione DHCP."
  type        = string
  default     = ""
}

variable "start_on_create" {
  description = "Avvia la VM subito dopo terraform apply (l'autostart al boot dell'host è sempre attivo)"
  type        = bool
  default     = false
}

# -----------------------------------------------------------------------------
# LXC Alexa-skill (Cloudflare tunnel) — replica CT200 di Albani
# -----------------------------------------------------------------------------
variable "alexa_enabled" {
  description = "Crea il container alexa-skill (cloudflared tunnel)"
  type        = bool
  default     = true
}

variable "alexa_ct_id" {
  description = "Proxmox CT ID per alexa-skill (0 = auto). 210 è la VM HAOS."
  type        = number
  default     = 211
}

variable "alexa_hostname" {
  description = "Hostname del container"
  type        = string
  default     = "alexa-skill"
}

variable "alexa_template_file_id" {
  description = "Template LXC Debian 12 (presente su pve-wagmi local)"
  type        = string
  default     = "local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst"
}

variable "alexa_started" {
  description = "Avvia il container subito dopo la creazione (serve per installare cloudflared)"
  type        = bool
  default     = true
}

variable "alexa_root_password" {
  description = "Password root del container (opzionale, per console)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "ssh_public_key" {
  description = "Chiave pubblica SSH da iniettare nel container (opzionale)"
  type        = string
  default     = ""
}

variable "dns_servers" {
  description = "DNS per il container"
  type        = list(string)
  default     = ["192.168.1.1", "1.1.1.1"]
}

variable "cloudflare_tunnel_token" {
  description = "Token del tunnel Cloudflare (NUOVO, non quello di Albani). Token mode: cloudflared service install <token>."
  type        = string
  sensitive   = true
  default     = ""
}
