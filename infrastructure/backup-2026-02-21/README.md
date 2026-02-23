# JARVIS — Deploy Locale (GPU + OpenClaw su LXC/VM separata)

Guida completa per il deploy locale di JARVIS su Proxmox con GPU NVIDIA.
I modelli locali (Qwen 7B router, Whisper STT) girano on-premise su un **LXC con
GPU device sharing** (driver NVIDIA installato sull'host Proxmox, GPU condivisa via
cgroup2 — NON PCIe passthrough esclusivo).
Il reasoning e gestito da Gemini 3 Pro via OpenClaw che gira **bare-metal su un
LXC/VM dedicato e separato** per isolamento di sicurezza.
Tailscale gira host-level (non in Docker) su tutti i container/VM per raggiungere
HA remoti e il LXC OpenClaw.

> **NOTA IMPORTANTE — GPU Device Sharing vs Passthrough:**
> La GPU **non** e assegnata in esclusiva a nessuna VM/LXC tramite PCIe passthrough.
> Il driver NVIDIA gira sull'**host Proxmox** e la GPU e condivisa con l'LXC jarvis
> tramite device binding (`/dev/nvidia*` + cgroup2 allow). Questo significa:
> - Zero overhead di virtualizzazione GPU
> - La GPU resta disponibile anche per l'host (e potenzialmente altri container)
> - Non servono IOMMU, vfio-pci, o UEFI
> - Setup piu semplice e performance native

---

## Architettura Hardware

```
+=====================================================================+
|  HARDWARE: Proxmox Host (es. AtomMan G7 Pro)                        |
|  CPU: 32 vCPU (Intel Core i9) | RAM: 64 GB | GPU: RTX 5070 Laptop 8GB|
|  Disco: 2 TB NVMe                                                   |
|  NVIDIA Driver: installato SULL'HOST (nvidia-smi funziona qui)       |
|  Schermo + tastiera + mouse collegati fisicamente                    |
+=====================================================================+
|                                                                      |
|  [1] LXC-JARVIS (Ubuntu 22.04) — GPU device sharing via cgroup2     |
|  Device /dev/nvidia* montati dall'host (NO PCIe passthrough)         |
|  NVIDIA Container Toolkit installato (no-cgroups=true per LXC)       |
|  Tailscale host-level (100.x.x.x) - jarvis-wagmi                   |
|                                                                      |
|  docker-compose.yml                                                  |
|  +----------------------------------------------------------------+ |
|  |                     jarvis_network                              | |
|  |                                                                 | |
|  |  ollama:11434   whisper:9000    postgres:5432                   | |
|  |  GPU: 4.7 GB    GPU: 0.4 GB    (side proj)                     | |
|  |  Qwen 7B Q4     faster-whisper                                 | |
|  |  nomic-embed    base            mongo:27017                     | |
|  |                                  (side proj)                    | |
|  |  orchestrator:5000 (network_mode: host)                        | |
|  |  FastAPI + Admin UI                                             | |
|  |  Speaker ID (Resemblyzer)                                       | |
|  |  SQLite + ChromaDB                                              | |
|  |                                                                 | |
|  |  ontology-server:8100 (127.0.0.1 only)                         | |
|  |  Knowledge Graph API — SQLite + ACL (X-Speaker-Id)             | |
|  +----------------------------------------------------------------+ |
|                                                                      |
|  GPU VRAM Budget:                                                    |
|  +-- Qwen 2.5 7B Q4_K_M .............. 4.4 GB                      |
|  +-- nomic-embed-text ................. 0.3 GB                      |
|  +-- faster-whisper base .............. 0.4 GB                      |
|  +-- TOTALE ........................... ~5.1 GB / 8 GB VRAM disp.  |
+---------------------------------------------------------------------+

+---------------------------------------------------------------------+
|  [2] LXC-OpenClaw (Ubuntu 22.04, NO Docker, NO GPU)                 |
|  Node.js bare-metal | systemd service                                |
|  Tailscale host-level - jarvis-openclaw                              |
|                                                                      |
|  openclaw gateway :18789                                             |
|  Chrome headless  :18800 (CDP, solo localhost)                       |
|  Gemini 3 Pro (API cloud)                                            |
|  Telegram bot integrato                                              |
|  Linuxbrew + skill dependencies                                      |
|                                                                      |
|  Skill (copiata):                                                    |
|  ~/.openclaw/workspace/skills/jarvis-orchestrator/                   |
|                                                                      |
|  Plugin:                                                             |
|  ~/.openclaw/extensions/browser-dom/ (DOM automation via CDP)        |
|                                                                      |
|  Raggiungibile via:                                                  |
|  - Tailscale MagicDNS: http://jarvis-openclaw:18789                  |
|  - LAN IP: http://192.168.x.x:18789                                 |
+---------------------------------------------------------------------+

+---------------------------------------------------------------------+
|  [3] LXC-Wakeword (1 per casa — stessa LAN degli AtomS3R)           |
|  CPU: 1 vCPU | RAM: 2 GB | Disco: 10 GB                            |
|  Docker: jarvis-wakeword-server :8200                                |
|  Tailscale host-level (100.x.x.x) — raggiungibile dall'orchestrator |
|                                                                      |
|  openWakeWord: modelli ~2MB, ~80ms/inference su CPU                  |
|  Opus decode + wake word detection per 4-5 device                    |
|  Multi-room cooldown (5s)                                            |
|                                                                      |
|  Rete: AtomS3R → LAN locale (:8200/ws/audio)                        |
|  Tailscale: orchestrator VPS → push config + trigger_listen          |
|  Relay: → LXC-JARVIS/VPS orchestrator via Tailscale (on-demand)     |
+---------------------------------------------------------------------+

+---------------------------------------------------------------------+
|  [4] VM-Workstation (Ubuntu + XFCE — vera VM KVM, NO GPU dedicata)   |
|  CPU: 6 core | RAM: 12 GB | Disco: 400 GB                           |
|  Display: VirtIO-GPU (QXL) | Accesso: RDP (xrdp) + noVNC Proxmox   |
|                                                                      |
|  Chrome (reale, non headless) + OpenClaw browser extension           |
|  Cursor IDE, Git, Node.js (nvm), Python 3                            |
|  Workspace di sviluppo, email, browsing                              |
|                                                                      |
|  Accesso da host Proxmox: desktop XFCE locale + Remmina (RDP)       |
|  Accesso da LAN: RDP diretto all'IP della VM                        |
|  Accesso da ovunque: noVNC da Proxmox Web UI                        |
|                                                                      |
|  Vedi: infrastructure/WORKSTATION.md per setup completo              |
+---------------------------------------------------------------------+

+---------------------------------------------------------------------+
|  [5] VM-HAOS (opzionale — Home Assistant OS, vera VM KVM)            |
|  CPU: 2 core | RAM: 8 GB | Disco: 64 GB                            |
|  Porta: :8123 (HA API)                                               |
|  Zigbee/Z-Wave dongle via USB passthrough                            |
|  Raggiungibile dall'orchestrator via LAN o Tailscale                 |
+---------------------------------------------------------------------+

+---------------------------------------------------------------------+
|  [6] LXC-Alexa (opzionale — Alexa Media Server)                     |
|  CPU: 1 core | RAM: 1 GB | Disco: 5 GB                             |
|  Fa funzionare gli Amazon Echo come speaker di JARVIS                |
|  Comunica con HAOS via integrazione alexa_media                      |
+---------------------------------------------------------------------+

  Topologia di rete:

  LXC-JARVIS <--- LAN / Tailscale ---> LXC-OpenClaw
      ^
      |  (Tailscale, on-demand relay + push config)
      |
  LXC-Wakeword (LAN + Tailscale) <--- WiFi --- AtomS3R devices

  LXC-JARVIS <--- LAN ---> VM-HAOS (:8123)
  LXC-JARVIS <--- Tailscale ---> HA remoti (Milano, ecc.)

  Host Proxmox --- schermo locale --- desktop XFCE + Remmina
      |
      +--- RDP locale -----> VM-Workstation (Chrome + IDE + dev)
      +--- RDP locale -----> altre VM (se servono)
      +--- Browser locale -> Proxmox Web UI (:8006)
```

