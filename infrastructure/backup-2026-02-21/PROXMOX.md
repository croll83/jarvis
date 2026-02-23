# Infrastruttura Proxmox — Guida Installazione

Creazione del container LXC su Proxmox VE per ospitare lo stack JARVIS.

> **DEPLOY CLOUD**: Salta questo file, vai direttamente a [DOCKER.md](DOCKER.md).
> Se usi un VPS (Hetzner, Contabo, ecc.), non hai bisogno di Proxmox. Passa all'installazione di Docker.

> **AUTOMAZIONE**: Questa guida descrive la procedura manuale. Se preferisci,
> puoi automatizzare tutto con Terraform + Ansible: vedi `infrastructure/terraform/` e `infrastructure/ansible/`.

---

## Prerequisiti

| Requisito | Minimo | Consigliato |
|-----------|--------|-------------|
| Proxmox VE | 7.4+ | 8.x |
| CPU host | 4 core | 8+ core |
| RAM host | 16 GB | 32+ GB |
| Disco | 100 GB SSD | 500 GB NVMe |
| GPU (opzionale) | NVIDIA con 8 GB VRAM | RTX 4060/5070 16 GB |

---

## Scelta: LXC con GPU vs LXC senza GPU

| Caratteristica | LXC con GPU | LXC senza GPU |
|----------------|-------------|---------------|
| GPU accessibile | Si (condivisa dall'host) | No |
| Overhead | Minimo (kernel host condiviso) | Minimo |
| Driver NVIDIA | Installato sull'HOST, non nel container | Non necessario |
| Isolamento | Condiviso col kernel host | Condiviso col kernel host |
| Complessita setup | Media (device binding + cgroup2) | Bassa |
| Caso d'uso | Deploy locale con Ollama/Whisper su GPU | Modalita API cloud |

**Regola pratica:**
- Hai una GPU NVIDIA sullo stesso host Proxmox? --> **LXC con GPU** (condivisione device dall'host)
- Non hai GPU o usi API cloud? --> **LXC senza GPU** (piu semplice)

> **Perche LXC e non VM?** L'LXC gira sullo stesso kernel dell'host Proxmox.
> La GPU non richiede PCIe passthrough (come in una VM), ma viene semplicemente
> resa disponibile nel container via device binding. Questo significa:
> - Zero overhead di virtualizzazione
> - GPU condivisa (non dedicata esclusivamente)
> - Setup piu semplice (no IOMMU, no vfio-pci, no UEFI)
> - Performance native

---

## Opzione A — LXC con GPU NVIDIA

### A.1 Installare il driver NVIDIA sull'host Proxmox

> **IMPORTANTE**: Il driver NVIDIA va installato sull'HOST Proxmox, NON nel container LXC.
> L'LXC condivide il kernel dell'host, quindi ha accesso diretto ai device GPU.

```bash
# SSH nell'host Proxmox
ssh root@<proxmox-ip>

# Disabilita i repo enterprise (aggiungi Enabled: no)
echo -e "\nEnabled: no" >> /etc/apt/sources.list.d/pve-enterprise.sources
echo -e "\nEnabled: no" >> /etc/apt/sources.list.d/ceph.sources

# Crea il repo pve no-subscription
cat > /etc/apt/sources.list.d/pve-no-subscription.sources <<EOF
Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: trixie
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF

# Crea il repo ceph no-subscription
cat > /etc/apt/sources.list.d/ceph-no-subscription.sources <<EOF
Types: deb
URIs: http://download.proxmox.com/debian/ceph-squid
Suites: trixie
Components: no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF

# Trova e patcha il file JS della web UI
sed -Ezi.bak "s/(Ext\.Msg\.show\(\{\s+title: gettext\('No valid sub)/void\(\{ \/\/\1/g" /usr/share/javascript/proxmox-widget-toolkit/proxmoxlib.js

# Riavvia il servizio web
systemctl restart pveproxy.service

# Aggiorna il sistema
apt update && apt upgrade -y

# Installa i kernel headers (necessari per il driver)
apt install -y pve-headers-$(uname -r)

# Aggiungi il repository contrib e non-free
# (Proxmox 8.x / Debian Bookworm)
sed -i 's/main/main contrib non-free non-free-firmware/g' /etc/apt/sources.list
apt update

# Installa il driver NVIDIA
apt install -y pve-headers build-essential pkg-config libglvnd-dev

# Scarica il driver 580 (ultimo disponibile)
cd /tmp
wget https://download.nvidia.com/XFree86/Linux-x86_64/580.126.09/NVIDIA-Linux-x86_64-580.126.09.run

# Rendi eseguibile e installa
chmod +x NVIDIA-Linux-x86_64-580.126.09.run
./NVIDIA-Linux-x86_64-580.126.09.run --no-questions --ui=none --disable-nouveau

# Se ti chiede di fare blacklist di nouveau, accetta.

# Dopo l'installazione: Verifica
nvidia-smi

# Riavvia l'host
reboot
```

Dopo il reboot, verifica il driver:

```bash
nvidia-smi
```

Deve mostrare la GPU con nome, temperatura, VRAM e driver version.

# Abilita persistence mode
nvidia-smi -pm 1

# Crea un servizio per i device nodes (mancano /dev/nvidia*)
cat > /etc/systemd/system/nvidia-dev.service <<EOF
[Unit]
Description=Create NVIDIA device nodes
After=nvidia-persistenced.service

[Service]
Type=oneshot
ExecStart=/usr/bin/nvidia-smi -L
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl enable nvidia-dev.service

cat > /etc/systemd/system/nvidia-persistenced.service <<EOF
[Unit]
Description=NVIDIA Persistence Daemon
Wants=syslog.target

[Service]
Type=forking
ExecStart=/usr/bin/nvidia-persistenced --user root
ExecStopPost=/bin/rm -rf /var/run/nvidia-persistenced

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now nvidia-persistenced
systemctl status nvidia-persistenced


### A.2 Identificare i device NVIDIA

```bash
# Elenca tutti i device NVIDIA
ls -la /dev/nvidia*
```

Output tipico:

```
crw-rw-rw- 1 root root 195,   0 ... /dev/nvidia0
crw-rw-rw- 1 root root 195, 255 ... /dev/nvidiactl
crw-rw-rw- 1 root root 195, 254 ... /dev/nvidia-modeset
crw-rw-rw- 1 root root 509,   0 ... /dev/nvidia-uvm
crw-rw-rw- 1 root root 509,   1 ... /dev/nvidia-uvm-tools
crw-rw-rw- 1 root root 234,   0 ... /dev/nvidia-caps/nvidia-cap1
crw-rw-rw- 1 root root 234,   1 ... /dev/nvidia-caps/nvidia-cap2
```
```
crw-rw-rw- 1 root root 195,   0 Feb 20 23:45 /dev/nvidia0
crw-rw-rw- 1 root root 195, 255 Feb 20 23:45 /dev/nvidiactl
crw-rw-rw- 1 root root 195, 254 Feb 20 23:48 /dev/nvidia-modeset
crw-rw-rw- 1 root root 507,   0 Feb 20 23:45 /dev/nvidia-uvm
crw-rw-rw- 1 root root 507,   1 Feb 20 23:45 /dev/nvidia-uvm-tools
cr--------  1 root root 510, 1 Feb 20 23:45 nvidia-cap1
cr--r--r--  1 root root 510, 2 Feb 20 23:45 nvidia-cap2
```

Prendi nota dei **major number** (195, 509, 234): servono per la configurazione cgroup.

### A.3 Creare il container LXC

Dalla Web UI di Proxmox (`https://<proxmox-ip>:8006`):

1. **Create CT**
2. **General:**
   - Hostname: `jarvis`
   - Password: scegli una password
   - CT ID: a scelta (es. 100)
3. **Template:**
   - Storage: local
   - Template: `ubuntu-22.04-standard` o `ubuntu-24.04-standard`
   - (Scarica il template da Proxmox se non presente)
4. **Disks:**
   - Root Disk: 100 GB minimo (200 GB consigliato per modelli AI)
5. **CPU:**
   - Cores: 4 minimo (6-8 consigliato)
6. **Memory:**
   - RAM: 16384 MB (16 GB)
   - Swap: 2048 MB
7. **Network:**
   - Bridge: `vmbr0`
   - IPv4: DHCP o statico

**NON avviare ancora il container!** Devi prima configurare i device GPU.

### A.4 Configurare l'accesso GPU nel container

Edita la configurazione del container sull'host Proxmox:

```bash
nano /etc/pve/lxc/100.conf
```

Aggiungi queste righe in fondo (sostituisci i major number con quelli del tuo sistema):

```
# Docker support
features: keyctl=1,nesting=1

# Device NVIDIA - mount
lxc.mount.entry: /dev/nvidia0 dev/nvidia0 none bind,optional,create=file 0 0
lxc.mount.entry: /dev/nvidiactl dev/nvidiactl none bind,optional,create=file 0 0
lxc.mount.entry: /dev/nvidia-uvm dev/nvidia-uvm none bind,optional,create=file 0 0
lxc.mount.entry: /dev/nvidia-uvm-tools dev/nvidia-uvm-tools none bind,optional,create=file 0 0

# cgroup2 - permetti accesso ai device NVIDIA
# Major 195 = nvidia (nvidia0, nvidiactl, nvidia-modeset)
# Major 509 = nvidia-uvm (nvidia-uvm, nvidia-uvm-tools)
# Major 234 = nvidia-caps (nvidia-cap1, nvidia-cap2)
lxc.cgroup2.devices.allow: c 195:* rwm
lxc.cgroup2.devices.allow: c 509:* rwm
lxc.cgroup2.devices.allow: c 234:* rwm
```

> **NOTA**: Se hai piu di una GPU (es. `/dev/nvidia0`, `/dev/nvidia1`), aggiungi
> una riga `lxc.mount.entry` per ogni device.

> **NOTA**: I major number possono variare tra sistemi. Verifica sempre con
> `ls -la /dev/nvidia*` sull'host.

### A.5 Avviare e verificare

```bash
# Avvia il container
pct start 100

# Entra nel container
pct enter 100

# Verifica che i device NVIDIA siano visibili
ls -la /dev/nvidia*
```

I device devono essere presenti dentro il container. Se non li vedi, controlla la configurazione cgroup2 nel file `.conf`.

### A.6 Setup iniziale nel LXC con GPU

```bash
# Aggiorna
apt update && apt upgrade -y

# Installa utility base
apt install -y curl wget git nano htop jq ca-certificates gnupg lsb-release \
  python3-pip sqlite3 ffmpeg unzip

# Crea utente jarvis
adduser jarvis
usermod -aG sudo jarvis

# Sudo senza password (opzionale ma comodo per Ansible)
echo "jarvis ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/jarvis
chmod 440 /etc/sudoers.d/jarvis
```

> **Prossimo step**: Procedi con [DOCKER.md](DOCKER.md), che include la sezione
> NVIDIA Container Toolkit per rendere la GPU accessibile a Docker.

---

## Opzione B — LXC senza GPU

Per un container leggero quando non serve GPU (modalita API cloud).

### B.1 Creare il container LXC

Dalla Web UI di Proxmox:

1. **Create CT**
2. **General:**
   - Hostname: `jarvis`
   - Password: scegli una password
   - CT ID: a scelta (es. 101)
3. **Template:**
   - Storage: local
   - Template: `ubuntu-22.04-standard` o `ubuntu-24.04-standard`
   - (Scarica il template da Proxmox se non presente)
4. **Disks:**
   - Root Disk: 50 GB minimo
5. **CPU:**
   - Cores: 2 minimo (4 consigliato)
6. **Memory:**
   - RAM: 4096 MB (4 GB) minimo
   - Swap: 2048 MB
7. **Network:**
   - Bridge: `vmbr0`
   - IPv4: DHCP o statico

### B.2 Configurazione post-creazione

Abilita le feature necessarie per Docker in LXC:

```bash
# Sul nodo Proxmox, edita la config del container
nano /etc/pve/lxc/101.conf
```

Aggiungi queste righe per supporto Docker:

```
features: keyctl=1,nesting=1
```

Avvia il container:

```bash
pct start 101
pct enter 101
```

### B.3 Setup iniziale nel LXC

```bash
# Aggiorna
apt update && apt upgrade -y

# Installa utility base
apt install -y curl wget git nano htop

# Crea utente jarvis
adduser jarvis
usermod -aG sudo jarvis
```

---

## Configurazione Rete

### IP Statico (consigliato per server)

Configurabile dalla Web UI di Proxmox: Container > Network > Edit.

Oppure da CLI sull'host Proxmox:

```bash
# Imposta IP statico per il container 100
pct set 100 -net0 name=eth0,bridge=vmbr0,ip=192.168.1.50/24,gw=192.168.1.1
```

In alternativa, dentro il container (Ubuntu con Netplan):

```bash
sudo nano /etc/netplan/10-lxc.yaml
```

```yaml
network:
  version: 2
  ethernets:
    eth0:
      dhcp4: false
      addresses:
        - 192.168.1.50/24
      routes:
        - to: default
          via: 192.168.1.1
      nameservers:
        addresses:
          - 8.8.8.8
          - 1.1.1.1
```

```bash
sudo netplan apply
```

### Port Forwarding (se necessario)

Se JARVIS deve essere raggiungibile dall'esterno (es. webhook Telegram), configura il port forwarding sul tuo router:

| Porta esterna | Porta interna | Servizio |
|--------------|--------------|----------|
| 443 | 5000 | Orchestrator (via reverse proxy) |

In alternativa, usa **Tailscale** per accesso sicuro senza port forwarding.

---

## Risorse Consigliate

### Deploy Locale con GPU (LXC)

| Risorsa | Minimo | Consigliato |
|---------|--------|-------------|
| vCPU | 4 | 6-8 |
| RAM | 16 GB | 24-32 GB |
| Disco | 100 GB SSD | 200 GB NVMe |
| GPU VRAM (host) | 8 GB | 12-16 GB |

### Deploy senza GPU (LXC)

| Risorsa | Minimo | Consigliato |
|---------|--------|-------------|
| vCPU | 2 | 4 |
| RAM | 4 GB | 8 GB |
| Disco | 50 GB | 100 GB |

---

## Setup SSH

### Dal container LXC

```bash
# Verifica che SSH sia attivo
sudo systemctl status sshd

# Se non installato
sudo apt install -y openssh-server
sudo systemctl enable --now sshd
```

### Dal tuo PC (accesso senza password)

```bash
# Genera chiave SSH (se non ne hai una)
ssh-keygen -t ed25519 -C "jarvis-admin"

# Copia la chiave sul container
ssh-copy-id jarvis@192.168.1.50

# Testa l'accesso
ssh jarvis@192.168.1.50
```

### Hardening SSH (opzionale ma consigliato)

```bash
sudo nano /etc/ssh/sshd_config
```

Modifica:

```
PermitRootLogin no
PasswordAuthentication no
PubkeyAuthentication yes
```

```bash
sudo systemctl restart sshd
```

---

## Verifica Installazione

```bash
# Dal container LXC, verifica il sistema
uname -a              # Kernel Linux
free -h               # RAM disponibile
df -h                 # Spazio disco
ip a                  # IP assegnato

# Solo per LXC con GPU
ls -la /dev/nvidia*   # Device GPU visibili nel container
```

---

## Desktop locale sull'host Proxmox (KVM switch virtuale)

Per usare lo schermo, tastiera e mouse fisici collegati all'AtomMan per controllare
le VM (Workstation, HAOS, ecc.), installa un desktop leggero **direttamente sull'host Proxmox**.
Questo ti permette di switchare tra VM come un KVM switch virtuale, senza un PC esterno.

```bash
# Dalla console locale dell'host Proxmox (o via SSH)

# Installa XFCE leggero + Remmina (client RDP multi-tab)
apt update
apt install -y xfce4 xfce4-terminal lightdm remmina remmina-plugin-rdp

# LightDM si avvia automaticamente — lo schermo locale mostra il login
# Username: root (o l'utente Proxmox)
```

### Dopo il login XFCE sull'host

1. Apri **Remmina**
2. Crea una nuova connessione RDP:
   - Protocollo: RDP
   - Server: `192.168.1.60` (IP della VM Workstation)
   - Username/password: quelli della VM
3. Salva e connetti — **F11** per full screen
4. Per aggiungere altre VM in futuro: nuova connessione Remmina → IP della VM
5. Switcha tra VM con le **tab di Remmina** o Alt+Tab
6. Apri il browser e vai a `https://localhost:8006` per la **Proxmox Web UI** sullo schermo locale

### Accesso rapido alla console Proxmox

Dallo stesso desktop XFCE sull'host, puoi gestire tutto:
- **Remmina**: RDP verso VM Workstation, HAOS, altre VM
- **Browser**: Proxmox Web UI (`https://localhost:8006`) per gestire VM, backup, storage, rete
- **Terminale**: accesso diretto all'host per comandi `pct`, `qm`, `nvidia-smi`, ecc.

> **Note:**
> - Va fatto una sola volta — il desktop persiste tra i reboot.
> - XFCE sull'host consuma ~200-300 MB RAM — trascurabile su 64 GB.
> - Le VM devono avere **xrdp** installato per essere raggiungibili via RDP.
>   Per la VM Workstation vedi [WORKSTATION.md](WORKSTATION.md) Step 6.
> - Se non ti serve il desktop locale (es. gestisci tutto via SSH/Web UI da un altro PC),
>   puoi saltare questo step.

---

## Prossimo Step

Procedi con l'installazione di Docker: **[DOCKER.md](DOCKER.md)**
