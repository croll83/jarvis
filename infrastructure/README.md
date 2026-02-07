# JARVIS — Deploy Locale (GPU)

Guida completa per il deploy locale di JARVIS su Proxmox con GPU NVIDIA.
I modelli locali (Qwen 7B router, Whisper STT) girano on-premise; il reasoning
e gestito da Gemini 3 Pro via OpenClaw (API cloud).
Tailscale gira come container Docker per raggiungere HA remoti.

---

## Architettura Hardware

```
+---------------------------------------------------------------------+
|  HARDWARE: Proxmox Host (es. AtomMan G7 / Mini PC)                  |
|  CPU: 6-8 core | RAM: 24-32 GB | GPU: RTX 4060+ (8GB+ VRAM)        |
|  Disco: 200GB+ NVMe                                                 |
+---------------------------------------------------------------------+
|                                                                      |
|  LXC Container (Ubuntu 22.04) - GPU passthrough via cgroup           |
|  NVIDIA Container Toolkit installato                                 |
|                                                                      |
|  docker-compose.yml                                                  |
|  +----------------------------------------------------------------+ |
|  |                     jarvis_network                              | |
|  |                                                                 | |
|  |  ollama:11434   whisper:9000    tailscale          openclaw     | |
|  |  GPU: 4.7 GB    GPU: 0.4 GB    VPN gateway        :18789       | |
|  |  Qwen 7B Q4     faster-whisper  (100.x.x.x)       Gemini 3 Pro| |
|  |  nomic-embed    base                                            | |
|  |                                                                 | |
|  |  orchestrator:5000              postgres:5432   mongo:27017     | |
|  |  FastAPI + Admin UI             (side projects)                 | |
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
```

### Ordine di boot dei container

```
1. ollama          -> diventa healthy (modelli caricati)
2. whisper         -> started
3. tailscale       -> si connette alla tailnet, diventa healthy
4. orchestrator    -> aspetta ollama + whisper + tailscale, poi parte
5. openclaw        -> aspetta orchestrator healthy, poi parte
```

### Cosa gira dove

| Servizio | Container | CPU | RAM | GPU/VRAM | Funzione |
|----------|-----------|-----|-----|----------|----------|
| **Ollama** | `jarvis_ollama` | - | - | 4.7 GB | Qwen 7B pre-routing + embeddings |
| **Whisper** | `jarvis_whisper` | - | - | 0.4 GB | Speech-to-text (faster-whisper) |
| **Orchestrator** | `jarvis_core` | 1-2 | 2 GB | - | FastAPI, HA control, memory, security |
| **OpenClaw** | `jarvis_openclaw` | 0.5 | 512 MB | - | Gemini 3 Pro brain (API cloud) |
| **Tailscale** | `jarvis_tailscale` | - | 64 MB | - | VPN mesh per HA remoti |
| **PostgreSQL** | `jarvis_postgres` | 0.5 | 512 MB | - | Database side projects |
| **MongoDB** | `jarvis_mongo` | 0.5 | 512 MB | - | Database side projects |

---

## Requisiti

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

---

## Quick Setup (Step-by-Step)

### Prerequisiti