### Ordine di boot

I container/VM sono indipendenti su Proxmox e si avviano in parallelo.

**LXC-OpenClaw** (boot autonomo):
```
systemd -> tailscaled.service -> openclaw-chrome.service (Chrome CDP :18800)
                              -> openclaw.service (Node.js, porta 18789)
```
`openclaw-chrome.service` ha `Before=openclaw.service`, quindi Chrome parte prima del gateway.

**LXC-JARVIS** (boot sequenziale):
```
1. tailscaled      -> host-level service, si connette alla tailnet
2. ollama          -> diventa healthy (modelli caricati)
3. whisper         -> started
4. orchestrator    -> aspetta ollama + whisper, poi parte (network_mode: host)
                      vede Tailscale direttamente, raggiunge OpenClaw via OPENCLAW_URL
```

**LXC-Wakeword** (boot autonomo, 1 per casa):
```
1. tailscaled      -> host-level service, si connette alla tailnet
2. docker          -> started
3. wakeword-server -> healthcheck :8200/health, si connette a VPS orchestrator via relay
```

### Cosa gira dove

| Servizio | Dove gira | Container/Processo | CPU | RAM | GPU/VRAM | Funzione |
|----------|-----------|-------------------|-----|-----|----------|----------|
| **NVIDIA Driver** | **Host Proxmox** | kernel module | - | - | - | Driver GPU, `nvidia-smi` funziona qui |
| **Tailscale** | LXC-JARVIS | host-level (`tailscaled.service`) | - | 64 MB | - | VPN mesh per HA remoti + OpenClaw |
| **Ollama** | LXC-JARVIS | `jarvis_ollama` (Docker, `--gpus all`) | - | - | 4.7 GB | Qwen 7B pre-routing + embeddings |
| **Whisper** | LXC-JARVIS | `jarvis_whisper` (Docker, `--gpus all`) | - | - | 0.4 GB | Speech-to-text (faster-whisper) |
| **Orchestrator** | LXC-JARVIS | `jarvis_core` (`network_mode: host`) | 1-2 | 2 GB | - | FastAPI, HA control, memory, security |
| **Ontology Server** | LXC-JARVIS | `jarvis_ontology` (Docker, 127.0.0.1:8100) | 0.5 | 256 MB | - | Knowledge Graph API + ACL |
| **PostgreSQL** | LXC-JARVIS | `jarvis_postgres` (Docker) | 0.5 | 512 MB | - | Database side projects |
| **MongoDB** | LXC-JARVIS | `jarvis_mongo` (Docker) | 0.5 | 512 MB | - | Database side projects |
| **OpenClaw** | LXC-OpenClaw (bare-metal) | `openclaw.service` (systemd) | 0.5 | 512 MB | - | Gemini 3 Pro brain (API cloud) |
| **Chrome Headless** | LXC-OpenClaw (bare-metal) | `openclaw-chrome.service` (systemd) | 0.5 | ≤1 GB | - | Browser automation via CDP :18800 |
| **Wakeword Server** | LXC-Wakeword (1/casa) | `jarvis_wakeword` (Docker) | 1 | 2 GB | - | openWakeWord detection + relay :8200 |
| **Tailscale** | LXC-Wakeword | host-level (`tailscaled`) | - | 64 MB | - | Raggiungibilita dall'orchestrator VPS |
| **Workstation** | VM-Workstation (opz.) | KVM VM (Ubuntu + XFCE) | 6 | 12 GB | - | Chrome reale + OpenClaw ext + IDE + dev |
| **HAOS** | VM-HAOS (opz.) | KVM VM | 2 | 8 GB | - | Home Assistant OS + MASS + add-ons |
| **Alexa Media** | LXC-Alexa (opz.) | Docker/nativo | 1 | 1 GB | - | Echo come speaker JARVIS |

---

## Requisiti

### Host Proxmox

| Componente | Minimo | Consigliato |
|------------|--------|-------------|
| CPU | 8 core x86_64 | 32 vCPU (es. Intel Core i9) |
| RAM | 32 GB | 64 GB |
| GPU | NVIDIA 8 GB VRAM | RTX 5070 Laptop 8 GB / RTX 5070 Ti 16 GB |
| Disco | 500 GB SSD | 2 TB NVMe |
| OS | Proxmox VE 8.x | latest |
| NVIDIA Driver | installato sull'host | latest |

