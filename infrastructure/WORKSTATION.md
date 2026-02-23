# VM Workstation — Ubuntu Desktop per JARVIS

VM desktop per sviluppo e controllo browser reale con OpenClaw.
Chrome (non headless) con l'estensione browser OpenClaw supera i controlli
anti-bot di Cloudflare e gestisce SPA dinamiche — impossibile con Chrome headless.

**Funzioni principali:**
- Chrome reale + estensione browser OpenClaw (controllo browser non-headless)
- IDE di sviluppo (Zed)
- Git, Node.js (nvm), Python 3
- Email, browsing, workspace generale

**Accesso:**
- **RustDesk** dal Mac (Direct IP via Tailscale o ID numerico)
- **RDP** (GNOME Remote Desktop) da Proxmox host con Remmina
- **noVNC** dalla Proxmox Web UI (per installazione iniziale e troubleshooting)

> **Nota:** GNOME Remote Desktop RDP ha un bug noto su Ubuntu 24.04 con i client
> Mac (kkRemote, Windows App, Microsoft Remote Desktop) → black screen.
> RustDesk risolve il problema completamente.

---

## Prerequisiti

| Requisito | Dettaglio |
|-----------|-----------|
| Proxmox VE | 8.x installato e funzionante |
| ISO Ubuntu | Scaricata su Proxmox (vedi Step 1) |
| RAM host libera | 12 GB per la VM (oltre a LXC-JARVIS) |
| Disco host libero | 400 GB NVMe |

---

## Step 1 — Scarica la ISO su Proxmox

Dalla Web UI di Proxmox (`https://<proxmox-ip>:8006`):

1. Vai a **local** (o il tuo storage) > **ISO Images** > **Download from URL**
2. URL: `https://releases.ubuntu.com/24.04.2/ubuntu-24.04.2-desktop-amd64.iso`
3. Clicca **Download** e attendi il completamento

Oppure da CLI sull'host Proxmox:

```bash
cd /var/lib/vz/template/iso/
wget https://releases.ubuntu.com/24.04.2/ubuntu-24.04.2-desktop-amd64.iso
```

---

## Step 2 — Crea la VM (Terraform)

```bash
cd infrastructure/terraform

# Se non l'hai ancora fatto:
cp terraform.tfvars.example terraform.tfvars
nano terraform.tfvars
```

Modifica `terraform.tfvars` — abilita la workstation:

```hcl
# Scegli cosa creare:
jarvis_enabled      = false    # disabilita se non installi ancora LXC-JARVIS
openclaw_enabled    = false    # disabilita se non installi ancora LXC-OpenClaw
workstation_enabled = true     # <-- abilita la workstation

# Configurazione workstation:
workstation_hostname    = "jarvis-workstation"
workstation_vm_id       = 200
workstation_cores       = 6
workstation_memory      = 12288       # 12 GB
workstation_disk_size   = 400         # GB
workstation_iso_file_id = "local:iso/ubuntu-24.04.2-desktop-amd64.iso"
workstation_ip_address  = "192.168.1.60/24"   # oppure "dhcp"
workstation_password    = "la-tua-password"
```

```bash
terraform init    # solo la prima volta
terraform plan    # verifica cosa verrà creato
terraform apply   # crea la VM
```

Terraform crea la VM con disco VirtIO, display VirtIO-GPU, rete bridge, e la avvia
automaticamente dall'ISO.

---

## Step 3 — Installa Ubuntu

1. Apri la **console noVNC** dalla Proxmox Web UI (VM 200 > Console)
2. Segui l'installer Ubuntu Desktop:
   - Lingua: Italiano
   - Keyboard: Italian
   - Installazione normale (non minimale — serve il browser)
   - Cancella disco e installa
   - Nome: `jarvis`
   - Hostname: `jarvis-workstation`
   - Password: quella che hai scelto
3. Al termine, riavvia
4. **Rimuovi la ISO dopo il reboot:**
   - Da Proxmox UI: VM > Hardware > CD/DVD > Edit > Do not use any media
   - Oppure da CLI:
     ```bash
     qm set 200 -ide2 none,media=cdrom
     ```