- Proxmox host con GPU NVIDIA passthrough configurato (vedi [PROXMOX.md](PROXMOX.md))
- LXC container creato con accesso GPU (vedi [Terraform](terraform/) o manuale)
- Docker + Compose installati (vedi [DOCKER.md](DOCKER.md))
- NVIDIA Container Toolkit installato (vedi [DOCKER.md](DOCKER.md) Step 5)
- API keys pronte: Gemini, e opzionalmente Groq/OpenRouter come fallback
- Tailscale auth key: [login.tailscale.com/admin/settings/keys](https://login.tailscale.com/admin/settings/keys)

### STEP 1 — Infrastruttura (una tantum)

Se parti da zero su Proxmox:

```bash
# Opzione A: Terraform (automatizzato)
cd infrastructure/terraform
cp terraform.tfvars.example terraform.tfvars
nano terraform.tfvars   # Credenziali Proxmox, sizing, GPU
terraform init && terraform apply

# Opzione B: Manuale
# Segui PROXMOX.md per creare LXC con GPU passthrough
# Poi segui DOCKER.md per installare Docker + NVIDIA toolkit
```

### STEP 2 — Clone repository

```bash
git clone https://github.com/croll83/jarvis.git
cd jarvis
```

### STEP 3 — Configura .env

```bash
cp .env.example .env
nano .env
```

Variabili obbligatorie:

| Variabile | Come ottenerla |
|-----------|----------------|
| `AI_BACKEND` | `local` (usa Ollama + Whisper locali) |
| `GEMINI_API_KEY` | [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey) |
| `OPENCLAW_GATEWAY_TOKEN` | `openssl rand -hex 32` |
| `OPENCLAW_TELEGRAM_BOT_TOKEN` | @BotFather su Telegram |
| `JARVIS_APPROVAL_BOT_TOKEN` | @BotFather (secondo bot, separato) |
| `JARVIS_APPROVAL_CHAT_ID` | Scrivi al bot, poi `curl https://api.telegram.org/bot<TOKEN>/getUpdates` |
| `TAILSCALE_AUTHKEY` | [login.tailscale.com/admin/settings/keys](https://login.tailscale.com/admin/settings/keys) - reusable + ephemeral |
| `TAILSCALE_HOSTNAME` | `wagmi` (o il nome che preferisci nella tailnet) |
| `HASS_URL` | `http://homeassistant:8123` (locale) o `http://100.x.x.x:8123` (via Tailscale) |
| `JARVIS_HASS_TOKEN` | HA -> Profilo -> Token di lunga durata |
| `JWT_SECRET` | `openssl rand -hex 32` |
| `JARVIS_TELEGRAM_TOKEN` | @BotFather (bot principale notifiche) |
| `JARVIS_TELEGRAM_CHAT_ID` | Il tuo chat ID Telegram |
| `POSTGRES_PASSWORD` | Password forte a scelta |
| `MONGO_PASSWORD` | Password forte a scelta |

Variabili opzionali (API cloud come fallback):

```env
GROQ_API_KEY=gsk_...          # STT via Groq (fallback se Whisper locale down)
OPENROUTER_API_KEY=sk-or-...  # Routing via OpenRouter (fallback se Ollama down)
```

### STEP 4 — Configura system prompt

```bash
nano config/router_system_prompt.txt   # Regole di routing per Qwen
```

### STEP 5 — Avvia lo stack

```bash
docker compose up -d
```

### STEP 6 — Scarica modelli Ollama

```bash
# Attendi che Ollama sia pronto, poi:
bash setup.sh
```

Lo script scarica:
1. **Qwen 2.5 7B Instruct Q4_K_M** (~4.7 GB) - router/pre-router
2. **nomic-embed-text** (~274 MB) - embeddings per memoria semantica
3. Esegue warmup dei modelli

### STEP 7 — Verifica

```bash
# Tailscale connesso?
docker exec jarvis_tailscale tailscale status

# Orchestrator healthy?
curl http://localhost:5000/health

# OpenClaw healthy?
curl http://localhost:18789/health

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

### STEP 8 — Primo accesso alla dashboard

Apri `http://localhost:5000/admin` nel browser.

Da qui puoi:
- Creare utenti e assegnare ruoli (admin/user)
- Enrollare voci (speaker identification con Resemblyzer)
- Gestire location e entity maps
- Configurare preferenze globali
- Monitorare lo stato dei servizi

### STEP 9 — Telegram webhook (opzionale)

```bash
curl "https://api.telegram.org/bot<JARVIS_TELEGRAM_TOKEN>/setWebhook?url=https://<tuo-dominio>/telegram_webhook"
```

---

## Deploy Automatizzato (Ansible)

Se preferisci automatizzare tutto, Ansible fa gli step 1-6 in un comando:

```bash
cd infrastructure/ansible
cp inventory/hosts.yml.example inventory/hosts.yml
cp group_vars/all.yml.example group_vars/all.yml
nano group_vars/all.yml    # Tutte le variabili: deploy_type, API keys, etc.
nano inventory/hosts.yml   # IP del target

ansible-playbook playbooks/site.yml
```

Il playbook esegue in sequenza:

```
common.yml   -> Sistema base, Docker, firewall
nvidia.yml   -> NVIDIA Container Toolkit
jarvis.yml   -> Clone repo, .env, docker-compose up, pull modelli
security.yml -> Frigate + DoubleTake (opzionale)
verify.yml   -> Health check di tutti i servizi
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

Questi richiedono riavvio del container:

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

Tailscale gira come container Docker (non sull'host). Permette all'orchestrator
di raggiungere HA remoti senza aprire porte.

### Dove serve Tailscale

| Nodo | Dove gira | Ruolo | Hostname |
|------|-----------|-------|----------|
| **Napoli (Wagmi)** | Container Docker nel LXC | Gateway VPN per lo stack | `jarvis-wagmi` |
| **Milano (Albani)** | Add-on HAOS o host-level | Espone HA sulla tailnet | `ha-albani` |

### Schema di rete

```
+---------------------------------------------------------------+
|                    TAILSCALE MESH (100.x.x.x)                  |
|                                                                 |
|   Napoli (LXC Docker)              Milano (Mini PC)            |
|   +-------------------+           +-------------------+        |
|   | jarvis-wagmi      |<--------->| ha-albani         |        |
|   |                   |           |                   |        |
|   | Orchestrator      |           | Home Assistant    |        |
|   | Ollama, Whisper   |           | Zigbee/Z-Wave     |        |
|   | OpenClaw          |           | Automazioni       |        |
|   | HA Wagmi (locale) |           |                   |        |
|   +-------------------+           +-------------------+        |
|                                                                 |
|   wagmi -> albani: 100.x.x.x:8123 (HA API via Tailscale)      |
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

---

## Porte di Rete

| Porta Host | Servizio | Protocollo | Accesso |
|------------|----------|------------|---------|
| 5000 | Orchestrator + Admin UI | HTTP | LAN |
| 9000 | Whisper STT | HTTP | Interno |
| 11434 | Ollama API | HTTP | Interno |
| 18789 | OpenClaw | HTTP | Interno |
| 5432 | PostgreSQL | TCP | Interno |
| 27017 | MongoDB | TCP | Interno |
| 41641/udp | Tailscale NAT traversal | UDP | WAN (container) |

---

## Confronto Locale vs Cloud

```
                    LOCALE (GPU)                    CLOUD (VPS)
                    ────────────                    ───────────
Pre-routing:        Qwen 7B Q4 (Ollama, locale)     Qwen 7B (OpenRouter API)
STT:                faster-whisper (GPU, locale)     Groq Whisper (API cloud)
Brain:              Gemini 3 Pro (OpenClaw, API)     Gemini 3 Pro (OpenClaw, API)
Embeddings:         nomic-embed-text (Ollama)        nomic-embed-text (solo locale)
HA control:         HTTP diretto / Tailscale         Tailscale (tutto remoto)
Speaker ID:         Resemblyzer (in orchestrator)    Resemblyzer (in orchestrator)

GPU richiesta:      Si (8GB+ VRAM)                   No
Latenza voce:       ~200ms (locale)                  ~800ms (API round-trip)
Offline mode:       Parziale (Qwen locale)           No (tutto API)
Costo mensile:      ~0 (solo corrente)               ~4-8/mese VPS + API
```

---

## Monitoring

```bash
# Stato container
docker compose ps

# Logs
docker compose logs -f orchestrator

# Health servizi interni
curl http://localhost:5000/health/services

# Risorse e GPU
docker stats
nvidia-smi
```

---

## Troubleshooting

### GPU non rilevata

```bash
nvidia-smi                           # Verifica driver NVIDIA
docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi
docker info | grep -i nvidia         # Runtime configurato?
```

### Ollama non risponde

```bash
docker logs jarvis_ollama
curl http://localhost:11434/api/tags  # Deve rispondere con lista modelli
```

### Memoria VRAM insufficiente

```bash
nvidia-smi   # Verifica utilizzo VRAM

# Se OOM, riduci modelli caricati contemporaneamente:
# In docker-compose.yml, cambia OLLAMA_MAX_LOADED_MODELS=1
```

### Tailscale non si connette

```bash
docker exec jarvis_tailscale tailscale status
docker compose logs tailscale
# Se la TAILSCALE_AUTHKEY e scaduta, generane una nuova
```

### HA non raggiungibile

```bash
# Test dal container orchestrator
docker exec jarvis_core curl -H "Authorization: Bearer $TOKEN" \
  http://<HA_IP>:8123/api/

# Se via Tailscale
docker exec jarvis_tailscale tailscale ping 100.x.x.x
```

### Database corrotto

```bash
cp data/jarvis_state.db data/jarvis_state.db.bak
# Il DB viene ricreato automaticamente al riavvio se mancante
docker compose restart orchestrator
```

### Container non parte

```bash
docker compose logs <servizio>
docker stats   # RAM esaurita?
```

---

## Aggiornamenti

```bash
git pull
docker compose down
docker compose build --no-cache
docker compose up -d
```

---

## File di Riferimento

| File | Contenuto |
|------|-----------|
| [DOCKER.md](DOCKER.md) | Docker Engine + Compose + NVIDIA Toolkit |
| [PROXMOX.md](PROXMOX.md) | LXC con GPU passthrough |
| [OLLAMA.md](OLLAMA.md) | Modelli AI (Qwen 7B, nomic-embed-text) |
| [WHISPER.md](WHISPER.md) | faster-whisper STT |
| [terraform/](terraform/) | IaC per Proxmox LXC |
| [ansible/](ansible/) | Playbook di configurazione |
| [../docker-compose.yml](../docker-compose.yml) | Stack locale (GPU) |
| [../cloud/](../cloud/) | Deploy cloud (VPS senza GPU) |
| [../security/](../security/) | Stack security (Frigate + DoubleTake) |