> **NVIDIA Driver sull'HOST:** Il driver va installato su Proxmox, non nei container.
> L'LXC-JARVIS accede alla GPU tramite device binding (`/dev/nvidia*` + cgroup2).
> Vedi [PROXMOX.md](PROXMOX.md) per la procedura dettagliata.

### LXC-JARVIS (GPU)

| Componente | Minimo | Consigliato |
|------------|--------|-------------|
| CPU | 4 core | 8 core |
| RAM | 16 GB | 20 GB |
| Disco | 100 GB | 250 GB |
| OS | Ubuntu 22.04+ | - |
| Docker | 24.0+ | latest |
| Docker Compose | 2.20+ | latest |
| NVIDIA Container Toolkit | installato (`no-cgroups=true`) | latest |
| Tailscale | installato host-level | latest |

### LXC-OpenClaw

| Componente | Minimo | Consigliato |
|------------|--------|-------------|
| CPU | 1 core | 2 core |
| RAM | 1 GB | 2 GB |
| Disco | 10 GB | 20 GB |
| OS | Ubuntu 22.04+ / Debian 12+ | - |
| Node.js | 22+ | 22 LTS |
| Google Chrome | stable | latest |
| Linuxbrew | installato | latest |
| GPU | Non richiesta | - |
| Tailscale | installato host-level | latest |

> **Nota:** Chrome headless (per browser-dom) richiede ~512 MB extra di RAM rispetto al solo gateway.
> Node.js 22+ è necessario per il supporto nativo WebSocket usato dal plugin browser-dom.

### LXC-Wakeword (1 per casa)

| Componente | Minimo | Consigliato |
|------------|--------|-------------|
| CPU | 1 core | 1 core |
| RAM | 1 GB | 2 GB |
| Disco | 5 GB | 10 GB |
| OS | Ubuntu 22.04+ / Debian 12+ | - |
| Docker | 24.0+ | latest |
| Tailscale | installato host-level | latest |
| GPU | Non richiesta | - |

> **Nota:** Un singolo LXC gestisce 4-5 device AtomS3R. Il container deve essere sulla stessa LAN
> dei device (stessa rete WiFi). Tailscale serve per la raggiungibilita dall'orchestrator VPS
> (push config, trigger_listen). I device lo raggiungono via IP LAN.

---

## Quick Setup (Step-by-Step)

### Prerequisiti

- Proxmox host con NVIDIA driver installato (vedi [PROXMOX.md](PROXMOX.md))
- LXC container creato con accesso GPU via device binding (vedi [Terraform](terraform/) o manuale)
- LXC/VM dedicato per OpenClaw (senza GPU, anche leggero)
- Docker + Compose installati nel LXC-JARVIS (vedi [DOCKER.md](DOCKER.md))
- NVIDIA Container Toolkit installato nel LXC-JARVIS con `no-cgroups=true` (vedi [DOCKER.md](DOCKER.md) Step 5)
- Node.js 22+ installato nel LXC-OpenClaw
- API keys pronte: Gemini, e opzionalmente Groq/OpenRouter come fallback
- Tailscale installato host-level su tutti i container/VM

### STEP 1 — Infrastruttura (una tantum)

Se parti da zero su Proxmox:

```bash
# Opzione A: Terraform (automatizzato) — crea i container/VM scelti
cd infrastructure/terraform
cp terraform.tfvars.example terraform.tfvars
nano terraform.tfvars

# Scegli cosa creare:
#   jarvis_enabled      = true     # LXC-JARVIS (GPU + Ollama + Orchestrator)
#   openclaw_enabled    = true     # LXC-OpenClaw (Gateway Gemini)
#   workstation_enabled = false    # VM-Workstation (Ubuntu Desktop + Chrome)
#
# Per ora puoi abilitare solo la workstation:
#   jarvis_enabled      = false
#   openclaw_enabled    = false
#   workstation_enabled = true

terraform init && terraform apply

# Opzione B: Manuale
# Segui PROXMOX.md per installare driver NVIDIA sull'host e creare LXC con GPU device sharing
# Crea un secondo LXC per OpenClaw (senza GPU, 2 core, 4 GB RAM)
# Crea una VM per la Workstation (vedi WORKSTATION.md)
# Poi segui DOCKER.md per installare Docker + NVIDIA Container Toolkit nel LXC-JARVIS
```

### STEP 2 — Setup Tailscale (tutti i container/VM)

Tailscale gira host-level su tutti i container/VM (non in Docker).

```bash
# === LXC-JARVIS ===
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname=jarvis-wagmi

# === LXC-OpenClaw ===
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname=jarvis-openclaw
```

### STEP 2b — Deploy LXC-Wakeword (1 per casa, opzionale)

Se usi il wakeword-server (rilevamento wake word lato server invece che on-device):

```bash
# Opzione A: Script interattivo (consigliato — eseguire sul Proxmox HOST)
sudo bash cloud/scripts/deploy-wakeword.sh

# Opzione B: Terraform + Ansible (automatizzato)
cd infrastructure/terraform
# Aggiungi le istanze wakeword in terraform.tfvars:
# wakeword_instances = {
#   "casa1" = {
#     ct_id             = 210
#     ip_address        = "192.168.1.210/24"
#     hostname          = "jarvis-wakeword-casa1"
#     node_name         = "pve-casa1"
#     tailscale_authkey = "tskey-auth-xxxxx"
#   }
# }
terraform apply

# Poi con Ansible:
cd ../ansible
ansible-playbook playbooks/wakeword.yml -e "wakeword_host=192.168.1.210"
```

Lo script/playbook:
1. Crea un LXC container (1 core, 2 GB RAM, 10 GB disco)
2. Installa Docker + Tailscale nel container
3. Connette Tailscale alla tailnet (serve auth key)
4. Clona il repo (sparse checkout di `jarvis/wakeword-server/`)
5. Crea `.env` e avvia `docker compose up -d`
6. Verifica health su `:8200/health`

Dopo il deploy, aggiungi l'IP Tailscale del wakeword server al `.env` dell'orchestrator:

```bash
# Nel .env dell'orchestrator (LXC-JARVIS o VPS cloud)
WAKEWORD_SERVER_URLS={"tua_location_id": "http://<TAILSCALE_IP_WAKEWORD>:8200"}

# Poi riavvia l'orchestrator
docker compose restart orchestrator
```

