# VM Workstation — Ubuntu Desktop per JARVIS

VM desktop per sviluppo e controllo browser reale con OpenClaw.
Chrome (non headless) con l'estensione browser OpenClaw supera i controlli
anti-bot di Cloudflare e gestisce SPA dinamiche — impossibile con Chrome headless.

**Funzioni principali:**
- Chrome reale + estensione browser OpenClaw (controllo browser non-headless)
- IDE di sviluppo (Cursor)
- Git, Node.js (nvm), Python 3
- Email, browsing, workspace generale

**Accesso:**
- RDP (xrdp) da host Proxmox con XFCE + Remmina (schermo locale)
- RDP da qualsiasi PC in LAN
- noVNC dalla Proxmox Web UI (per installazione iniziale e troubleshooting)

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

## Step 2 — Crea la VM

### Opzione A: Terraform (consigliato)

```bash
cd infrastructure/terraform

# Se non l'hai ancora fatto:
cp terraform.tfvars.example terraform.tfvars
nano terraform.tfvars
```

Modifica `terraform.tfvars` — abilita solo la workstation (disabilita il resto se non ti serve):

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

Terraform crea la VM con disco VirtIO, display QXL, rete bridge, e la avvia
automaticamente dall'ISO.

### Opzione B: Manuale da Proxmox Web UI

1. **Create VM** (non CT!)
2. **General:**
   - Name: `jarvis-workstation`
   - VM ID: 200
3. **OS:**
   - ISO image: `ubuntu-24.04.2-desktop-amd64.iso`
   - Type: Linux, Version: 6.x - 2.6 Kernel
4. **System:**
   - Machine: q35
   - BIOS: SeaBIOS
   - SCSI Controller: VirtIO SCSI single
   - Qemu Agent: abilitato
5. **Disks:**
   - Bus: SCSI, Disco: 400 GB
   - Storage: local-lvm
   - Discard: abilitato, IO Thread: abilitato, SSD emulation: si
6. **CPU:**
   - Cores: 6
   - Type: host
7. **Memory:**
   - RAM: 12288 MB (12 GB)
8. **Network:**
   - Bridge: vmbr0
   - Model: VirtIO (paravirtualized)
9. **Confirm** e **Start**

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

---

## Step 4 — Setup automatico (Ansible) o manuale

### Opzione A: Ansible (consigliato — fa tutto in un comando)

Dopo aver installato Ubuntu e verificato che SSH funziona:

```bash
cd infrastructure/ansible
# Assicurati che inventory/hosts.yml abbia la sezione workstation con l'IP corretto
# Poi:
ansible-playbook playbooks/workstation.yml
```

Il playbook installa: qemu-guest-agent, XFCE, xrdp, Chrome, Git, nvm + Node.js,
Python 3, Cursor IDE, Tailscale, firewall. Fatto — salta al **Step 12** (desktop host).

### Opzione B: Manuale (step 4–11)

Entra nella VM (console noVNC o SSH dopo aver configurato la rete):

```bash
# Aggiorna tutto
sudo apt update && sudo apt upgrade -y

# Installa utility base
sudo apt install -y curl wget git nano htop jq ca-certificates gnupg \
  lsb-release build-essential software-properties-common unzip \
  apt-transport-https

# Installa QEMU Guest Agent (per Proxmox integration)
sudo apt install -y qemu-guest-agent
sudo systemctl enable --now qemu-guest-agent
```

---

## Step 5 — Desktop XFCE (leggero, se non usi Ubuntu default)

Se hai installato Ubuntu Desktop standard, hai gia GNOME.
Se preferisci XFCE (piu leggero, ~300 MB RAM in meno):

```bash
sudo apt install -y xfce4 xfce4-goodies
# Al login, seleziona "Xfce Session" dal menu sessione
```

> **Nota:** Puoi tenere sia GNOME che XFCE e scegliere al login.
> XFCE consuma ~1 GB RAM vs ~1.5 GB di GNOME — su 12 GB totali non è critico ma comunque meglio.

---

## Step 6 — xrdp (accesso RDP)

```bash
# Installa xrdp
sudo apt install -y xrdp

# Abilita e avvia
sudo systemctl enable --now xrdp

# Aggiungi utente al gruppo ssl-cert
sudo adduser jarvis ssl-cert

# Configura xrdp per usare XFCE (se installato)
echo "xfce4-session" > ~/.xsession
chmod +x ~/.xsession

# Riavvia xrdp
sudo systemctl restart xrdp

# Verifica
sudo systemctl status xrdp
```

