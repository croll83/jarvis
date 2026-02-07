# JARVIS — Deploy Locale (GPU + OpenClaw su VM separata)

Guida completa per il deploy locale di JARVIS su Proxmox con GPU NVIDIA.
I modelli locali (Qwen 7B router, Whisper STT) girano on-premise sulla VM-GPU;
il reasoning e gestito da Gemini 3 Pro via OpenClaw che gira **bare-metal su una
VM dedicata e separata** per isolamento di sicurezza.
Tailscale gira come container Docker per raggiungere HA remoti e la VM OpenClaw.

---

## Architettura Hardware

```
+---------------------------------------------------------------------+
|  HARDWARE: Proxmox Host (es. AtomMan G7 / Mini PC)                  |
|  CPU: 6-8 core | RAM: 24-32 GB | GPU: RTX 4060+ (8GB+ VRAM)        |
|  Disco: 200GB+ NVMe                                                 |
+---------------------------------------------------------------------+
|                                                                      |
|  VM-GPU (LXC Ubuntu 22.04) - GPU passthrough via cgroup             |
|  NVIDIA Container Toolkit installato                                 |
|                                                                      |
|  docker-compose.yml                                                  |
|  +----------------------------------------------------------------+ |
|  |                     jarvis_network                              | |
|  |                                                                 | |
|  |  ollama:11434   whisper:9000    tailscale         postgres:5432 | |
|  |  GPU: 4.7 GB    GPU: 0.4 GB    VPN gateway       (side proj)   | |
|  |  Qwen 7B Q4     faster-whisper  (100.x.x.x)                    | |
|  |  nomic-embed    base                              mongo:27017   | |
|  |                                                    (side proj)  | |
|  |  orchestrator:5000                                              | |
|  |  FastAPI + Admin UI                                             | |
|  |  Speaker ID (Resemblyzer)                                       | |
|  |  SQLite + ChromaDB                                              | |
|  +----------------------------------------------------------------+ |
|                                                                      |
|  GPU VRAM Budget:                                                    |
|  +-- Qwen 2.5 7B Q4_K_M .............. 4.4 GB                      |
|  +-- nomic-embed-text ................. 0.3 GB                      |
|  +-- faster-whisper base .............. 0.4 GB                      |
|  +-- TOTALE ........................... ~5.1 GB (serve 8GB+ GPU)    |
+---------------------------------------------------------------------+

+---------------------------------------------------------------------+
|  VM-OpenClaw (VM dedicata su Proxmox, NO Docker)                     |
|  Node.js bare-metal | systemd service                                |
|                                                                      |
|  openclaw gateway :18789                                             |
|  Gemini 3 Pro (API cloud)                                            |
|  Telegram bot integrato                                              |
|                                                                      |
|  Skill symlink:                                                      |
|  ~/.openclaw/skills/jarvis-orchestrator -> /opt/jarvis/skill         |
|                                                                      |
|  Raggiungibile via:                                                  |
|  - Tailscale MagicDNS: http://jarvis-openclaw:18789                  |
|  - LAN IP: http://192.168.x.x:18789                                 |
+---------------------------------------------------------------------+

        VM-GPU <--- LAN / Tailscale ---> VM-OpenClaw
```

### Ordine di boot

Le due VM sono indipendenti su Proxmox e si avviano in parallelo.

**VM-OpenClaw** (boot autonomo):
```
systemd -> openclaw.service (Node.js, porta 18789)
```

**VM-GPU** (boot sequenziale dei container):
```
1. ollama          -> diventa healthy (modelli caricati)
2. whisper         -> started
3. tailscale       -> si connette alla tailnet, diventa healthy
4. orchestrator    -> aspetta ollama + whisper + tailscale, poi parte
                      raggiunge OpenClaw via OPENCLAW_URL
```

### Cosa gira dove

| Servizio | Dove gira | Container/Processo | CPU | RAM | GPU/VRAM | Funzione |
|----------|-----------|-------------------|-----|-----|----------|----------|
| **Ollama** | VM-GPU | `jarvis_ollama` | - | - | 4.7 GB | Qwen 7B pre-routing + embeddings |
| **Whisper** | VM-GPU | `jarvis_whisper` | - | - | 0.4 GB | Speech-to-text (faster-whisper) |
| **Orchestrator** | VM-GPU | `jarvis_core` | 1-2 | 2 GB | - | FastAPI, HA control, memory, security |
| **Tailscale** | VM-GPU | `jarvis_tailscale` | - | 64 MB | - | VPN mesh per HA remoti + OpenClaw |
| **PostgreSQL** | VM-GPU | `jarvis_postgres` | 0.5 | 512 MB | - | Database side projects |
| **MongoDB** | VM-GPU | `jarvis_mongo` | 0.5 | 512 MB | - | Database side projects |
| **OpenClaw** | VM separata (bare-metal) | `openclaw.service` (systemd) | 0.5 | 512 MB | - | Gemini 3 Pro brain (API cloud) |