### STEP 3 — Setup LXC-OpenClaw (una tantum)

Sul LXC/VM dedicato a OpenClaw:

```bash
# Installa Node.js 20 LTS
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -
sudo apt-get install -y nodejs

# Installa OpenClaw globalmente
npm install -g openclaw

# (Tailscale già installato nello STEP 2)

# Configura OpenClaw con onboard
openclaw onboard
# Inserisci: GEMINI_API_KEY, OPENCLAW_GATEWAY_TOKEN (stesso del .env sulla LXC-JARVIS)

# Copia la skill JARVIS nella directory OpenClaw
sudo mkdir -p /opt/jarvis/jarvis-orchestrator/skill
# (copia o clona i file della skill JARVIS in /opt/jarvis/jarvis-orchestrator/skill)
mkdir -p ~/.openclaw/workspace/skills/jarvis-orchestrator
cp /opt/jarvis/jarvis-orchestrator/skill/SKILL.md ~/.openclaw/workspace/skills/jarvis-orchestrator/
cp /opt/jarvis/jarvis-orchestrator/skill/skill.json ~/.openclaw/workspace/skills/jarvis-orchestrator/

# Crea il servizio systemd
sudo tee /etc/systemd/system/openclaw.service > /dev/null <<EOF
[Unit]
Description=OpenClaw Gateway
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User=$USER
ExecStart=$(which openclaw) gateway run
Restart=always
RestartSec=5
Environment=NODE_ENV=production

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now openclaw
```

#### STEP 3b — Browser-DOM Plugin (LXC-OpenClaw, opzionale)

Il plugin browser-dom aggiunge 8 tool DOM per automazione web (navigazione, click,
fill, screenshot via CSS selectors / XPath / text matching) parlando direttamente con
Chrome headless tramite CDP.

```bash
# Installa Chrome headless (se non presente)
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y ./google-chrome-stable_current_amd64.deb
rm google-chrome-stable_current_amd64.deb

# Copia il plugin
mkdir -p ~/.openclaw/extensions/browser-dom
cp -r /opt/jarvis/extensions/browser-dom/{src,index.ts,package.json,openclaw.plugin.json} \
    ~/.openclaw/extensions/browser-dom/
cd ~/.openclaw/extensions/browser-dom && npm install --omit=dev && cd -

# Crea directory Chrome user-data
mkdir -p ~/.openclaw/browser/openclaw/user-data

# Crea servizio Chrome headless (come root)
sudo cp /opt/jarvis/extensions/browser-dom/openclaw-chrome.service \
    /etc/systemd/system/openclaw-chrome.service
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-chrome

# Verifica CDP
curl -s http://127.0.0.1:18800/json/version

# Configura il plugin in openclaw.json (dopo onboarding)
bash /opt/jarvis/cloud/scripts/configure-browser-dom.sh

# Riavvia OpenClaw per caricare il plugin
sudo systemctl restart openclaw

# Verifica che il plugin sia caricato
journalctl -u openclaw --since '1 min ago' | grep browser-dom
```

### STEP 4 — Clone repository (LXC-JARVIS)

```bash
git clone https://github.com/croll83/jarvis.git
cd jarvis
```

### STEP 5 — Configura .env (LXC-JARVIS)

```bash
cp .env.example .env
nano .env
```

Variabili obbligatorie:

| Variabile | Come ottenerla |
|-----------|----------------|
| `AI_BACKEND` | `local` (usa Ollama + Whisper locali) |
| `GEMINI_API_KEY` | [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey) |
| `OPENCLAW_GATEWAY_TOKEN` | `openssl rand -hex 32` — deve essere lo stesso usato in `openclaw onboard` sulla LXC-OpenClaw |
| `OPENCLAW_URL` | `http://jarvis-openclaw:18789` (Tailscale MagicDNS) o `http://192.168.x.x:18789` (LAN) |
| `OPENCLAW_TELEGRAM_BOT_TOKEN` | @BotFather su Telegram |
| `JARVIS_APPROVAL_BOT_TOKEN` | @BotFather (secondo bot, separato) |
| `JARVIS_APPROVAL_CHAT_ID` | Scrivi al bot, poi `curl https://api.telegram.org/bot<TOKEN>/getUpdates` |
| `HASS_URL` | `http://homeassistant:8123` (locale) o `http://100.x.x.x:8123` (via Tailscale) |
| `JARVIS_HASS_TOKEN` | HA -> Profilo -> Token di lunga durata |
| `POSTGRES_PASSWORD` | Password forte a scelta |
| `MONGO_PASSWORD` | Password forte a scelta |

Variabili opzionali (API cloud come fallback):

```env
GROQ_API_KEY=gsk_...          # STT via Groq (fallback se Whisper locale down)
OPENROUTER_API_KEY=sk-or-...  # Routing via OpenRouter (fallback se Ollama down)
```

### STEP 6 — Configura system prompt (LXC-JARVIS)

```bash
nano config/router_system_prompt.txt   # Regole di routing per Qwen
```

### STEP 7 — Avvia lo stack (LXC-JARVIS)

```bash
docker compose up -d
```

### STEP 8 — Scarica modelli Ollama (LXC-JARVIS)

```bash
# Attendi che Ollama sia pronto, poi:
bash setup.sh
```

Lo script scarica:
1. **Qwen 2.5 7B Instruct Q4_K_M** (~4.7 GB) - router/pre-router
2. **nomic-embed-text** (~274 MB) - embeddings per memoria semantica
3. Esegue warmup dei modelli

### STEP 9 — Verifica