Ora puoi connetterti via RDP da:
- **Host Proxmox** (con Remmina): `192.168.1.60:3389`
- **Qualsiasi PC in LAN**: client RDP → `192.168.1.60`

---

## Step 7 — Google Chrome

```bash
# Scarica e installa Chrome
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y ./google-chrome-stable_current_amd64.deb
rm google-chrome-stable_current_amd64.deb

# Verifica
google-chrome --version
```

### Estensione OpenClaw Browser

1. Apri Chrome nella VM
2. Vai al Chrome Web Store (o installa da file .crx se distribuzione interna)
3. Cerca e installa l'estensione OpenClaw per il controllo browser
4. Configura l'estensione con l'URL del gateway OpenClaw:
   - Se sulla stessa LAN: `http://192.168.1.51:18789`
   - Se via Tailscale: `http://jarvis-openclaw:18789`

> **Nota:** Il browser reale con profilo persistente supera i controlli anti-bot
> di Cloudflare. Per costruire un profilo pulito, naviga manualmente per qualche
> giorno prima di attivare l'automazione OpenClaw. Risolvi i CAPTCHA
> manualmente quando compaiono — il profilo imparera e ne vedrai sempre meno.

---

## Step 8 — Git

```bash
# Git è gia installato (step 4), configura identità
git config --global user.name "Il Tuo Nome"
git config --global user.email "tua@email.com"
git config --global init.defaultBranch main

# Genera chiave SSH per GitHub/GitLab
ssh-keygen -t ed25519 -C "jarvis-workstation"
cat ~/.ssh/id_ed25519.pub
# Copia la chiave pubblica nelle impostazioni SSH di GitHub/GitLab
```

---

## Step 9 — Node.js (nvm)

```bash
# Installa nvm
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash

# Ricarica il profilo
source ~/.bashrc

# Installa Node.js LTS
nvm install --lts
nvm alias default lts/*

# Verifica
node --version    # v22.x
npm --version
```

---

## Step 10 — Python 3

```bash
# Python 3 è preinstallato su Ubuntu 24.04, installa pip e venv
sudo apt install -y python3-pip python3-venv python3-dev

# Verifica
python3 --version   # 3.12.x
pip3 --version

# (Opzionale) pyenv per gestire più versioni Python
curl https://pyenv.run | bash
# Aggiungi a ~/.bashrc:
echo 'export PYENV_ROOT="$HOME/.pyenv"' >> ~/.bashrc
echo '[[ -d $PYENV_ROOT/bin ]] && export PATH="$PYENV_ROOT/bin:$PATH"' >> ~/.bashrc
echo 'eval "$(pyenv init -)"' >> ~/.bashrc
source ~/.bashrc
```

---

## Step 11 — Cursor IDE

```bash
# Scarica Cursor AppImage
curl -fL "https://www.cursor.com/api/download?platform=linux-x64&releaseTrack=stable" \
  -o ~/cursor.appimage
chmod +x ~/cursor.appimage

# Sposta in una directory dedicata
mkdir -p ~/.local/bin
mv ~/cursor.appimage ~/.local/bin/cursor

# Crea desktop entry (per launcher XFCE/GNOME)
mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/cursor.desktop << 'EOF'
[Desktop Entry]
Name=Cursor
Exec=/home/jarvis/.local/bin/cursor --no-sandbox %U
Icon=cursor
Type=Application
Categories=Development;IDE;
StartupWMClass=Cursor
EOF

# Installa dipendenza FUSE per AppImage
sudo apt install -y libfuse2t64

# Lancia Cursor
~/.local/bin/cursor --no-sandbox &
```

> **Nota:** `--no-sandbox` è necessario dentro una VM. Cursor funziona come
> VS Code con AI integrata. Le estensioni VS Code sono compatibili.

---

## Step 12 — Desktop sull'host Proxmox (KVM switch virtuale)

Per controllare la VM Workstation (e altre VM in futuro) dallo schermo
fisico collegato all'AtomMan G7 Pro, installa un desktop leggero sull'host Proxmox:

