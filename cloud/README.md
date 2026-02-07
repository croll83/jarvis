# JARVIS — Deploy Cloud (VPS senza GPU)

Guida completa per il deploy di JARVIS su un VPS. Nessuna GPU richiesta: AI via API esterne
(Gemini 3 Pro via OpenClaw, Groq per STT, OpenRouter per routing).
Tailscale gira come container Docker per raggiungere Home Assistant.

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
              |  docker-compose.cloud.yml                           |
              |  +-----------------------------------------------+ |
              |  |              jarvis_cloud                      | |
              |  |                                                | |
              |  |  tailscale ──► orchestrator ──► openclaw       | |
              |  |  (VPN mesh)    :5000 (FastAPI)   :18789        | |
              |  |                AI_BACKEND=api    Gemini 3 Pro  | |
              |  +-----------------------------------------------+ |
              +--------+-------------------------------------------+
                       | Tailscale (100.x.x.x)
                       v
              +-------------------+        +-------------------+
              | HA Wagmi (Napoli) |        | HA Albani (Milano)|
              | 100.x.x.x:8123   |        | 100.x.x.x:8123   |
              +-------------------+        +-------------------+
```

### Ordine di boot dei container

```
1. tailscale       → si connette alla tailnet, diventa healthy
2. orchestrator    → aspetta tailscale healthy, poi parte
3. openclaw        → aspetta orchestrator healthy, poi parte
```

Docker Compose gestisce tutto automaticamente con `depends_on` + `service_healthy`.

---

## Quick Setup (Step-by-Step)

### Prerequisiti

- VPS con Ubuntu 22.04/24.04 LTS, 2+ vCPU, 4+ GB RAM
- Accesso root via SSH (IP pubblico)
- API keys pronte: Gemini, Groq, OpenRouter
- Tailscale auth key: [login.tailscale.com/admin/settings/keys](https://login.tailscale.com/admin/settings/keys)
- HA con Tailscale add-on attivo (o Tailscale host-level sulla macchina HA)

### STEP 1 — Setup VPS (~3 minuti)

```bash
ssh root@<vps-ip>
curl -fsSL https://raw.githubusercontent.com/croll83/jarvis/main/cloud/scripts/setup-vps.sh | bash
```

Lo script installa: Docker + Compose, Nginx + Certbot, utente `jarvis`, firewall (SSH/HTTP/HTTPS/Tailscale UDP), 2GB swap, log rotation Docker.

> **Tailscale NON viene installato sull'host** — gira come container Docker.

### STEP 2 — Clone repository

```bash
su - jarvis
git clone https://github.com/croll83/jarvis.git /opt/jarvis
```

### STEP 3 — Configura .env

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
| `OPENCLAW_GATEWAY_TOKEN` | `openssl rand -hex 32` |
| `OPENCLAW_TELEGRAM_BOT_TOKEN` | @BotFather su Telegram |
| `JARVIS_APPROVAL_BOT_TOKEN` | @BotFather (secondo bot, separato) |
| `JARVIS_APPROVAL_CHAT_ID` | Scrivi al bot, poi `curl https://api.telegram.org/bot<TOKEN>/getUpdates` |
| `TAILSCALE_AUTHKEY` | [login.tailscale.com/admin/settings/keys](https://login.tailscale.com/admin/settings/keys) — reusable + ephemeral |
| `HASS_URL` | `http://100.x.x.x:8123` (IP Tailscale del tuo HA, senza `/api`) |
| `JARVIS_HASS_TOKEN` | HA → Profilo → Token di lunga durata |
| `JWT_SECRET` | `openssl rand -hex 32` |
| `JARVIS_TELEGRAM_TOKEN` | @BotFather (bot principale notifiche) |
| `JARVIS_TELEGRAM_CHAT_ID` | Il tuo chat ID Telegram |

### STEP 4 — Avvia lo stack

```bash
docker compose -f docker-compose.cloud.yml up -d
```

### STEP 5 — Verifica

```bash
# Tailscale connesso alla tailnet?
docker exec jarvis_tailscale tailscale status

# Orchestrator healthy?
curl http://localhost:5000/health

# OpenClaw healthy?
curl http://localhost:18789/health

# HA raggiungibile dal container?
docker exec jarvis_orchestrator curl -s \
  -H "Authorization: Bearer <HASS_TOKEN>" \
  http://100.x.x.x:8123/api/ | head -c 100

# Logs in tempo reale
docker compose -f docker-compose.cloud.yml logs -f
```

### STEP 6 — Telegram webhook (opzionale, se usi Telegram diretto)

```bash
curl "https://api.telegram.org/bot<JARVIS_TELEGRAM_TOKEN>/setWebhook?url=https://<tuo-dominio>/telegram_webhook"
```

### STEP 7 — SSL con Nginx (dopo aver configurato il DNS)

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

Questi richiedono riavvio del container:

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
| 18789 | OpenClaw | Solo interno (Docker) |
| 41641/udp | Tailscale NAT traversal | WAN (gestito dal container) |

---

## Monitoring

```bash
# Stato di tutti i container
docker compose -f docker-compose.cloud.yml ps

# Logs di un servizio
docker compose -f docker-compose.cloud.yml logs -f orchestrator

# Health dei servizi interni
curl http://localhost:5000/health/services

# Risorse
docker stats
```

---

## Troubleshooting

### Tailscale non si connette

```bash
# Controlla lo stato
docker exec jarvis_tailscale tailscale status

# Se il container non parte, verifica la TAILSCALE_AUTHKEY
docker compose -f docker-compose.cloud.yml logs tailscale

# Se la key e scaduta, generane una nuova su login.tailscale.com
```

### HA non raggiungibile

```bash
# Ping dal container Tailscale
docker exec jarvis_tailscale tailscale ping 100.x.x.x

# Curl dal container orchestrator
docker exec jarvis_orchestrator curl -H "Authorization: Bearer $TOKEN" \
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

```bash
cd /opt/jarvis
git pull
cd cloud
docker compose -f docker-compose.cloud.yml down
docker compose -f docker-compose.cloud.yml build --no-cache
docker compose -f docker-compose.cloud.yml up -d
```

---

## Transizione Cloud → Locale

Dopo 2 settimane di testing cloud, per passare al deploy locale (con GPU):

1. Esporta il database: `cp data/jarvis_state.db ~/jarvis_backup.db`
2. Sul server locale, segui la guida in [`infrastructure/README.md`](../infrastructure/README.md)
3. Copia il database: `cp ~/jarvis_backup.db data/jarvis_state.db`
4. Cambia `AI_BACKEND=local` nel `.env` locale
5. Spegni il VPS cloud

La transizione e trasparente: il database, le location, gli utenti, le voci enrollate e la memoria sono tutti nel file SQLite.
