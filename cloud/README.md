# JARVIS — Deploy Cloud (VPS senza GPU)

Guida completa per il deploy di JARVIS su un VPS. Nessuna GPU richiesta: AI via API esterne
(Gemini 3 Pro via OpenClaw, Groq per STT, OpenRouter per routing).
Tailscale gira host-level (servizio di sistema, NON in Docker) per raggiungere Home Assistant.
**OpenClaw gira bare-metal** (Node.js, non in Docker) sulla stessa macchina.

---

## Architettura

```
                    Internet
                        |
                        v
              +-------------------+
              |   Nginx + SSL     |
              |  (certbot)        |
              +--------+----------+
                       | :5000
              +--------v-------------------------------------------+
              |  VPS (Hetzner / Contabo / simile)                  |
              |  2 vCPU, 4 GB RAM, 40 GB SSD                      |
              |                                                     |
              |  OpenClaw (bare-metal, systemd)                     |
              |  :18789 — Gemini 3 Pro — Telegram bot               |
              |  ~/.openclaw/skills/jarvis-orchestrator -> skill/    |
              |                                                     |
              |  Tailscale (host-level, systemd)                    |
              |  VPN mesh — 100.x.x.x                              |
              |                                                     |
              |  docker-compose.cloud.yml                           |
              |  +-----------------------------------------------+ |
              |  |              jarvis_cloud                      | |
              |  |                                                | |
              |  |  orchestrator (network_mode: host)             | |
              |  |  :5000 (FastAPI)                               | |
              |  |  AI_BACKEND=api                                | |
              |  +-----------------------------------------------+ |
              +--------+-------------------------------------------+
                       | Tailscale (100.x.x.x)
                       v
              +-------------------+        +-------------------+
              | HA Wagmi (Napoli) |        | HA Albani (Milano)|
              | 100.x.x.x:8123   |        | 100.x.x.x:8123   |
              +-------------------+        +-------------------+
```

### Ordine di boot

```
1. tailscale (systemd)  → servizio host-level, parte al boot del VPS, si connette alla tailnet
2. openclaw (systemd)   → servizio bare-metal, parte al boot del VPS
3. orchestrator (Docker) → network_mode: host, vede Tailscale direttamente
                           raggiunge OpenClaw via localhost:18789
```

Tailscale e OpenClaw sono processi systemd che partono prima di Docker.
L'orchestrator usa `network_mode: host`, quindi vede la rete dell'host direttamente (Tailscale, localhost, ecc.).

---

## Quick Setup (Step-by-Step)

### Prerequisiti