```bash
# === LXC-OpenClaw ===

# OpenClaw attivo?
sudo systemctl status openclaw

# OpenClaw healthy?
curl http://localhost:18789/health

# Tailscale connesso?
tailscale status

# === LXC-JARVIS ===

# Tailscale connesso?
tailscale status

# Orchestrator healthy?
curl http://localhost:5000/health

# OpenClaw raggiungibile dalla LXC-JARVIS?
# (via Tailscale MagicDNS)
curl http://jarvis-openclaw:18789/health
# (oppure via LAN)
curl http://192.168.x.x:18789/health

# Ollama con modelli?
curl http://localhost:11434/api/tags

# GPU ok?
nvidia-smi

# HA raggiungibile?
curl -s \
  -H "Authorization: Bearer <HASS_TOKEN>" \
  http://<HA_IP>:8123/api/ | head -c 100

# Logs in tempo reale
docker compose logs -f orchestrator

# === LXC-Wakeword (se deployato) ===

# Health check
curl http://<WAKEWORD_LAN_IP>:8200/health

# Devices connessi
curl http://<WAKEWORD_LAN_IP>:8200/api/devices

# Tailscale connesso?
pct exec <CT_ID> -- tailscale status

# Raggiungibile dall'orchestrator via Tailscale?
tailscale ping jarvis-wakeword-casa1

# Logs
pct exec <CT_ID> -- docker logs -f jarvis_wakeword
```

### STEP 10 — Primo accesso alla dashboard

Apri `http://localhost:5000/admin` nel browser (dalla LXC-JARVIS o via Tailscale da qualsiasi dispositivo nella tailnet).

Da qui puoi:
- Creare utenti e assegnare ruoli (admin/user)
- Enrollare voci (speaker identification con Resemblyzer)
- Gestire location e entity maps
- Configurare preferenze globali
- Monitorare lo stato dei servizi

### STEP 11 — Dashboard OpenClaw

La dashboard di OpenClaw e accessibile direttamente dalla LXC-OpenClaw:
`http://jarvis-openclaw:18789` (via Tailscale MagicDNS da qualsiasi dispositivo nella tailnet)
oppure `http://192.168.x.x:18789` dalla LAN.

Da qui puoi gestire le skill registrate, vedere i log delle conversazioni e monitorare lo stato del gateway.

### STEP 12 — Telegram webhook

