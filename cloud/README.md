# JARVIS — Deploy Cloud (VPS senza GPU)

> **NOTA: Deploy Legacy**
> Il deploy principale di JARVIS è stato migrato su **LXC locale con GPU** (vedi `infrastructure/README.md`).
> Questa guida cloud rimane come riferimento per:
> - Deploy senza GPU su VPS economico
> - Testing/staging prima di passare al deploy locale
> - Scenari dove non si dispone di hardware GPU locale

Guida completa per il deploy di JARVIS su un VPS. Nessuna GPU richiesta: AI via API esterne
(Cloud LLM via AI Agent, Groq per STT, OpenRouter per routing).
Tailscale gira host-level (servizio di sistema, NON in Docker) per raggiungere Home Assistant.
**AI Agent gira bare-metal** sulla stessa macchina o su un server separato (blackbox — vedi documentazione dell'AI Agent usato).

> **NOTA:** Il wakeword-server (`jarvis/wakeword-server/`) NON va deployato su VPS cloud.
> Ogni casa ha il proprio wakeword-server su un LXC locale (stessa LAN degli AtomS3R).
> Il VPS riceve solo il relay audio post-wake tramite Tailscale.

---

## Architettura

```
                    Internet
                        |
                        v
              +-------------------+
              |   Nginx + SSL     |       (VPS — jarvis.yourdomain.com)
              |  (certbot)        |
              +--------+----------+
                       | :5000
              +--------v-------------------------------------------+
              |  VPS (Hetzner / Contabo / simile)                  |
              |  2 vCPU, 4 GB RAM, 40 GB SSD                      |
              |                                                     |
              |  AI Agent (bare-metal, blackbox)                    |
              |  ws://127.0.0.1:18789 — loopback only              |
              |                                                     |
              |  Nginx TLS proxy (your-agent-domain)                |
              |  :18789 TLS → ws://127.0.0.1:18789 (API/WS)       |
              |  :443   TLS → http://127.0.0.1:18789 (dashboard)   |
              |  Let's Encrypt cert (Cloudflare DNS challenge)      |
              |                                                     |
              |  Tailscale (host-level, systemd)                    |
              |  VPN mesh — 100.x.x.x                              |
              |                                                     |
              |  docker-compose.cloud.yml                           |
              |  +-----------------------------------------------+ |
              |  |  orchestrator (network_mode: host)             | |
              |  |  :5000 (FastAPI) — AI_BACKEND=api              | |
              |  +-----------------------------------------------+ |
              |  +-----------------------------------------------+ |
              |  |  chromadb (Docker)                             | |
              |  |  127.0.0.1:8000 — Shared vector store          | |
              |  |  chromadb/chroma:0.6.3                          | |
              |  +-----------------------------------------------+ |
              |  +-----------------------------------------------+ |
              |  |  ontology-server (Docker)                      | |
              |  |  127.0.0.1:8100 (FastAPI) — Knowledge Graph   | |
              |  |  SQLite + ACL (X-Speaker-Id)                   | |
              |  +-----------------------------------------------+ |
              +--------+-------------------------------------------+
                       | Tailscale (100.x.x.x)
                       v
              +-------------------+        +-------------------+
              | HA Wagmi (Napoli) |        | HA Albani (Milano)|
              | 100.x.x.x:8123   |        | 100.x.x.x:8123   |
              +-------------------+        +-------------------+

              +-------------------------------------------+
              | Proxmox locale (1 per casa)               |
              |  LXC Wakeword (100.x.x.x:8200 Tailscale) |
              |  openWakeWord + relay on-demand            |
              |  LAN :8200 <- AtomS3R devices (WiFi)      |
              +-------------------------------------------+

Connessioni TLS esterne:
  orchestrator  -->  wss://your-agent-host:18789  (API/WS)
  browser       -->  https://your-agent-host       (dashboard, :443)
```

> **Wakeword server**: il VPS raggiunge i wakeword-server locali via Tailscale per push
> di configurazione (`POST /api/config/{device_id}`) e trigger_listen
> (`POST /api/trigger_listen/{device_id}`). Il relay audio avviene in direzione opposta:
> il wakeword-server apre un WebSocket on-demand verso il VPS solo quando rileva un wake word.

### Ordine di boot

```
1. tailscale (systemd)     → servizio host-level, parte al boot del VPS, si connette alla tailnet
2. ai-agent (systemd)      → servizio bare-metal AI Agent, bind: "auto" (loopback), parte al boot del VPS
3. nginx (systemd)         → TLS proxy, termina TLS e proxya a localhost:18789
4. chromadb (Docker)        → Shared vector store, 127.0.0.1:8000
5. ontology-server (Docker) → Knowledge Graph API, 127.0.0.1:8100
6. orchestrator (Docker)    → network_mode: host, vede Tailscale direttamente
                              raggiunge AI Agent via wss://your-agent-host:18789
                              raggiunge ontology via localhost:8100
```

Tailscale, AI Agent e Nginx sono processi systemd che partono prima di Docker.
L'orchestrator usa `network_mode: host`, quindi vede la rete dell'host direttamente (Tailscale, localhost, ecc.).
Nginx termina TLS (Let's Encrypt) e proxya le connessioni API/WS a AI Agent su loopback.

---

## Quick Setup (Step-by-Step)

### Prerequisiti

- VPS con Ubuntu 22.04/24.04 LTS, 2+ vCPU, 4+ GB RAM
- Accesso root via SSH (IP pubblico)
- API keys pronte: Gemini, Groq, OpenRouter
- Account Tailscale: [login.tailscale.com](https://login.tailscale.com)
- HA con Tailscale add-on attivo (o Tailscale host-level sulla macchina HA)

### STEP 1 — Setup base VPS

Installa i prerequisiti di sistema:

```bash
ssh root@<vps-ip>

# Aggiornamento sistema
apt update && apt upgrade -y

# Docker + Compose
curl -fsSL https://get.docker.com | sh
apt install -y docker-compose-plugin

# Tailscale (host-level)
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --hostname=jarvis-cloud

# Crea utente jarvis
adduser --disabled-password --gecos "JARVIS System User" jarvis
usermod -aG docker jarvis

# Directory
mkdir -p /opt/jarvis /opt/jarvis/data /opt/jarvis/config /opt/jarvis/voice_models
chown -R jarvis:jarvis /opt/jarvis

# Tool utili
apt install -y htop curl wget git vim nano jq nginx certbot python3-certbot-dns-cloudflare

# Firewall
apt install -y ufw
ufw default deny incoming && ufw default allow outgoing
ufw allow ssh && ufw allow http && ufw allow https && ufw allow 41641/udp
echo "y" | ufw enable
```

### STEP 2 — Clone repository

```bash
su - jarvis
git clone https://github.com/croll83/jarvis.git /opt/jarvis
```

### STEP 3 — Installa AI Agent (blackbox)

AI Agent (Hermes, OpenClaw, o altro) viene trattato come una blackbox dall'orchestrator.
L'orchestrator si connette via `AI_AGENT_URL` (HTTPS/WSS) e si autentica con `AI_AGENT_TOKEN`.

**Installa il tuo AI Agent seguendo la documentazione specifica del software scelto.**

Requisiti dall'orchestrator:
- L'agent deve esporre un'API HTTP/WS sulla porta 18789 (o altra porta configurabile)
- L'agent deve supportare skill registration (la skill JARVIS viene registrata come tool)
- Il token di autenticazione gateway deve essere lo stesso configurato nel `.env` dell'orchestrator

Dopo l'installazione, copia le skill JARVIS nella directory dell'agent:

```bash
# Skill orchestrator (domotica, TTS, security, memory)
# Adatta il path alla directory skills del tuo agent
cp /opt/jarvis/jarvis-orchestrator/skill/SKILL.md <agent-skills-dir>/jarvis-orchestrator/
cp /opt/jarvis/jarvis-orchestrator/skill/skill.json <agent-skills-dir>/jarvis-orchestrator/

# Skill ontology (knowledge graph — crea/query/relate entita)
cp -r /opt/jarvis/ontology-server/skill/* <agent-skills-dir>/ontology/
```

> **Nota**: non usare symlink — alcuni agent vanno in ELOOP con i link simbolici.

### STEP 4 — Configura .env

```bash
cd /opt/jarvis/cloud
cp .env.example .env
nano .env
```

Variabili obbligatorie da compilare:

| Variabile | Come ottenerla |
|-----------|----------------|
| `AI_AGENT_URL` | URL HTTPS del gateway AI Agent (es. `https://your-agent-host:18789`) |
| `GEMINI_API_KEY` | [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey) |
| `GROQ_API_KEY` | [console.groq.com/keys](https://console.groq.com/keys) |
| `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) |
| `AI_AGENT_TOKEN` | Token gateway condiviso tra orchestrator e AI Agent |
| `AI_AGENT_WS_URL` | URL WebSocket TLS del gateway (es. `wss://your-agent-host:18789`) |
| `AI_AGENT_TELEGRAM_BOT_TOKEN` | @BotFather su Telegram |
| `JARVIS_APPROVAL_BOT_TOKEN` | @BotFather (secondo bot, separato da AI Agent) |
| `JARVIS_APPROVAL_CHAT_ID` | Scrivi al bot, poi `curl https://api.telegram.org/bot<TOKEN>/getUpdates` |
| `HASS_URL` | `http://100.x.x.x:8123` (IP Tailscale del tuo HA, senza `/api`) |
| `JARVIS_HASS_TOKEN` | HA → Profilo → Token di lunga durata |
| `ONTOLOGY_API_TOKEN` | (opzionale) `openssl rand -hex 32` — protegge l'API ontology |
| `WAKEWORD_SERVER_URLS` | (opzionale) JSON map `{"location_id": "http://<TAILSCALE_IP>:8200"}` |

> **Nota**: AI Agent gira bare-metal, NON in Docker. Tailscale gira host-level, NON in Docker. Il `.env` viene letto solo dal container Docker (orchestrator). AI Agent ha la sua configurazione separata. Tailscale si autentica con `tailscale up --hostname=jarvis-cloud`.

### STEP 4b — Deploy Wakeword Server (locale, 1 per casa)

Il wakeword-server NON gira sul VPS. Va deployato su un LXC locale (Proxmox) nella stessa
LAN degli AtomS3R. Dopo il deploy, inserisci il suo IP Tailscale nel `.env` del VPS.

```bash
# Sul Proxmox HOST (non sul VPS)
sudo bash /opt/jarvis/cloud/scripts/deploy-wakeword.sh
```

Lo script crea un LXC container con Docker + Tailscale + wakeword-server.
Al termine, riporta l'IP Tailscale da inserire in `WAKEWORD_SERVER_URLS`.

Per i dettagli, vedi: [`infrastructure/README.md` STEP 2b](../infrastructure/README.md#step-2b--deploy-vm-wakeword-1-per-casa-opzionale)

### STEP 5 — Avvia AI Agent + stack Docker

```bash
# 1. Verifica che Tailscale sia connesso (host-level, gia attivo dal boot)
tailscale status

# 2. Avvia AI Agent (secondo la documentazione del tuo agent)
# Esempio con systemd:
sudo systemctl start ai-agent

# 3. Verifica che AI Agent sia attivo su loopback
curl http://127.0.0.1:18789/health

# 4. Avvia lo stack Docker (orchestrator + ontology-server)
cd /opt/jarvis/cloud
docker compose -f docker-compose.cloud.yml up -d
```

### STEP 6 — Verifica

```bash
# AI Agent healthy? (bare-metal, loopback)
curl http://127.0.0.1:18789/health

# Tailscale connesso alla tailnet? (host-level)
tailscale status

# Ontology Server healthy?
curl http://127.0.0.1:8100/health

# Orchestrator healthy?
curl http://localhost:5000/health

# HA raggiungibile?
curl -s -H "Authorization: Bearer <HASS_TOKEN>" \
  http://100.x.x.x:8123/api/ | head -c 100

# Wakeword server raggiungibile via Tailscale? (se deployato)
curl http://<TAILSCALE_IP_WAKEWORD>:8200/health

# Logs in tempo reale
docker compose -f docker-compose.cloud.yml logs -f   # Docker (orchestrator)
```

### STEP 7 — Telegram webhook

Il webhook Telegram e gestito da **AI Agent** (non dall'orchestrator).
Configura il webhook del bot AI Agent puntando al tuo dominio:

```bash
curl "https://api.telegram.org/bot<AI_AGENT_TELEGRAM_BOT_TOKEN>/setWebhook?url=https://<tuo-dominio>/telegram_webhook"
```

### STEP 7b — Exec Approvals (bottoni Telegram)

L'orchestrator si connette al gateway AI Agent via WebSocket come operator client.
Quando un agente richiede l'esecuzione di un comando, l'approval arriva come
messaggio Telegram con bottoni inline sul **JARVIS Approval Bot**.

**Prerequisiti (gia nel .env):**
- `AI_AGENT_WS_URL` — URL WebSocket TLS del gateway
- `AI_AGENT_TOKEN` — token di autenticazione gateway
- `JARVIS_APPROVAL_BOT_TOKEN` — token del secondo bot Telegram (separato da AI Agent)
- `JARVIS_APPROVAL_CHAT_ID` — chat ID per ricevere le notifiche

### STEP 8 — Nginx + SSL (certbot DNS Cloudflare)

Ci sono **due configurazioni Nginx** distinte:

| Nginx | Server | Scopo | Domini/Porte |
|-------|--------|-------|--------------|
| **A** | VPS | Proxy HTTPS pubblico per orchestratore | `jarvis.yourdomain.com:443` → `localhost:5000` |
| **B** | VPS (stesso server) | TLS termination per gateway AI Agent API/WS + dashboard | `agent.yourdomain.com:18789` → `ws://127.0.0.1:18789` |
|       |     |                                                          | `agent.yourdomain.com:443` → `http://127.0.0.1:18789` |

```bash
# Esegui lo script (da root)
sudo CLOUDFLARE_API_TOKEN=<il-tuo-token> bash /opt/jarvis/cloud/scripts/setup-nginx.sh
```

Lo script:
- Installa Nginx + certbot + plugin Cloudflare
- Configura i vhost per orchestratore e AI Agent
- Genera i certificati SSL via DNS challenge (non serve esporre porte pubbliche)
- Configura auto-renewal con deploy hook

---

## Configurazione Avanzata

### Parametri LLM (runtime, da dashboard)

I parametri LLM sono in `global_preferences` nel database. Modificabili dalla dashboard admin (`http://<vps-ip>:5000/admin`) senza riavvio:

| Chiave | Descrizione |
|--------|-------------|
| `llm_params` | Temperature, max_tokens, timeout per routing/reasoning/quick/summary/gemini |
| `default_location_id` | Location di default |
| `default_fallback_speaker` | Speaker entity_id di fallback |

### Parametri restart-required (.env)

Questi richiedono riavvio del container orchestrator (`docker compose restart orchestrator`):

```env
# Timeouts (secondi)
AI_AGENT_TIMEOUT=30
TAILSCALE_TIMEOUT_REMOTE=15.0
TAILSCALE_TIMEOUT_LOCAL=10.0

# Intervalli
INTERVAL_CLEANUP=1800
INTERVAL_HEALTH_CHECK=30
INTERVAL_PROACTIVE=300

# Context budget (token)
CTX_ROUTING_USER_SQL=300
CTX_REASONING_USER_SQL=800

# Speaker ID
VOICE_SIMILARITY_THRESHOLD=0.75

# Memory jobs
MEMORY_HOURLY_MINUTE=5
MEMORY_DAILY_HOUR=3
```

### Prompt templates

I prompt di sistema sono in `jarvis-orchestrator/prompts/`. Modificabili senza toccare il codice:

```
prompts/
  quick_response_system.txt     # System prompt per chat semplice
  user_hourly_summary.txt       # Prompt per summary orario
  user_daily_summary.txt        # Prompt per summary giornaliero
```

Per ricaricare senza riavvio:
```bash
curl -X POST http://localhost:5000/admin/prompts/reload
```

### Speaker Interno (AtomS3R Mobile)

Un AtomS3R con batteria puo essere configurato per usare lo speaker integrato (ES8311)
invece di un media_player HA esterno. L'orchestrator genera il TTS con Edge TTS
(`it-IT-ElsaNeural`), lo converte in frame Opus e li invia via WebSocket al device.

**Nessuna modifica al docker-compose**: il Dockerfile dell'orchestrator include gia
`ffmpeg`, `libopus-dev` e `edge-tts` (in requirements.txt). Il flusso e interamente
gestito dal container orchestrator esistente.

**Configurazione**: Dashboard orchestrator → Dispositivi → checkbox "Speaker Interno".

---

## Costi Stimati (uso personale)

| Servizio | Costo |
|----------|-------|
| Cloud LLM | ~$0 (free tier generoso) |
| Groq Whisper | ~$0 (free tier 14k min/mese) |
| OpenRouter Qwen 2.5 3B | ~$0.001/richiesta |
| VPS Hetzner CX22 | ~4/mese |
| Tailscale | Gratis (piano personal, fino a 100 nodi) |

---

## Servizi e Porte

| Porta | Servizio | Accesso |
|-------|----------|---------|
| 5000 | Orchestrator + Admin UI | Pubblico (dietro nginx) |
| 8000 | ChromaDB (shared vector store) | Solo localhost (Docker, 127.0.0.1 bind) |
| 8100 | Ontology Server (Knowledge Graph) | Solo localhost (Docker, 127.0.0.1 bind) |
| 18789 | AI Agent (bare-metal) | Solo localhost + Tailscale (NO Docker, NO internet) |
| 18800 | Chrome CDP (headless) | Solo localhost (browser-dom plugin) |
| 41641/udp | Tailscale NAT traversal | WAN (host-level, servizio systemd) |

---

## Monitoring

```bash
# Stato Ontology Server (Docker)
docker inspect --format='{{.State.Health.Status}}' jarvis_ontology
curl -s http://127.0.0.1:8100/health
docker compose -f docker-compose.cloud.yml logs --tail=20 ontology-server

# Stato AI Agent (systemd)
# Adatta il nome del servizio al tuo agent
systemctl status ai-agent
journalctl -u ai-agent -f --no-pager

# Stato Tailscale (host-level, systemd)
tailscale status
systemctl status tailscaled
journalctl -u tailscaled -f --no-pager

# Stato container Docker
docker compose -f docker-compose.cloud.yml ps

# Logs di un servizio Docker
docker compose -f docker-compose.cloud.yml logs -f orchestrator

# Health dei servizi interni
curl http://localhost:5000/health/services

# Risorse container Docker
docker stats

# Wakeword server raggiungibile? (via Tailscale, se deployato)
curl http://<TAILSCALE_IP_WAKEWORD>:8200/health
curl http://<TAILSCALE_IP_WAKEWORD>:8200/api/devices
```

---

## Troubleshooting

### Ontology Server non risponde

```bash
# Verifica stato container
docker compose -f docker-compose.cloud.yml ps ontology-server
docker compose -f docker-compose.cloud.yml logs --tail=30 ontology-server

# Healthcheck
curl -v http://127.0.0.1:8100/health

# Se il DB e corrotto, il volume persiste — basta ricreare il container
docker compose -f docker-compose.cloud.yml restart ontology-server

# Se hai configurato ONTOLOGY_API_TOKEN, verifica che il token sia corretto
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8100/entities?limit=1
```

### L'orchestrator non raggiunge AI Agent

```bash
# Dal container orchestrator (network_mode: host, usa localhost)
docker exec jarvis_orchestrator curl -v http://localhost:18789/health

# Verifica che AI Agent sia attivo sulla porta 18789
curl http://localhost:18789/health
```

### Tailscale non si connette

```bash
# Controlla lo stato (host-level)
tailscale status

# Logs del servizio systemd
journalctl -u tailscaled -e --no-pager

# Se disconnesso, riautentica
sudo tailscale up --hostname=jarvis-cloud

# Verifica che il servizio sia attivo
systemctl status tailscaled
```

### HA non raggiungibile

```bash
# Ping via Tailscale (host-level)
tailscale ping 100.x.x.x

# Curl direttamente (l'orchestrator usa network_mode: host)
curl -H "Authorization: Bearer $TOKEN" \
  http://100.x.x.x:8123/api/
```

### API timeout

Le API esterne possono avere latenza variabile:
- Aumenta `AI_AGENT_TIMEOUT` nel `.env`
- Verifica status: [status.groq.com](https://status.groq.com), [openrouter.ai](https://openrouter.ai)

### Container non parte

```bash
docker compose -f docker-compose.cloud.yml logs <servizio>
docker stats  # RAM esaurita?
```

### Memory issues

Se il VPS esaurisce la RAM:
```bash
# Verifica swap
free -h

# Aggiungi swap se serve
fallocate -l 2G /swapfile
chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

### Database corrotto

```bash
# Backup
cp /opt/jarvis/data/jarvis_state.db /opt/jarvis/data/jarvis_state.db.bak

# Il DB viene ricreato automaticamente al riavvio se mancante
docker compose -f docker-compose.cloud.yml restart orchestrator
```

---

## Aggiornamenti

AI Agent, Tailscale e lo stack Docker si aggiornano separatamente:

```bash
# 1. Aggiorna Tailscale (host-level)
sudo apt update && sudo apt install -y tailscale
tailscale status

# 2. Aggiorna AI Agent (secondo la documentazione del tuo agent)
# Riavvia il servizio dopo l'aggiornamento

# 3. Aggiorna JARVIS (orchestrator + config + skill)
cd /opt/jarvis
git pull
cd cloud
docker compose -f docker-compose.cloud.yml down
docker compose -f docker-compose.cloud.yml build --no-cache
docker compose -f docker-compose.cloud.yml up -d

# 4. Se SKILL.md o skill.json sono cambiati, ricopiali per AI Agent
cp /opt/jarvis/jarvis-orchestrator/skill/SKILL.md <agent-skills-dir>/jarvis-orchestrator/
cp /opt/jarvis/jarvis-orchestrator/skill/skill.json <agent-skills-dir>/jarvis-orchestrator/
cp -r /opt/jarvis/ontology-server/skill/* <agent-skills-dir>/ontology/
# Riavvia AI Agent per ricaricare le skill

# 5. Aggiorna wakeword-server (sul Proxmox HOST, non sul VPS)
# pct exec <CT_ID> -- bash -c '
#   cd /opt/jarvis-wakeword && git pull --depth 1
#   cd wakeword-server && docker compose up -d --build
# '
```

> **Nota**: l'aggiornamento di AI Agent non richiede rebuild Docker. L'aggiornamento Docker non tocca AI Agent. L'aggiornamento di Tailscale non tocca ne Docker ne AI Agent. Se cambiano solo file Python dell'orchestrator, basta rebuild Docker. Se cambia la skill definition, serve anche la copia + restart AI Agent. Il database ontology (`graph.db`) persiste nel volume Docker `ontology_data` ed e indipendente dal rebuild. Il wakeword-server si aggiorna direttamente sul Proxmox host (non sul VPS).

---

## Transizione Cloud → Locale

Dopo 2 settimane di testing cloud, per passare al deploy locale (con GPU):

1. Esporta i database: `cp data/jarvis_state.db ~/jarvis_backup.db` e `docker cp jarvis_ontology:/app/data/graph.db ~/ontology_backup.db`
2. Sul server locale, segui la guida in [`infrastructure/README.md`](../infrastructure/README.md)
3. Copia i database: `cp ~/jarvis_backup.db data/jarvis_state.db` e copia `ontology_backup.db` nel volume ontology
4. Cambia `AI_BACKEND=local` nel `.env` locale
5. Spegni il VPS cloud

La transizione e trasparente: il database, le location, gli utenti, le voci enrollate e la memoria sono tutti nel file SQLite.
