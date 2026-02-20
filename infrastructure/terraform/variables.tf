# =============================================================================
# JARVIS — Terraform Variables
# =============================================================================
# LXC per lo stack JARVIS + VM opzionale per workstation desktop.
# La GPU è condivisa dall'host Proxmox via device binding (cgroup2).

# -----------------------------------------------------------------------------
# Deployment Type
# -----------------------------------------------------------------------------
variable "deploy_type" {
  description = "Tipo di deploy: 'lxc_gpu' per LXC con GPU NVIDIA, 'lxc' per container senza GPU"
  type        = string
  default     = "lxc_gpu"

  validation {
    condition     = contains(["lxc_gpu", "lxc"], var.deploy_type)
    error_message = "deploy_type deve essere 'lxc_gpu' o 'lxc'."
  }
}

# -----------------------------------------------------------------------------
# Proxmox Connection
# -----------------------------------------------------------------------------
variable "proxmox_endpoint" {
  description = "URL API Proxmox (es. https://192.168.1.10:8006/)"
  type        = string
}

variable "proxmox_api_token" {
  description = "Proxmox API token (formato: user@realm!tokenid=secret)"
  type        = string
  sensitive   = true
}

variable "proxmox_insecure" {
  description = "Ignora verifica TLS (per certificati self-signed)"
  type        = bool
  default     = true
}

variable "proxmox_node" {
  description = "Nome del nodo Proxmox"
  type        = string
  default     = "pve"
}

# -----------------------------------------------------------------------------
# LXC Settings
# -----------------------------------------------------------------------------
variable "hostname" {
  description = "Hostname per il container LXC"
  type        = string
  default     = "jarvis"
}

variable "ct_id" {
  description = "Proxmox CT ID (0 = auto-assign)"
  type        = number
  default     = 0
}

variable "ssh_public_key" {
  description = "Chiave pubblica SSH per accesso"
  type        = string
}

variable "lxc_password" {
  description = "Password root per il container LXC"
  type        = string
  sensitive   = true
  default     = ""
}

variable "lxc_cores" {
  description = "Numero core CPU"
  type        = number
  default     = 8
}

variable "lxc_memory" {
  description = "RAM in MB"
  type        = number
  default     = 24576
}

variable "lxc_swap" {
  description = "Swap in MB"
  type        = number
  default     = 2048
}

variable "lxc_disk_size" {
  description = "Dimensione disco root in GB"
  type        = number
  default     = 400
}

variable "lxc_template_file_id" {
  description = "Template OS per LXC (es. local:vztmpl/ubuntu-22.04-standard_22.04-1_amd64.tar.zst)"
  type        = string
}

# -----------------------------------------------------------------------------
# Network
# -----------------------------------------------------------------------------
variable "network_bridge" {
  description = "Bridge di rete Proxmox"
  type        = string
  default     = "vmbr0"
}

variable "ip_address" {
  description = "IP statico in notazione CIDR (es. 192.168.1.50/24) oppure 'dhcp'"
  type        = string
  default     = "dhcp"
}

variable "gateway" {
  description = "Gateway predefinito (necessario se ip_address è statico)"
  type        = string
  default     = ""
}

variable "dns_servers" {
  description = "Lista server DNS"
  type        = list(string)
  default     = ["8.8.8.8", "1.1.1.1"]
}

# -----------------------------------------------------------------------------
# Storage
# -----------------------------------------------------------------------------
variable "datastore_id" {
  description = "Storage Proxmox per il disco root"
  type        = string
  default     = "local-lvm"
}

# -----------------------------------------------------------------------------
# GPU Device Paths (solo per deploy_type = "lxc_gpu")
# -----------------------------------------------------------------------------
# Questi device devono esistere sull'host Proxmox (driver NVIDIA installato).
# Trova i tuoi device con: ls -la /dev/nvidia*
#
# Tipicamente:
#   /dev/nvidia0      — GPU 0
#   /dev/nvidiactl    — Control device
#   /dev/nvidia-uvm   — Unified Virtual Memory
#   /dev/nvidia-uvm-tools — UVM tools (opzionale)

variable "gpu_devices" {
  description = "Lista di device paths NVIDIA da montare nell'LXC"
  type        = list(string)
  default = [
    "/dev/nvidia0",
    "/dev/nvidiactl",
    "/dev/nvidia-uvm",
    "/dev/nvidia-uvm-tools"
  ]
}

# I major number dei device NVIDIA per cgroup allow.
# Trova con: stat -c '%t' /dev/nvidia0 (hex) → converti in decimale
# Default comuni: 195 (nvidia), 234 (nvidia-uvm), 509/510 (nvidia-caps)
variable "gpu_cgroup_device_majors" {
  description = "Major number dei device NVIDIA per cgroup2 allow (decimale)"
  type        = list(number)
  default     = [195, 234, 509]
}

# -----------------------------------------------------------------------------
# VM Workstation (opzionale — Ubuntu Desktop + XFCE)
# -----------------------------------------------------------------------------
# VM KVM per uso desktop: Chrome reale + OpenClaw browser extension,
# IDE, Git, Node.js, Python 3. Accessibile via RDP (xrdp) e noVNC.
# Vedi WORKSTATION.md per la configurazione post-install.

variable "workstation_enabled" {
  description = "Crea la VM Workstation (true/false)"
  type        = bool
  default     = false
}

variable "workstation_hostname" {
  description = "Hostname per la VM Workstation"
  type        = string
  default     = "jarvis-workstation"
}

variable "workstation_vm_id" {
  description = "Proxmox VM ID per la workstation (0 = auto-assign)"
  type        = number
  default     = 0
}

variable "workstation_cores" {
  description = "Numero core CPU per la workstation"
  type        = number
  default     = 6
}

variable "workstation_memory" {
  description = "RAM in MB per la workstation"
  type        = number
  default     = 12288
}

variable "workstation_disk_size" {
  description = "Dimensione disco root in GB per la workstation"
  type        = number
  default     = 400
}

variable "workstation_iso_file_id" {
  description = "ISO Ubuntu Desktop su Proxmox (es. local:iso/ubuntu-24.04.2-desktop-amd64.iso)"
  type        = string
  default     = "local:iso/ubuntu-24.04.2-desktop-amd64.iso"
}

variable "workstation_ip_address" {
  description = "IP statico della workstation in CIDR (es. 192.168.1.60/24) oppure 'dhcp'"
  type        = string
  default     = "dhcp"
}

variable "workstation_password" {
  description = "Password utente jarvis per la workstation"
  type        = string
  sensitive   = true
  default     = ""
}