5. **Prepara la VM per Ansible** (dalla console noVNC, loggato come `jarvis`):
   ```bash
   # Installa SSH server (Ubuntu Desktop non lo include)
   sudo apt update && sudo apt install -y openssh-server

   # Abilita sudo senza password (necessario per Ansible)
   echo "jarvis ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/jarvis
   ```
6. **Copia la chiave SSH dal Mac** (dal terminale del Mac):
   ```bash
   # Se non hai una chiave SSH, generala prima:
   # ssh-keygen -t ed25519 -C "jarvis-admin"

   ssh-copy-id jarvis@<IP_DELLA_VM>
   ```

---

## Step 4 — Setup Software (Ansible)

Prerequisiti: SSH attivo, sudo passwordless, chiave SSH copiata (vedi Step 3.5-3.6).

```bash
cd infrastructure/ansible
# Assicurati che inventory/hosts.yml abbia la sezione workstation con l'IP corretto
# Poi:
ansible-playbook playbooks/workstation.yml
```

Il playbook installa automaticamente:
- **qemu-guest-agent** (integrazione Proxmox)
- **Gruppi video/render** per utente jarvis e gdm (necessari per VirtIO-GPU/DRI)
- **GNOME Remote Desktop** (RDP nativo :3389 — per Remmina da Proxmox)
- **RustDesk** (accesso remoto da Mac, Direct IP via Tailscale)
- **Google Chrome** (browser reale, non headless)
- **Git** + generazione chiave SSH
- **nvm + Node.js LTS** (v22.x) + symlink in /usr/local/bin per sudo
- **Python 3** + pip + venv
- **Zed IDE**
- **Tailscale** (opzionale — default: abilitato)
- **UFW firewall** (SSH + RDP + RustDesk aperti)
- **/dev/shm** aumentato a 2 GB (fix Chrome in VM)
- **SSSD disabilitato** (non necessario, genera warning inutili)

### Post-Ansible (manuale)

#### 1. Connettiti via RustDesk dal Mac