Il webhook Telegram e gestito da **OpenClaw** (non dall'orchestrator).
Configura il webhook del bot OpenClaw puntando al tuo dominio:

```bash
curl "https://api.telegram.org/bot<OPENCLAW_TELEGRAM_BOT_TOKEN>/setWebhook?url=https://<tuo-dominio>/telegram_webhook"
```

### STEP 13 — Desktop locale sull'host Proxmox (KVM switch virtuale)

Per usare lo schermo/tastiera/mouse fisici collegati all'AtomMan per controllare
le VM (Workstation, HAOS, ecc.), installa un desktop leggero **direttamente sull'host Proxmox**:

```bash
# SSH nell'host Proxmox (o dalla console locale)

# Installa XFCE leggero + Remmina (client RDP multi-tab)
apt update
apt install -y xfce4 xfce4-terminal lightdm remmina remmina-plugin-rdp

# LightDM si avvia automaticamente — lo schermo locale mostra il login
# Username: root (o l'utente Proxmox)
```

Dopo il login XFCE sull'host:

1. **Remmina** → Nuova connessione RDP → `192.168.1.60` (IP VM Workstation)
2. Salva e connetti — full screen con F11
3. Per aggiungere altre VM: nuova connessione Remmina → IP della VM
4. Switcha tra VM con le **tab di Remmina** o Alt+Tab
5. Apri `https://localhost:8006` nel browser per la **Proxmox Web UI** sullo schermo locale

> **Nota:** Questo step e manuale (non automatizzabile con Terraform/Ansible perche e l'host stesso).
> Va fatto una sola volta. XFCE sull'host consuma ~200-300 MB RAM — trascurabile su 64 GB.
> Vedi [PROXMOX.md — Desktop locale sull'host](PROXMOX.md#desktop-locale-sullhost-proxmox-kvm-switch-virtuale) per la guida completa.

---

## Deploy Automatizzato (Ansible)

Ansible configura il software su container/VM gia creati (da Terraform o manualmente).
Ogni componente ha il suo playbook — esegui solo quelli che ti servono.

```bash
cd infrastructure/ansible
cp inventory/hosts.yml.example inventory/hosts.yml
cp group_vars/all.yml.example group_vars/all.yml
nano group_vars/all.yml    # Tutte le variabili: deploy_type, API keys, etc.
nano inventory/hosts.yml   # IP dei container/VM target
```

**Esegui solo i componenti che hai abilitato in Terraform:**

```bash
# LXC-JARVIS (Ollama, Whisper, Orchestrator)
ansible-playbook playbooks/site.yml --tags common,nvidia,jarvis,verify

# LXC-OpenClaw (Node.js, Chrome headless, skill deps)
ansible-playbook playbooks/site.yml --tags openclaw

# VM-Workstation (Chrome reale, xrdp, nvm, Cursor)
# Prerequisito: Ubuntu installato manualmente da noVNC
ansible-playbook playbooks/workstation.yml

# LXC-Wakeword
ansible-playbook playbooks/wakeword.yml -e "wakeword_host=192.168.1.210"

# Tutto insieme (se hai abilitato tutto)
ansible-playbook playbooks/site.yml
ansible-playbook playbooks/workstation.yml
```

Il playbook `site.yml` esegue in sequenza:

```
common.yml      -> Sistema base, Docker, firewall, Tailscale (LXC-JARVIS)
nvidia.yml      -> NVIDIA Container Toolkit (LXC-JARVIS, solo lxc_gpu)
openclaw.yml    -> Node.js, Chrome headless, OpenClaw, Linuxbrew (LXC-OpenClaw)
jarvis.yml      -> Clone repo, .env, docker-compose up, pull modelli (LXC-JARVIS)
security.yml    -> Frigate + DoubleTake (opzionale)
verify.yml      -> Health check di tutti i servizi
```

Per il wakeword server (separato, eseguito sull'LXC wakeword):

```bash
ansible-playbook playbooks/wakeword.yml -e "wakeword_host=192.168.1.210"
```

```
wakeword.yml -> Docker, Tailscale, clone repo, .env, docker-compose up, health check
```

---

## Configurazione Avanzata

### Parametri LLM (runtime, da dashboard)

I parametri LLM sono in `global_preferences` nel database. Modificabili dalla dashboard admin senza riavvio:

| Chiave | Descrizione |
|--------|-------------|
| `llm_params` | Temperature, max_tokens, timeout per routing/reasoning/quick/summary/gemini |
| `default_location_id` | Location di default |
| `default_fallback_speaker` | Speaker entity_id di fallback |
| `security_announcement_speaker` | Speaker per annunci security |

Esempio modifica via API:
```bash
curl -X PUT http://localhost:5000/admin/preferences/llm_params \
  -H "Content-Type: application/json" \
  -d '{
    "routing": {"temperature": 0.1, "max_tokens": 500, "timeout": 10},
    "reasoning": {"temperature": 0.7, "max_tokens": 2000, "timeout": 120},
    "quick_response": {"temperature": 0.7, "max_tokens": 200, "timeout": 10},
    "gemini": {"temperature": 0.7, "max_tokens": 2000}
  }'
```

### Parametri restart-required (.env)

Questi richiedono riavvio del container orchestrator sulla LXC-JARVIS:

```env
# Timeouts (secondi)
OPENCLAW_TIMEOUT=30
TAILSCALE_TIMEOUT_REMOTE=15.0
TAILSCALE_TIMEOUT_LOCAL=10.0
TIMEOUT_WHISPER=30
TIMEOUT_HA_READ=5

# Intervalli (secondi)
INTERVAL_CLEANUP=1800
INTERVAL_HEALTH_CHECK=30
INTERVAL_PROACTIVE=300
INTERVAL_PROACTIVE_COOLDOWN=1800

# Context budget (token)
CTX_ROUTING_USER_SQL=300
CTX_ROUTING_USER_VECTOR=200
CTX_REASONING_USER_SQL=800
CTX_REASONING_USER_VECTOR=600

# Soglie
VECTOR_SCORE_MIN_MESSAGES=0.3
VECTOR_SCORE_MIN_FACTS=0.4
VOICE_SIMILARITY_THRESHOLD=0.75

# Memory jobs
MEMORY_HOURLY_MINUTE=5
MEMORY_DAILY_HOUR=3

# Whisper locale
WHISPER_MODEL=base
WHISPER_LANGUAGE=it

# Proactive monitoring
PROACTIVE_DOOR_OPEN_MINUTES=30
PROACTIVE_NO_MOTION_HOURS=12
PROACTIVE_NIGHT_START=2
PROACTIVE_NIGHT_END=5
```

### Prompt templates

I prompt di sistema sono in `jarvis-orchestrator/prompts/`. Modificabili senza toccare il codice:

```
prompts/
  quick_response_system.txt     # System prompt per chat semplice
  gemini_verification.txt       # Prompt per verifica Gemini
  user_hourly_summary.txt       # Prompt per summary orario
  user_daily_summary.txt        # Prompt per summary giornaliero
```

Per ricaricare senza riavvio:
```bash
curl -X POST http://localhost:5000/admin/prompts/reload
```

---

## Tailscale Multi-Location

Tailscale gira host-level su tutti i container/VM (LXC-JARVIS, LXC-OpenClaw, LXC-Wakeword), non come container Docker.
L'orchestrator usa `network_mode: host` e vede l'interfaccia Tailscale direttamente.
Permette all'orchestrator di raggiungere HA remoti e il LXC-OpenClaw senza aprire porte.

### Dove serve Tailscale

| Nodo | Dove gira | Ruolo | Hostname |
|------|-----------|-------|----------|
| **Napoli (Wagmi)** | Host-level nel LXC-JARVIS | Gateway VPN per lo stack | `jarvis-wagmi` |
| **LXC-OpenClaw** | Host-level nel LXC dedicato | Espone OpenClaw sulla tailnet | `jarvis-openclaw` |
| **LXC-Wakeword** | Host-level nel LXC wakeword | Orchestrator → push config, trigger_listen | `jarvis-wakeword-<casa>` |
| **Milano (Albani)** | Add-on HAOS o host-level | Espone HA sulla tailnet | `ha-albani` |

### Schema di rete

```
+---------------------------------------------------------------+
|                    TAILSCALE MESH (100.x.x.x)                  |
|                                                                 |
|   Napoli LXC-JARVIS                    LXC-OpenClaw (bare-metal)  |
|   +-------------------+           +-------------------+        |
|   | jarvis-wagmi      |<--------->| jarvis-openclaw   |        |
|   | Tailscale (host)  |           | Tailscale (host)  |        |
|   |                   |           |                   |        |
|   | Docker:           |           | OpenClaw Gateway  |        |
|   |   Ollama, Whisper |           | Gemini 3 Pro      |        |
|   |   Orchestrator    |           | Telegram bot      |        |
|   |    (net: host)    |           | JARVIS skill      |        |
|   |   Postgres, Mongo |           +-------------------+        |
|   |                   |                                        |
|   | HA Wagmi (locale) |                                        |
|   +-------------------+                                        |
|           |                                                     |
|           v                                                     |
|   Milano (Mini PC)                                             |
|   +-------------------+                                        |
|   | ha-albani         |                                        |
|   | Home Assistant    |                                        |
|   | Zigbee/Z-Wave     |                                        |
|   | Automazioni       |                                        |
|   +-------------------+                                        |
|                                                                 |
|   Napoli (LXC su stesso host Proxmox)                              |
|   +-------------------+                                        |
|   | jarvis-wakeword-  |  LAN :8200 ← AtomS3R devices          |
|   |  casa1            |  Tailscale ← orchestrator (config/     |
|   | Docker: wakeword  |              trigger_listen)            |
|   +-------------------+                                        |
|                                                                 |
|   wagmi -> openclaw: http://jarvis-openclaw:18789 (MagicDNS)  |
|   wagmi -> albani: 100.x.x.x:8123 (HA API via Tailscale)     |
|   wagmi -> wakeword: http://jarvis-wakeword-casa1:8200        |
|   Zero porte aperte, NAT traversal automatico                  |
+---------------------------------------------------------------+
```

### Come funziona nel codice

L'orchestrator rileva automaticamente se un URL HA e remoto:

```
URL contiene "100." o ".ts.net"?
  Si: timeout = 15s (TAILSCALE_TIMEOUT_REMOTE)
  No: timeout = 10s (TAILSCALE_TIMEOUT_LOCAL)
```

Le location HA (URL + token) sono nel database SQLite, gestibili dalla dashboard admin.

L'orchestrator raggiunge OpenClaw tramite la variabile `OPENCLAW_URL` (default: `http://jarvis-openclaw:18789` via Tailscale MagicDNS).
Poiche l'orchestrator usa `network_mode: host`, vede l'interfaccia Tailscale direttamente senza bisogno di un container dedicato.

---

## Porte di Rete

### LXC-JARVIS

| Porta Host | Servizio | Protocollo | Accesso |
|------------|----------|------------|---------|
| 5000 | Orchestrator + Admin UI | HTTP | LAN / Tailscale |
| 9000 | Whisper STT | HTTP | Interno |
| 11434 | Ollama API | HTTP | Interno |
| 5432 | PostgreSQL | TCP | Interno |
| 27017 | MongoDB | TCP | Interno |
| 41641/udp | Tailscale NAT traversal | UDP | WAN (host-level) |

### LXC-OpenClaw

| Porta | Servizio | Protocollo | Accesso |
|-------|----------|------------|---------|
| 18789 | OpenClaw Gateway + Dashboard | HTTP | LAN / Tailscale |
| 18800 | Chrome Headless (CDP) | HTTP/WS | Solo localhost (127.0.0.1) |

### LXC-Wakeword (per ogni casa)

| Porta | Servizio | Protocollo | Accesso |
|-------|----------|------------|---------|
| 8200 | Wakeword Server (HTTP + WS) | HTTP/WS | LAN (AtomS3R) + Tailscale (orchestrator) |
| 41641/udp | Tailscale NAT traversal | UDP | WAN (host-level) |

---

## Confronto Locale vs Cloud

```
                    LOCALE (GPU)                    CLOUD (VPS)
                    ────────────                    ───────────
Pre-routing:        Qwen 7B Q4 (Ollama, locale)     Qwen 7B (OpenRouter API)
STT:                faster-whisper (GPU, locale)     Groq Whisper (API cloud)
Brain:              Gemini 3 Pro (OpenClaw, VM sep.) Gemini 3 Pro (OpenClaw, API)
Embeddings:         nomic-embed-text (Ollama)        nomic-embed-text (solo locale)
HA control:         HTTP diretto / Tailscale         Tailscale (tutto remoto)
Speaker ID:         Resemblyzer (in orchestrator)    Resemblyzer (in orchestrator)

GPU richiesta:      Si (8GB+ VRAM, solo LXC-JARVIS)      No
Latenza voce:       ~200ms (locale)                  ~800ms (API round-trip)
Offline mode:       Parziale (Qwen locale)           No (tutto API)
Costo mensile:      ~0 (solo corrente)               ~4-8/mese VPS + API
```

---

## Monitoring

```bash
# === LXC-JARVIS ===

# Stato container
docker compose ps

# Tailscale status
tailscale status

# Logs orchestrator
docker compose logs -f orchestrator

# Health servizi interni
curl http://localhost:5000/health/services

# Risorse e GPU
docker stats
nvidia-smi

# OpenClaw raggiungibile?
curl http://jarvis-openclaw:18789/health

# Tailscale ping test
tailscale ping jarvis-openclaw

# === LXC-Wakeword (dal Proxmox host) ===

# Health check
curl http://<WAKEWORD_LAN_IP>:8200/health

# Devices connessi
curl http://<WAKEWORD_LAN_IP>:8200/api/devices

# Logs
pct exec <CT_ID> -- docker logs -f jarvis_wakeword

# Tailscale
pct exec <CT_ID> -- tailscale status

# === LXC-OpenClaw ===

# Stato servizi
sudo systemctl status openclaw-chrome openclaw

# Chrome CDP attivo?
curl -s http://127.0.0.1:18800/json/version

# Tailscale status
tailscale status

# Logs OpenClaw
sudo journalctl -u openclaw -f

# Logs Chrome headless
sudo journalctl -u openclaw-chrome -f

# Health
curl http://localhost:18789/health
```

---

## Troubleshooting

### GPU non rilevata (LXC-JARVIS)

```bash
nvidia-smi                           # Verifica driver NVIDIA
docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi
docker info | grep -i nvidia         # Runtime configurato?
```

### Ollama non risponde (LXC-JARVIS)

```bash
docker logs jarvis_ollama
curl http://localhost:11434/api/tags  # Deve rispondere con lista modelli
```

### Memoria VRAM insufficiente (LXC-JARVIS)

```bash
nvidia-smi   # Verifica utilizzo VRAM

# Se OOM, riduci modelli caricati contemporaneamente:
# In docker-compose.yml, cambia OLLAMA_MAX_LOADED_MODELS=1
```

### Tailscale non si connette (LXC-JARVIS)

```bash
tailscale status
sudo systemctl status tailscaled
sudo journalctl -u tailscaled --since '5 min ago'
# Se serve ri-autenticare:
sudo tailscale up --hostname=jarvis-wagmi
```

### OpenClaw non raggiungibile (dalla LXC-JARVIS)

```bash
# Verifica che OpenClaw sia attivo sulla sua VM
ssh user@jarvis-openclaw "sudo systemctl status openclaw"

# Test connettivita Tailscale
tailscale ping jarvis-openclaw

# Test diretto via LAN (se sulla stessa rete)
curl http://192.168.x.x:18789/health

# Test via Tailscale MagicDNS
curl http://jarvis-openclaw:18789/health

# Logs OpenClaw sulla VM dedicata
ssh user@jarvis-openclaw "sudo journalctl -u openclaw --since '5 min ago'"
```

### OpenClaw non parte (LXC-OpenClaw)

```bash
# Controlla lo stato del servizio
sudo systemctl status openclaw

# Logs dettagliati
sudo journalctl -u openclaw -e

# Verifica che Node.js sia installato
node --version

# Verifica che openclaw sia installato
which openclaw
openclaw --version

# Riavvia il servizio
sudo systemctl restart openclaw
```

### Chrome headless non parte (LXC-OpenClaw)

```bash
# Stato del servizio
sudo systemctl status openclaw-chrome
sudo journalctl -u openclaw-chrome --since '5 min ago'

# Verifica Chrome installato
google-chrome --version

# CDP risponde?
curl -s http://127.0.0.1:18800/json/version

# Stale singleton files? (Chrome crashato in precedenza)
ls -la ~/.openclaw/browser/openclaw/user-data/Singleton*
# Il servizio li pulisce automaticamente all'avvio (ExecStartPre)

# Riavvia il servizio
sudo systemctl restart openclaw-chrome
```

### browser-dom plugin non carica (LXC-OpenClaw)

```bash
# Verifica che il plugin sia presente
ls -la ~/.openclaw/extensions/browser-dom/

# Controlla i log OpenClaw per errori di caricamento
journalctl -u openclaw --since '5 min ago' | grep -i 'browser-dom\|plugin'

# Verifica che openclaw.json abbia il plugin configurato
jq '.plugins["browser-dom"]' ~/.openclaw/openclaw.json

# Test manuale CDP
curl -s http://127.0.0.1:18800/json/list   # Deve mostrare tab aperte

# Riavvia entrambi i servizi
sudo systemctl restart openclaw-chrome
sudo systemctl restart openclaw
```

### Wakeword server non risponde (LXC-Wakeword)

```bash
# Container LXC avviato?
pct status <CT_ID>

# Docker container attivo?
pct exec <CT_ID> -- docker ps

# Health check
pct exec <CT_ID> -- curl -sf http://localhost:8200/health

# Logs wakeword
pct exec <CT_ID> -- docker logs --tail=50 jarvis_wakeword

# Tailscale connesso?
pct exec <CT_ID> -- tailscale status

# Riavvia il container Docker
pct exec <CT_ID> -- docker compose -f /opt/jarvis-wakeword/wakeword-server/docker-compose.yml restart

# Redeploy completo (se serve)
sudo bash cloud/scripts/deploy-wakeword.sh
```

### Orchestrator non raggiunge il wakeword server

```bash
# Verifica IP Tailscale del wakeword
pct exec <CT_ID> -- tailscale ip -4

# Ping via Tailscale (dalla LXC-JARVIS o VPS)
tailscale ping jarvis-wakeword-casa1

# Test REST API (dalla LXC-JARVIS o VPS)
curl http://<TAILSCALE_IP_WAKEWORD>:8200/health

# Verifica .env orchestrator
grep WAKEWORD_SERVER_URLS .env
# Deve contenere: {"location_id": "http://<TAILSCALE_IP>:8200"}
```

### HA non raggiungibile

```bash
# Test diretto dall'host (LXC-JARVIS) — orchestrator usa network_mode: host
curl -H "Authorization: Bearer $TOKEN" \
  http://<HA_IP>:8123/api/

# Se via Tailscale
tailscale ping 100.x.x.x
```

### Database corrotto (LXC-JARVIS)

```bash
cp data/jarvis_state.db data/jarvis_state.db.bak
# Il DB viene ricreato automaticamente al riavvio se mancante
docker compose restart orchestrator
```

### Container non parte (LXC-JARVIS)

```bash
docker compose logs <servizio>
docker stats   # RAM esaurita?
```

---

## Aggiornamenti

### LXC-JARVIS (Docker stack)

```bash
git pull
docker compose down
docker compose build --no-cache
docker compose up -d
```

### LXC-OpenClaw (bare-metal)

OpenClaw si aggiorna indipendentemente sulla sua VM:

```bash
# Sulla LXC-OpenClaw
npm update -g openclaw

# Riavvia il servizio
sudo systemctl restart openclaw

# Verifica
curl http://localhost:18789/health
```

Per aggiornare la skill JARVIS sulla LXC-OpenClaw:

```bash
# Sulla LXC-OpenClaw
cd /opt/jarvis
git pull
# Ricopia la skill aggiornata
cp jarvis-orchestrator/skill/SKILL.md ~/.openclaw/workspace/skills/jarvis-orchestrator/
cp jarvis-orchestrator/skill/skill.json ~/.openclaw/workspace/skills/jarvis-orchestrator/
# Non serve riavvio — OpenClaw ricarica le skill automaticamente
```

### LXC-Wakeword (LXC)

```bash
# Dal Proxmox host
pct exec <CT_ID> -- bash -c '
  cd /opt/jarvis-wakeword
  git pull --depth 1
  cd wakeword-server
  docker compose up -d --build
  sleep 5
  curl -sf http://localhost:8200/health && echo " OK" || echo " FAIL"
'
```

Per aggiornare il plugin browser-dom:

```bash
# Sulla LXC-OpenClaw
cd /opt/jarvis
git pull
# Ricopia il plugin aggiornato
cp -r extensions/browser-dom/{src,index.ts,package.json,openclaw.plugin.json} \
    ~/.openclaw/extensions/browser-dom/
cd ~/.openclaw/extensions/browser-dom && npm install --omit=dev && cd -

# Riavvia OpenClaw per ricaricare il plugin
sudo systemctl restart openclaw
```

---

## File di Riferimento

| File | Contenuto |
|------|-----------|
| [DOCKER.md](DOCKER.md) | Docker Engine + Compose + NVIDIA Toolkit |
| [PROXMOX.md](PROXMOX.md) | LXC con GPU device sharing (driver su host) |
| [WORKSTATION.md](WORKSTATION.md) | VM Workstation Ubuntu Desktop (Chrome reale + IDE) |
| [OLLAMA.md](OLLAMA.md) | Modelli AI (Qwen 7B, nomic-embed-text) |
| [WHISPER.md](WHISPER.md) | faster-whisper STT |
| [terraform/](terraform/) | IaC per Proxmox (LXC-JARVIS + LXC-OpenClaw + LXC-Wakeword + VM-Workstation) |
| [ansible/](ansible/) | Playbook di configurazione (LXC-JARVIS + LXC-OpenClaw + VM-Workstation) |
| [../docker-compose.yml](../docker-compose.yml) | Stack Docker dentro LXC-JARVIS (NO OpenClaw, NO Tailscale) |
| [../cloud/scripts/deploy-wakeword.sh](../cloud/scripts/deploy-wakeword.sh) | Script deploy wakeword LXC (interattivo, su Proxmox host) |
| [../wakeword-server/](../wakeword-server/) | Wakeword server (openWakeWord + relay) |
| [../cloud/](../cloud/) | Deploy cloud (VPS senza GPU) |
| [../security/](../security/) | Stack security (Frigate + DoubleTake) |
| [../../extensions/browser-dom/](../../extensions/browser-dom/) | Plugin DOM automation (CDP) |
| [../../extensions/browser-dom/README.md](../../extensions/browser-dom/README.md) | Documentazione browser-dom |
