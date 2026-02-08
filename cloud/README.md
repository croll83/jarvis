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
              |  ~/.openclaw/workspace/skills/jarvis-orchestrator/ (copied)    |
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

Per setup **headless** (consigliato), genera prima un auth key Tailscale:

1. Vai su [login.tailscale.com/admin/settings/keys](https://login.tailscale.com/admin/settings/keys)
2. Clicca **Generate auth key**
3. Seleziona **Reusable** (opzionale), **NOT ephemeral**
4. Copia la chiave (`tskey-auth-...`)

```bash
ssh root@<vps-ip>

# Con auth key (headless — Tailscale si connette automaticamente)
export TAILSCALE_AUTHKEY=tskey-auth-xxxxxxxxxxxx
bash /opt/jarvis/cloud/scripts/setup-vps.sh

# Oppure senza auth key (dovrai connettere Tailscale manualmente dopo)
bash /opt/jarvis/cloud/scripts/setup-vps.sh
```

Lo script esegue 8 step:

1. Aggiornamento sistema
2. Installazione Docker + Compose
3. Installazione Node.js 22 + OpenClaw (`npm install -g openclaw`)
4. **Installazione e connessione Tailscale** (headless con auth key, o istruzioni per connessione manuale)
5. Installazione Nginx + Certbot
6. Creazione utente `jarvis` (con gruppo docker + sudo)
7. Creazione directory (`/opt/jarvis`, `~/.openclaw/workspace/skills`, ecc.) + tool utili
8. Configurazione firewall (SSH/HTTP/HTTPS/Tailscale UDP), swap 2GB, log rotation Docker, **servizio systemd OpenClaw**

> **Tailscale gira host-level** — installato come servizio di sistema (systemd), non in Docker. L'auth key serve solo la prima volta.
> **OpenClaw gira bare-metal** — installato globalmente via npm, gestito da systemd.

Se non hai usato l'auth key, connetti Tailscale manualmente:

```bash
sudo tailscale up --hostname=jarvis-cloud
# Apri il link stampato nel browser per autenticare
tailscale status   # verifica connessione
```

### STEP 2 — Clone repository

```bash
su - jarvis
git clone https://github.com/croll83/jarvis.git /opt/jarvis
```

### STEP 3 — Copia skill per OpenClaw

```bash
mkdir -p ~/.openclaw/workspace/skills/jarvis-orchestrator
cp /opt/jarvis/jarvis-orchestrator/skill/SKILL.md ~/.openclaw/workspace/skills/jarvis-orchestrator/
cp /opt/jarvis/jarvis-orchestrator/skill/skill.json ~/.openclaw/workspace/skills/jarvis-orchestrator/
```

Questo rende la skill JARVIS visibile a OpenClaw. Dopo ogni `git pull` che modifica la skill, riesegui i `cp`.

> **Nota**: non usare symlink — il file watcher di OpenClaw va in ELOOP con i link simbolici.

### STEP 4 — OpenClaw onboarding

```bash
openclaw onboard
```

Il wizard interattivo configura:
- **Identita**: nome dell'istanza, descrizione
- **API key Gemini**: la chiave per Gemini 3 Pro
- **Gateway token**: il token per l'autenticazione skill (salvalo, servira nel `.env`)
- **Telegram bot**: token del bot OpenClaw da @BotFather
- **Skill discovery**: rileva automaticamente `jarvis-orchestrator` dalla directory copiata

> **IMPORTANTE**: il `OPENCLAW_GATEWAY_TOKEN` nel `.env` DEVE essere lo stesso valore usato durante `openclaw onboard`.

### STEP 4b — Configura bind OpenClaw su Tailscale

OpenClaw di default ascolta solo su `localhost`. Per renderlo raggiungibile dall'orchestrator (che usa `network_mode: host`) tramite l'IP Tailscale, configura il bind:

```bash
# Verifica il tuo IP Tailscale
tailscale ip -4
# Output esempio: 100.100.74.71

# Modifica il config di OpenClaw
nano ~/.openclaw/config.json5
```

Imposta `bind` a `tailnet` (richiede che `auth.token` sia gia configurato dall'onboarding):

```json5
{
  gateway: {
    bind: "tailnet",
    // ... auth e altro gia configurato dall'onboard
  }
}
```

Poi riavvia OpenClaw:

```bash
sudo systemctl restart openclaw

# Verifica che ascolti sull'IP Tailscale
ss -tlnp | grep 18789
# Deve mostrare: 100.x.x.x:18789
```

> **Nota**: con `bind: "tailnet"`, OpenClaw NON ascolta su `localhost:18789` ma sull'IP Tailscale. L'orchestrator deve puntare a quell'IP nel `.env`.

### STEP 5 — Configura .env

```bash
cd /opt/jarvis/cloud
cp .env.example .env
nano .env
```

Recupera prima l'IP Tailscale del VPS (serve per `OPENCLAW_URL`):

```bash
tailscale ip -4
# Output esempio: 100.100.74.71
```

Variabili obbligatorie da compilare:

| Variabile | Come ottenerla |
|-----------|----------------|
| `OPENCLAW_URL` | `http://<IP-TAILSCALE-VPS>:18789` (es: `http://100.100.74.71:18789`) — usa `tailscale ip -4` |
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

# 3. Verifica che OpenClaw sia attivo (bind=tailnet → usa IP Tailscale)
curl http://$(tailscale ip -4):18789/health

# 4. Avvia lo stack Docker (solo orchestrator, con network_mode: host)
cd /opt/jarvis/cloud
docker compose -f docker-compose.cloud.yml up -d
```

### STEP 7 — Verifica

```bash
# OpenClaw healthy? (bare-metal, bind=tailnet → IP Tailscale)
curl http://$(tailscale ip -4):18789/health

# Tailscale connesso alla tailnet? (host-level)
tailscale status

# Orchestrator healthy?
curl http://localhost:5000/health

# L'orchestrator raggiunge OpenClaw? (network_mode: host, via IP Tailscale)
curl -s http://$(tailscale ip -4):18789/health

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

# Verifica che la skill sia copiata
ls -la ~/.openclaw/workspace/skills/jarvis-orchestrator/
# deve mostrare: SKILL.md e skill.json

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