Scarica RustDesk da [rustdesk.com](https://rustdesk.com) e installalo sul Mac.

```bash
# Ottieni l'ID RustDesk dalla VM:
ssh jarvis@<IP_VM> "rustdesk --get-id"

# Nel client RustDesk sul Mac:
# - Inserisci l'ID (o l'IP Tailscale per Direct IP)
# - Password: quella configurata in workstation_rdp_password
```

**Via Tailscale (Direct IP, zero relay):**
Una volta connesso Tailscale su entrambi, inserisci l'IP Tailscale della VM
nel campo ID di RustDesk → connessione diretta peer-to-peer via WireGuard.

#### 2. Connettiti via RDP da Proxmox (con Remmina)

Dalla XFCE del host Proxmox, apri Remmina → RDP → `<IP_VM>:3389`.
Funziona perfettamente con GNOME Remote Desktop.

#### 3. Configurazione iniziale

```bash
# Connetti Tailscale
sudo tailscale up --hostname=jarvis-workstation

# Configura Git
git config --global user.name "Il Tuo Nome"
git config --global user.email "tua@email.com"

# Aggiungi chiave SSH a GitHub
cat ~/.ssh/id_ed25519.pub
# Copia e incolla in GitHub > Settings > SSH keys

# Apri Chrome e installa l'estensione OpenClaw browser
# (dal Chrome Web Store o da file .crx)
# Configura l'estensione con l'URL del gateway:
#   LAN: http://192.168.1.51:18789
#   Tailscale: http://jarvis-openclaw:18789
```

> **Nota:** Il browser reale con profilo persistente supera i controlli anti-bot
> di Cloudflare. Per costruire un profilo pulito, naviga manualmente per qualche
> giorno prima di attivare l'automazione OpenClaw. Risolvi i CAPTCHA
> manualmente quando compaiono — il profilo imparer e ne vedrai sempre meno.

---

## Step 5 — Desktop sull'host Proxmox (KVM switch virtuale)

Per usare lo schermo/tastiera/mouse fisici dell'AtomMan per controllare questa VM
(e altre in futuro) come un KVM switch virtuale, vedi:
**[PROXMOX.md — Desktop locale sull'host](PROXMOX.md#9-desktop-locale-sullhost-proxmox-kvm-switch-virtuale)**

---

## Riepilogo risorse

| Componente | Risorsa |
|------------|---------|
| CPU | 6 core (host passthrough) |
| RAM | 12 GB |
| Disco | 400 GB VirtIO SCSI (SSD, iothread, discard) |
| Display | VirtIO-GPU (DRI/EGL per GNOME) |
| Rete | VirtIO bridge vmbr0 |
| Accesso | RustDesk :21118 + RDP :3389 + noVNC Proxmox |
| GPU | Nessuna dedicata (rendering software via VirtIO-GPU) |

---

## Troubleshooting

### RustDesk non si connette

```bash
# Verifica che il servizio sia attivo
sudo systemctl status rustdesk

# Verifica porta Direct IP
ss -tlnp | grep 21118

# Verifica ID e password
rustdesk --get-id
# Password impostata con: sudo rustdesk --password <password>

# Riavvia
sudo systemctl restart rustdesk

# Log
sudo journalctl -u rustdesk --since '5 min ago'
```

### GNOME Remote Desktop (RDP) — black screen da Mac

Questo e un **bug noto** di gnome-remote-desktop 46 su Ubuntu 24.04 con client Mac.
Non risolvibile cambiando versione Ubuntu (presente anche in 25.04).

**Soluzione:** usa RustDesk per l'accesso da Mac.

RDP funziona correttamente da **Remmina** (installato sull'host Proxmox XFCE).

```bash
# Verifica servizio RDP
sudo systemctl status gnome-remote-desktop

# Verifica porta
ss -tlnp | grep 3389

# Log
sudo journalctl -u gnome-remote-desktop --since '5 min ago'
```

### Chrome non si avvia

```bash
# Se mancano dipendenze
sudo apt install -y --fix-broken

# Se errore sandbox
google-chrome --no-sandbox

# Se errore shared memory (comune in VM)
# Il playbook Ansible già configura /dev/shm a 2 GB.
# Per verificare:
df -h /dev/shm
```

### sudo node/npm non trovato

I binari nvm sono nella home utente e `sudo` resetta il PATH.
Il playbook crea automaticamente i symlink in `/usr/local/bin/`.

```bash
# Fix manuale se necessario:
sudo ln -sf $(which node) /usr/local/bin/node
sudo ln -sf $(which npm) /usr/local/bin/npm
sudo ln -sf $(which npx) /usr/local/bin/npx
```

### Keyring locked dopo reboot (auto-login)

L'auto-login non passa la password a PAM, quindi gnome-keyring resta bloccato.
Non impatta RustDesk. Impatta solo `grdctl` (GNOME Remote Desktop credenziali).

```bash
# Per sbloccare via SSH:
echo -n 'LA_TUA_PASSWORD' | gnome-keyring-daemon --unlock --replace
# ATTENZIONE: questo rimpiazza il keyring daemon della sessione desktop.
# Fallo solo se necessario per configurare grdctl.
```

---

## Struttura finale su Proxmox

```
Host Proxmox (AtomMan G7 Pro)
+-- NVIDIA Driver (host) --- GPU condivisa
+-- XFCE + Remmina (schermo locale) --- KVM switch virtuale
|
+-- [LXC 100] LXC-JARVIS --- Ollama, Whisper, Orchestrator (GPU)
+-- [LXC 101] LXC-OpenClaw --- Gateway OpenClaw + Chrome headless (CDP)
+-- [LXC 210] LXC-Wakeword --- Wake word detection (CPU)
+-- [VM  200] VM-Workstation --- Ubuntu GNOME + Chrome reale + Zed
+-- [VM  xxx] VM-HAOS --- Home Assistant OS (opzionale)
+-- [LXC xxx] LXC-Alexa --- Alexa Media Server (opzionale)
```