- VPS con Ubuntu 22.04/24.04 LTS, 2+ vCPU, 4+ GB RAM
- Accesso root via SSH (IP pubblico)
- API keys pronte: Gemini, Groq, OpenRouter
- Account Tailscale: [login.tailscale.com](https://login.tailscale.com)
- HA con Tailscale add-on attivo (o Tailscale host-level sulla macchina HA)

### STEP 1 — Setup VPS (~3 minuti)

```bash
ssh root@<vps-ip>
curl -fsSL https://raw.githubusercontent.com/croll83/jarvis/main/cloud/scripts/setup-vps.sh | bash
```

Lo script esegue 7 step:

1. Aggiornamento sistema
2. Installazione Docker + Compose
3. Installazione Node.js 22 + OpenClaw (`npm install -g openclaw`)
4. Installazione Nginx + Certbot
5. Creazione utente `jarvis` (con gruppo docker)
6. Creazione directory (`/opt/jarvis`, `~/.openclaw/skills`, ecc.) + tool utili
7. Configurazione firewall (SSH/HTTP/HTTPS/Tailscale UDP), swap 2GB, log rotation Docker, **servizio systemd OpenClaw**

> **Tailscale gira host-level** — installato come servizio di sistema (systemd), non in Docker.
> **OpenClaw gira bare-metal** — installato globalmente via npm, gestito da systemd.

### STEP 1b — Installa e autentica Tailscale (host-level)

```bash
# Installa Tailscale
curl -fsSL https://tailscale.com/install.sh | sh

# Autentica e imposta hostname
sudo tailscale up --hostname=jarvis-cloud

# Verifica connessione alla tailnet
tailscale status
```

> **Nota**: l'autenticazione avviene interattivamente via browser (il comando stampa un URL da aprire). Non serve auth key nel `.env`.

### STEP 2 — Clone repository

```bash
su - jarvis
git clone https://github.com/croll83/jarvis.git /opt/jarvis
```

### STEP 3 — Symlink skill per OpenClaw

```bash
ln -s /opt/jarvis/jarvis-orchestrator/skill ~/.openclaw/skills/jarvis-orchestrator
```

Questo rende la skill JARVIS visibile a OpenClaw. La directory `~/.openclaw/skills/` contiene symlink alle skill installate.

### STEP 4 — OpenClaw onboarding

```bash
openclaw onboard
```

Il wizard interattivo configura:
- **Identita**: nome dell'istanza, descrizione
- **API key Gemini**: la chiave per Gemini 3 Pro
- **Gateway token**: il token per l'autenticazione skill (salvalo, servira nel `.env`)
- **Telegram bot**: token del bot OpenClaw da @BotFather
- **Skill discovery**: rileva automaticamente `jarvis-orchestrator` dal symlink

> **IMPORTANTE**: il `OPENCLAW_GATEWAY_TOKEN` nel `.env` DEVE essere lo stesso valore usato durante `openclaw onboard`.

### STEP 5 — Configura .env

```bash
cd /opt/jarvis/cloud
cp .env.example .env
nano .env
```

Variabili obbligatorie da compilare:

| Variabile | Come ottenerla |
|-----------|----------------|
| `GEMINI_API_KEY` | [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey) |
| `GROQ_API_KEY` | [console.groq.com/keys](https://console.groq.com/keys) |
| `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) |
| `OPENCLAW_GATEWAY_TOKEN` | Stesso valore usato in `openclaw onboard` |
| `OPENCLAW_TELEGRAM_BOT_TOKEN` | @BotFather su Telegram |
| `JARVIS_APPROVAL_BOT_TOKEN` | @BotFather (secondo bot, separato) |
| `JARVIS_APPROVAL_CHAT_ID` | Scrivi al bot, poi `curl https://api.telegram.org/bot<TOKEN>/getUpdates` |
| `HASS_URL` | `http://100.x.x.x:8123` (IP Tailscale del tuo HA, senza `/api`) |
| `JARVIS_HASS_TOKEN` | HA → Profilo → Token di lunga durata |

> **Nota**: OpenClaw gira bare-metal, NON in Docker. Tailscale gira host-level, NON in Docker. Il `.env` viene letto solo dal container Docker (orchestrator). OpenClaw ha la sua configurazione in `~/.openclaw/`. Tailscale si autentica con `tailscale up --hostname=jarvis-cloud`.

### STEP 6 — Avvia OpenClaw + stack Docker

```bash
# 1. Verifica che Tailscale sia connesso (host-level, gia attivo dal boot)
tailscale status

# 2. Avvia OpenClaw (systemd)
sudo systemctl start openclaw

# 3. Verifica che OpenClaw sia attivo
curl http://localhost:18789/health

# 4. Avvia lo stack Docker (solo orchestrator, con network_mode: host)
cd /opt/jarvis/cloud
docker compose -f docker-compose.cloud.yml up -d
```

### STEP 7 — Verifica

```bash
# OpenClaw healthy? (bare-metal, porta 18789)
curl http://localhost:18789/health

# Tailscale connesso alla tailnet? (host-level)
tailscale status

# Orchestrator healthy?
curl http://localhost:5000/health

# L'orchestrator raggiunge OpenClaw via localhost? (network_mode: host)
docker exec jarvis_orchestrator curl -s http://localhost:18789/health

# HA raggiungibile?
curl -s -H "Authorization: Bearer <HASS_TOKEN>" \
  http://100.x.x.x:8123/api/ | head -c 100

# Logs in tempo reale
docker compose -f docker-compose.cloud.yml logs -f   # Docker (orchestrator)
journalctl -u openclaw -f                             # OpenClaw (systemd)
journalctl -u tailscaled -f                           # Tailscale (systemd)
```

### STEP 8 — Telegram webhook

Il webhook Telegram e gestito da **OpenClaw** (non dall'orchestrator).
Configura il webhook del bot OpenClaw puntando al tuo dominio:

```bash
curl "https://api.telegram.org/bot<OPENCLAW_TELEGRAM_BOT_TOKEN>/setWebhook?url=https://<tuo-dominio>/telegram_webhook"
```

### STEP 9 — SSL con Nginx (dopo aver configurato il DNS)

```bash
# Da root
sudo cp /opt/jarvis/cloud/nginx/jarvis.conf /etc/nginx/sites-available/
sudo ln -s /etc/nginx/sites-available/jarvis.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d jarvis.tuodominio.it
```

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
OPENCLAW_TIMEOUT=30
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
  gemini_verification.txt       # Prompt per verifica Gemini
  user_hourly_summary.txt       # Prompt per summary orario
  user_daily_summary.txt        # Prompt per summary giornaliero
```

Per ricaricare senza riavvio:
```bash
curl -X POST http://localhost:5000/admin/prompts/reload
```

### Dashboard OpenClaw

OpenClaw espone una dashboard web accessibile su:

- **Locale** (dal VPS): `http://localhost:18789`
- **Via Tailscale** (da altri dispositivi sulla tailnet): `http://jarvis-cloud:18789` oppure `http://100.x.x.x:18789`

La porta 18789 NON e esposta su internet (non e in Docker, e un processo locale). E raggiungibile solo da localhost o via Tailscale.

---

## Costi Stimati (uso personale)

| Servizio | Costo |
|----------|-------|
| Gemini 3 Pro | ~$0 (free tier generoso) |
| Groq Whisper | ~$0 (free tier 14k min/mese) |
| OpenRouter Qwen 7B | ~$0.001/richiesta |
| VPS Hetzner CX22 | ~€4/mese |
| Tailscale | Gratis (piano personal, fino a 100 nodi) |

---

## Servizi e Porte

| Porta | Servizio | Accesso |
|-------|----------|---------|
| 5000 | Orchestrator + Admin UI | Pubblico (dietro nginx) |
| 18789 | OpenClaw (bare-metal) | Solo localhost + Tailscale (NO Docker, NO internet) |
| 41641/udp | Tailscale NAT traversal | WAN (host-level, servizio systemd) |

---

## Monitoring

```bash
# Stato OpenClaw (systemd)
systemctl status openclaw
journalctl -u openclaw -f --no-pager

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

# Risorse OpenClaw (bare-metal)
ps aux | grep openclaw
```

---

## Troubleshooting

### OpenClaw non parte

```bash
# Stato del servizio systemd
systemctl status openclaw

# Logs dettagliati
journalctl -u openclaw -e --no-pager

# Verifica che Node.js sia installato
node -v  # deve essere >= 22

# Verifica che openclaw sia installato globalmente
openclaw --version

# Verifica che la skill sia linkata
ls -la ~/.openclaw/skills/
# deve mostrare: jarvis-orchestrator -> /opt/jarvis/jarvis-orchestrator/skill

# Riavvia il servizio
sudo systemctl restart openclaw
```

### L'orchestrator non raggiunge OpenClaw

```bash
# Dal container orchestrator (network_mode: host, usa localhost)
docker exec jarvis_orchestrator curl -v http://localhost:18789/health

# Verifica che OpenClaw sia attivo sulla porta 18789
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

# Verifica che l'HA abbia Tailscale attivo e sia sulla stessa tailnet
```

### API timeout

Le API esterne possono avere latenza variabile:
- Aumenta `OPENCLAW_TIMEOUT` nel `.env`
- Verifica status: [status.groq.com](https://status.groq.com), [openrouter.ai](https://openrouter.ai)

### Container non parte

```bash
docker compose -f docker-compose.cloud.yml logs <servizio>
docker stats  # RAM esaurita?
```

### Memory issues

Se il VPS esaurisce la RAM (lo script setup crea gia 2GB swap):
```bash
# Verifica swap
free -h

# Aggiungi piu swap se serve
fallocate -l 4G /swapfile2
chmod 600 /swapfile2
mkswap /swapfile2
swapon /swapfile2
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

OpenClaw, Tailscale e lo stack Docker si aggiornano separatamente:

```bash
# 1. Aggiorna Tailscale (host-level)
sudo apt update && sudo apt install -y tailscale
tailscale status

# 2. Aggiorna OpenClaw (bare-metal)
sudo npm update -g openclaw
sudo systemctl restart openclaw
curl http://localhost:18789/health

# 3. Aggiorna JARVIS (orchestrator + config)
cd /opt/jarvis
git pull
cd cloud
docker compose -f docker-compose.cloud.yml down
docker compose -f docker-compose.cloud.yml build --no-cache
docker compose -f docker-compose.cloud.yml up -d
```

> **Nota**: l'aggiornamento di OpenClaw non richiede rebuild Docker. L'aggiornamento Docker non tocca OpenClaw. L'aggiornamento di Tailscale non tocca ne Docker ne OpenClaw.

---

## Transizione Cloud → Locale

Dopo 2 settimane di testing cloud, per passare al deploy locale (con GPU):

1. Esporta il database: `cp data/jarvis_state.db ~/jarvis_backup.db`
2. Sul server locale, segui la guida in [`infrastructure/README.md`](../infrastructure/README.md)
3. Copia il database: `cp ~/jarvis_backup.db data/jarvis_state.db`
4. Cambia `AI_BACKEND=local` nel `.env` locale
5. Spegni il VPS cloud

La transizione e trasparente: il database, le location, gli utenti, le voci enrollate e la memoria sono tutti nel file SQLite.