---

## Requisiti

### VM-GPU (LXC)

| Componente | Minimo | Consigliato |
|------------|--------|-------------|
| CPU | 4 core x86_64 | 6-8 core |
| RAM | 16 GB | 24-32 GB |
| GPU | NVIDIA 8 GB VRAM | RTX 4060 / RTX 5070 |
| Disco | 100 GB SSD | 200 GB NVMe |
| OS Host | Proxmox VE 8.x | - |
| OS Container | Ubuntu 22.04+ | - |
| Docker | 24.0+ | latest |
| Docker Compose | 2.20+ | latest |

### VM-OpenClaw

| Componente | Minimo | Consigliato |
|------------|--------|-------------|
| CPU | 1 core | 2 core |
| RAM | 512 MB | 1 GB |
| Disco | 10 GB | 20 GB |
| OS | Ubuntu 22.04+ / Debian 12+ | - |
| Node.js | 18+ | 20 LTS |
| GPU | Non richiesta | - |

---

## Quick Setup (Step-by-Step)

### Prerequisiti

- Proxmox host con GPU NVIDIA passthrough configurato (vedi [PROXMOX.md](PROXMOX.md))
- LXC container creato con accesso GPU per la VM-GPU (vedi [Terraform](terraform/) o manuale)
- VM dedicata per OpenClaw (senza GPU, anche leggera)
- Docker + Compose installati sulla VM-GPU (vedi [DOCKER.md](DOCKER.md))
- NVIDIA Container Toolkit installato sulla VM-GPU (vedi [DOCKER.md](DOCKER.md) Step 5)
- Node.js 18+ installato sulla VM-OpenClaw
- API keys pronte: Gemini, e opzionalmente Groq/OpenRouter come fallback
- Tailscale installato su entrambe le VM (container Docker su VM-GPU, host-level su VM-OpenClaw)
- Tailscale auth key: [login.tailscale.com/admin/settings/keys](https://login.tailscale.com/admin/settings/keys)

### STEP 1 — Infrastruttura (una tantum)

Se parti da zero su Proxmox:

```bash
# Opzione A: Terraform (automatizzato) — crea la VM-GPU (LXC)
cd infrastructure/terraform
cp terraform.tfvars.example terraform.tfvars
nano terraform.tfvars   # Credenziali Proxmox, sizing, GPU
terraform init && terraform apply

# Opzione B: Manuale
# Segui PROXMOX.md per creare LXC con GPU passthrough (VM-GPU)
# Crea una seconda VM per OpenClaw (senza GPU, 1 core, 1 GB RAM)
# Poi segui DOCKER.md per installare Docker + NVIDIA toolkit sulla VM-GPU
```

### STEP 2 — Setup VM-OpenClaw (una tantum)

Sulla VM dedicata a OpenClaw:

```bash
# Installa Node.js 20 LTS
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -
sudo apt-get install -y nodejs

# Installa OpenClaw globalmente
npm install -g openclaw

# Installa Tailscale (host-level)
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname=jarvis-openclaw

# Configura OpenClaw con onboard
openclaw onboard
# Inserisci: GEMINI_API_KEY, OPENCLAW_GATEWAY_TOKEN (stesso del .env sulla VM-GPU)

# Crea la directory skill e il symlink
sudo mkdir -p /opt/jarvis/skill
# (copia o clona i file della skill JARVIS in /opt/jarvis/skill)
mkdir -p ~/.openclaw/skills
ln -s /opt/jarvis/skill ~/.openclaw/skills/jarvis-orchestrator

# Crea il servizio systemd
sudo tee /etc/systemd/system/openclaw.service > /dev/null <<EOF
[Unit]
Description=OpenClaw Gateway
After=network-online.target
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

### STEP 3 — Clone repository (VM-GPU)

```bash
git clone https://github.com/croll83/jarvis.git
cd jarvis
```

### STEP 4 — Configura .env (VM-GPU)

```bash
cp .env.example .env
nano .env
```

Variabili obbligatorie:

| Variabile | Come ottenerla |
|-----------|----------------|
| `AI_BACKEND` | `local` (usa Ollama + Whisper locali) |
| `GEMINI_API_KEY` | [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey) |
| `OPENCLAW_GATEWAY_TOKEN` | `openssl rand -hex 32` — deve essere lo stesso usato in `openclaw onboard` sulla VM-OpenClaw |
| `OPENCLAW_URL` | `http://jarvis-openclaw:18789` (Tailscale MagicDNS) o `http://192.168.x.x:18789` (LAN) |
| `OPENCLAW_TELEGRAM_BOT_TOKEN` | @BotFather su Telegram |
| `JARVIS_APPROVAL_BOT_TOKEN` | @BotFather (secondo bot, separato) |
| `JARVIS_APPROVAL_CHAT_ID` | Scrivi al bot, poi `curl https://api.telegram.org/bot<TOKEN>/getUpdates` |
| `TAILSCALE_AUTHKEY` | [login.tailscale.com/admin/settings/keys](https://login.tailscale.com/admin/settings/keys) - reusable + ephemeral |
| `TAILSCALE_HOSTNAME` | `wagmi` (o il nome che preferisci nella tailnet) |
| `HASS_URL` | `http://homeassistant:8123` (locale) o `http://100.x.x.x:8123` (via Tailscale) |
| `JARVIS_HASS_TOKEN` | HA -> Profilo -> Token di lunga durata |
| `POSTGRES_PASSWORD` | Password forte a scelta |
| `MONGO_PASSWORD` | Password forte a scelta |

Variabili opzionali (API cloud come fallback):

```env
GROQ_API_KEY=gsk_...          # STT via Groq (fallback se Whisper locale down)
OPENROUTER_API_KEY=sk-or-...  # Routing via OpenRouter (fallback se Ollama down)
```

### STEP 5 — Configura system prompt (VM-GPU)

```bash
nano config/router_system_prompt.txt   # Regole di routing per Qwen
```

### STEP 6 — Avvia lo stack (VM-GPU)

```bash
docker compose up -d
```

### STEP 7 — Scarica modelli Ollama (VM-GPU)

```bash
# Attendi che Ollama sia pronto, poi:
bash setup.sh
```

Lo script scarica:
1. **Qwen 2.5 7B Instruct Q4_K_M** (~4.7 GB) - router/pre-router
2. **nomic-embed-text** (~274 MB) - embeddings per memoria semantica
3. Esegue warmup dei modelli

### STEP 8 — Verifica

```bash
# === VM-OpenClaw ===

# OpenClaw attivo?
sudo systemctl status openclaw

# OpenClaw healthy?
curl http://localhost:18789/health

# Tailscale connesso?
tailscale status

# === VM-GPU ===

# Tailscale connesso?
docker exec jarvis_tailscale tailscale status

# Orchestrator healthy?
curl http://localhost:5000/health

# OpenClaw raggiungibile dalla VM-GPU?
# (via Tailscale MagicDNS)
curl http://jarvis-openclaw:18789/health
# (oppure via LAN)
curl http://192.168.x.x:18789/health

# Ollama con modelli?
curl http://localhost:11434/api/tags

# GPU ok?
nvidia-smi

# HA raggiungibile?
docker exec jarvis_core curl -s \
  -H "Authorization: Bearer <HASS_TOKEN>" \
  http://<HA_IP>:8123/api/ | head -c 100

# Logs in tempo reale
docker compose logs -f orchestrator
```

### STEP 9 — Primo accesso alla dashboard

Apri `http://localhost:5000/admin` nel browser (dalla VM-GPU o via Tailscale da qualsiasi dispositivo nella tailnet).

Da qui puoi:
- Creare utenti e assegnare ruoli (admin/user)
- Enrollare voci (speaker identification con Resemblyzer)
- Gestire location e entity maps
- Configurare preferenze globali
- Monitorare lo stato dei servizi

### STEP 10 — Dashboard OpenClaw

La dashboard di OpenClaw e accessibile direttamente dalla VM-OpenClaw:
`http://jarvis-openclaw:18789` (via Tailscale MagicDNS da qualsiasi dispositivo nella tailnet)
oppure `http://192.168.x.x:18789` dalla LAN.

Da qui puoi gestire le skill registrate, vedere i log delle conversazioni e monitorare lo stato del gateway.

### STEP 11 — Telegram webhook

Il webhook Telegram e gestito da **OpenClaw** (non dall'orchestrator).
Configura il webhook del bot OpenClaw puntando al tuo dominio:

```bash
curl "https://api.telegram.org/bot<OPENCLAW_TELEGRAM_BOT_TOKEN>/setWebhook?url=https://<tuo-dominio>/telegram_webhook"
```

---

## Deploy Automatizzato (Ansible)

Se preferisci automatizzare tutto, Ansible fa gli step di setup in un comando.

**Nota:** Il playbook Ansible gestisce la **VM-GPU**. Il setup della VM-OpenClaw
e un processo separato (vedi STEP 2 sopra) in quanto e una VM indipendente con
un'installazione bare-metal di Node.js e OpenClaw.

```bash
cd infrastructure/ansible
cp inventory/hosts.yml.example inventory/hosts.yml
cp group_vars/all.yml.example group_vars/all.yml
nano group_vars/all.yml    # Tutte le variabili: deploy_type, API keys, etc.
nano inventory/hosts.yml   # IP della VM-GPU target

ansible-playbook playbooks/site.yml
```

Il playbook esegue in sequenza (sulla VM-GPU):

```
common.yml   -> Sistema base, Docker, firewall
nvidia.yml   -> NVIDIA Container Toolkit
jarvis.yml   -> Clone repo, .env, docker-compose up, pull modelli
security.yml -> Frigate + DoubleTake (opzionale)
verify.yml   -> Health check di tutti i servizi (incluso OpenClaw remoto)
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

Questi richiedono riavvio del container orchestrator sulla VM-GPU:

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

Tailscale gira come container Docker sulla VM-GPU (non sull'host).
Sulla VM-OpenClaw gira host-level.
Permette all'orchestrator di raggiungere HA remoti e la VM-OpenClaw senza aprire porte.

### Dove serve Tailscale

| Nodo | Dove gira | Ruolo | Hostname |
|------|-----------|-------|----------|
| **Napoli (Wagmi)** | Container Docker nella VM-GPU | Gateway VPN per lo stack | `jarvis-wagmi` |
| **VM-OpenClaw** | Host-level sulla VM dedicata | Espone OpenClaw sulla tailnet | `jarvis-openclaw` |
| **Milano (Albani)** | Add-on HAOS o host-level | Espone HA sulla tailnet | `ha-albani` |

### Schema di rete

```
+---------------------------------------------------------------+
|                    TAILSCALE MESH (100.x.x.x)                  |
|                                                                 |
|   Napoli VM-GPU (LXC Docker)      VM-OpenClaw (bare-metal)    |
|   +-------------------+           +-------------------+        |
|   | jarvis-wagmi      |<--------->| jarvis-openclaw   |        |
|   |                   |           |                   |        |
|   | Orchestrator      |           | OpenClaw Gateway  |        |
|   | Ollama, Whisper   |           | Gemini 3 Pro      |        |
|   | Tailscale (ctnr)  |           | Telegram bot      |        |
|   | Postgres, Mongo   |           | JARVIS skill      |        |
|   | HA Wagmi (locale) |           | Tailscale (host)  |        |
|   +-------------------+           +-------------------+        |
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
|   wagmi -> openclaw: http://jarvis-openclaw:18789 (MagicDNS)  |
|   wagmi -> albani: 100.x.x.x:8123 (HA API via Tailscale)     |
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

---

## Porte di Rete

### VM-GPU

| Porta Host | Servizio | Protocollo | Accesso |
|------------|----------|------------|---------|
| 5000 | Orchestrator + Admin UI | HTTP | LAN / Tailscale |
| 9000 | Whisper STT | HTTP | Interno |
| 11434 | Ollama API | HTTP | Interno |
| 5432 | PostgreSQL | TCP | Interno |
| 27017 | MongoDB | TCP | Interno |
| 41641/udp | Tailscale NAT traversal | UDP | WAN (container) |

### VM-OpenClaw

| Porta | Servizio | Protocollo | Accesso |
|-------|----------|------------|---------|
| 18789 | OpenClaw Gateway + Dashboard | HTTP | LAN / Tailscale |

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

GPU richiesta:      Si (8GB+ VRAM, solo VM-GPU)      No
Latenza voce:       ~200ms (locale)                  ~800ms (API round-trip)
Offline mode:       Parziale (Qwen locale)           No (tutto API)
Costo mensile:      ~0 (solo corrente)               ~4-8/mese VPS + API
```

---

## Monitoring

```bash
# === VM-GPU ===

# Stato container
docker compose ps

# Logs orchestrator
docker compose logs -f orchestrator

# Health servizi interni
curl http://localhost:5000/health/services

# Risorse e GPU
docker stats
nvidia-smi

# OpenClaw raggiungibile?
curl http://jarvis-openclaw:18789/health

# === VM-OpenClaw ===

# Stato servizio
sudo systemctl status openclaw

# Logs OpenClaw
sudo journalctl -u openclaw -f

# Health
curl http://localhost:18789/health
```

---

## Troubleshooting

### GPU non rilevata (VM-GPU)

```bash
nvidia-smi                           # Verifica driver NVIDIA
docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi
docker info | grep -i nvidia         # Runtime configurato?
```

### Ollama non risponde (VM-GPU)

```bash
docker logs jarvis_ollama
curl http://localhost:11434/api/tags  # Deve rispondere con lista modelli
```

### Memoria VRAM insufficiente (VM-GPU)

```bash
nvidia-smi   # Verifica utilizzo VRAM

# Se OOM, riduci modelli caricati contemporaneamente:
# In docker-compose.yml, cambia OLLAMA_MAX_LOADED_MODELS=1
```

### Tailscale non si connette (VM-GPU)

```bash
docker exec jarvis_tailscale tailscale status
docker compose logs tailscale
# Se la TAILSCALE_AUTHKEY e scaduta, generane una nuova
```

### OpenClaw non raggiungibile (dalla VM-GPU)

```bash
# Verifica che OpenClaw sia attivo sulla sua VM
ssh user@jarvis-openclaw "sudo systemctl status openclaw"

# Test connettivita Tailscale
docker exec jarvis_tailscale tailscale ping jarvis-openclaw

# Test diretto via LAN (se sulla stessa rete)
curl http://192.168.x.x:18789/health

# Test via Tailscale MagicDNS
curl http://jarvis-openclaw:18789/health

# Logs OpenClaw sulla VM dedicata
ssh user@jarvis-openclaw "sudo journalctl -u openclaw --since '5 min ago'"
```

### OpenClaw non parte (VM-OpenClaw)

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

### HA non raggiungibile

```bash
# Test dal container orchestrator (VM-GPU)
docker exec jarvis_core curl -H "Authorization: Bearer $TOKEN" \
  http://<HA_IP>:8123/api/

# Se via Tailscale
docker exec jarvis_tailscale tailscale ping 100.x.x.x
```

### Database corrotto (VM-GPU)

```bash
cp data/jarvis_state.db data/jarvis_state.db.bak
# Il DB viene ricreato automaticamente al riavvio se mancante
docker compose restart orchestrator
```

### Container non parte (VM-GPU)

```bash
docker compose logs <servizio>
docker stats   # RAM esaurita?
```

---

## Aggiornamenti

### VM-GPU (Docker stack)

```bash
git pull
docker compose down
docker compose build --no-cache
docker compose up -d
```

### VM-OpenClaw (bare-metal)

OpenClaw si aggiorna indipendentemente sulla sua VM:

```bash
# Sulla VM-OpenClaw
npm update -g openclaw

# Riavvia il servizio
sudo systemctl restart openclaw

# Verifica
curl http://localhost:18789/health
```

Per aggiornare la skill JARVIS sulla VM-OpenClaw:

```bash
# Sulla VM-OpenClaw
cd /opt/jarvis/skill
git pull   # oppure copia i file aggiornati
# Non serve riavvio — OpenClaw ricarica le skill automaticamente
```

---

## File di Riferimento

| File | Contenuto |
|------|-----------|
| [DOCKER.md](DOCKER.md) | Docker Engine + Compose + NVIDIA Toolkit |
| [PROXMOX.md](PROXMOX.md) | LXC con GPU passthrough |
| [OLLAMA.md](OLLAMA.md) | Modelli AI (Qwen 7B, nomic-embed-text) |
| [WHISPER.md](WHISPER.md) | faster-whisper STT |
| [terraform/](terraform/) | IaC per Proxmox LXC (VM-GPU) |
| [ansible/](ansible/) | Playbook di configurazione (VM-GPU) |
| [../docker-compose.yml](../docker-compose.yml) | Stack locale VM-GPU (NO OpenClaw) |
| [../cloud/](../cloud/) | Deploy cloud (VPS senza GPU) |
| [../security/](../security/) | Stack security (Frigate + DoubleTake) |