```bash
# SSH nell'host Proxmox (o dalla console locale)

# Installa XFCE leggero + Remmina (client RDP)
apt update
apt install -y xfce4 xfce4-terminal lightdm remmina remmina-plugin-rdp

# LightDM si avvia automaticamente — lo schermo locale mostra il login
# Username: root (o l'utente Proxmox)

# Dopo il login XFCE:
# 1. Apri Remmina
# 2. Crea connessione RDP: 192.168.1.60 (IP VM Workstation)
# 3. Salva e connetti — full screen con F11
#
# Per aggiungere altre VM in futuro:
# - Nuova connessione Remmina → IP della nuova VM
# - Switcha tra VM con le tab di Remmina o Alt+Tab
```

### Accesso rapido alla console Proxmox

Dallo stesso desktop XFCE sull'host, apri il browser:
```
https://localhost:8006
```

Hai la Web UI Proxmox direttamente sullo schermo locale — gestisci VM, backup,
storage, rete, tutto senza un altro PC.

---

## Step 13 — Tailscale (opzionale)

Se vuoi raggiungere la workstation da fuori LAN:

```bash
# Nella VM Workstation
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname=jarvis-workstation
```

---

## Step 14 — Firewall (opzionale)

```bash
sudo ufw allow ssh
sudo ufw allow 3389/tcp   # RDP
sudo ufw enable
```

---

## Riepilogo risorse

| Componente | Risorsa |
|------------|---------|
| CPU | 6 core (host passthrough) |
| RAM | 12 GB |
| Disco | 400 GB VirtIO SCSI (SSD, iothread, discard) |
| Display | QXL (buona qualita con noVNC e SPICE) |
| Rete | VirtIO bridge vmbr0 |
| Accesso | RDP :3389 + noVNC Proxmox |
| GPU | Nessuna dedicata (rendering software) |

---

## Troubleshooting

### xrdp non si connette

```bash
# Verifica che xrdp sia attivo
sudo systemctl status xrdp

# Verifica porta
ss -tlnp | grep 3389

# Riavvia
sudo systemctl restart xrdp

# Log
sudo journalctl -u xrdp --since '5 min ago'
```

### Schermo nero dopo login RDP

```bash
# Problema comune con GNOME — configura XFCE come sessione:
echo "xfce4-session" > ~/.xsession
chmod +x ~/.xsession

# Disabilita il screen locker di GNOME (interferisce con xrdp)
sudo apt remove -y gnome-screensaver
sudo systemctl restart xrdp
```

### Chrome non si avvia

```bash
# Se mancano dipendenze
sudo apt install -y --fix-broken

# Se errore sandbox
google-chrome --no-sandbox

# Se errore shared memory (comune in VM)
# Aumenta /dev/shm:
echo "tmpfs /dev/shm tmpfs defaults,size=2g 0 0" | sudo tee -a /etc/fstab
sudo mount -o remount /dev/shm
```

### Cursor AppImage non parte

```bash
# Verifica FUSE
ls /dev/fuse
# Se non esiste:
sudo modprobe fuse
sudo apt install -y libfuse2t64

# Altrimenti estrai manualmente
cd ~/.local/bin
./cursor --appimage-extract
# Usa squashfs-root/cursor al posto dell'appimage
```

### Performance lenta via RDP

```bash
# Usa XFCE invece di GNOME (molto piu leggero)
sudo apt install -y xfce4
echo "xfce4-session" > ~/.xsession
sudo systemctl restart xrdp

# Disabilita compositing in XFCE:
# Settings > Window Manager Tweaks > Compositor > deseleziona "Enable display compositing"

# In Remmina, abbassa la qualità colore a 16 bit se serve
```

---

## Struttura finale su Proxmox

```
Host Proxmox (AtomMan G7 Pro)
├── NVIDIA Driver (host) ─── GPU condivisa
├── XFCE + Remmina (schermo locale) ─── KVM switch virtuale
│
├── [LXC 100] LXC-JARVIS ─── Ollama, Whisper, Orchestrator (GPU)
├── [LXC 101] LXC-OpenClaw ─── Gateway OpenClaw + Chrome headless (CDP)
├── [LXC 210] LXC-Wakeword ─── Wake word detection (CPU)
├── [VM  200] VM-Workstation ─── Ubuntu XFCE + Chrome reale + Cursor
├── [VM  xxx] VM-HAOS ─── Home Assistant OS (opzionale)
└── [LXC xxx] LXC-Alexa ─── Alexa Media Server (opzionale)
```
