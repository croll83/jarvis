# JARVIS — Deploy Cloud (VPS senza GPU)

Guida completa per il deploy di JARVIS su un VPS. Nessuna GPU richiesta: AI via API esterne
(Gemini 3 Pro via OpenClaw, Groq per STT, OpenRouter per routing).
Tailscale gira host-level (servizio di sistema, NON in Docker) per raggiungere Home Assistant.
**OpenClaw gira bare-metal** (Node.js, non in Docker) sulla stessa macchina.

> **NOTA:** Il wakeword-server (`jarvis/wakeword-server/`) NON va deployato su VPS cloud.
> Ogni casa ha il proprio wakeword-server su un LXC locale (stessa LAN degli AtomS3R).
> Il VPS riceve solo il relay audio post-wake tramite Tailscale.

> **TODO:**
> - Aggiungere conf Nginx per webhook Telegram (jarvis-pub.mintwork.it → IP pubblico VPS)
> - Aggiungere conf Nginx per AtomS3R (endpoint accessibile da rete locale/Tailscale)
> - Migrare Approval Bot da long-polling a webhook una volta configurato il DNS pubblico

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
              |  |  orchestrator (network_mode: host)             | |
              |  |  :5000 (FastAPI) — AI_BACKEND=api              | |
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
              |  LAN :8200 ← AtomS3R devices (WiFi)       |
              +-------------------------------------------+
```

> **Wakeword server**: il VPS raggiunge i wakeword-server locali via Tailscale per push
> di configurazione (`POST /api/config/{device_id}`) e trigger_listen
> (`POST /api/trigger_listen/{device_id}`). Il relay audio avviene in direzione opposta:
> il wakeword-server apre un WebSocket on-demand verso il VPS solo quando rileva un wake word.

### Ordine di boot

```
1. tailscale (systemd)     → servizio host-level, parte al boot del VPS, si connette alla tailnet
2. openclaw (systemd)      → servizio bare-metal, parte al boot del VPS
3. ontology-server (Docker) → Knowledge Graph API, 127.0.0.1:8100
4. orchestrator (Docker)    → network_mode: host, vede Tailscale direttamente
                              raggiunge OpenClaw via localhost:18789
                              raggiunge ontology via localhost:8100
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
# Skill orchestrator (domotica, TTS, security, memory)
mkdir -p ~/.openclaw/workspace/skills/jarvis-orchestrator
cp /opt/jarvis/jarvis-orchestrator/skill/SKILL.md ~/.openclaw/workspace/skills/jarvis-orchestrator/
cp /opt/jarvis/jarvis-orchestrator/skill/skill.json ~/.openclaw/workspace/skills/jarvis-orchestrator/

# Skill ontology (knowledge graph — crea/query/relate entita)
mkdir -p ~/.openclaw/workspace/skills/ontology
cp -r /opt/jarvis/ontology-server/skill/* ~/.openclaw/workspace/skills/ontology/
```

Questo rende le skill visibili a OpenClaw. Dopo ogni `git pull` che modifica le skill, riesegui i `cp`.

La skill orchestrator espone 11 tool REST, tra cui `entity_bulk` per query/azioni di gruppo su entita HA (elimina il problema N+1 delle query multi-entita).

La skill ontology espone il Knowledge Graph API (CRUD entita, relazioni, query, bulk) con ACL basata su `X-Speaker-Id`.

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

### STEP 4a — Configura env vars della skill JARVIS

Dopo l'onboarding, esegui lo script che legge il gateway token dal config e lo inietta nella configurazione della skill:

```bash
bash /opt/jarvis/cloud/scripts/configure-openclaw-skill.sh
```

Lo script configura automaticamente `OPENCLAW_GATEWAY_TOKEN` e `JARVIS_ORCHESTRATOR_URL` in `skill.json` (le env vars nello skill hanno precedenza su `openclaw.json`).

> **IMPORTANTE**: il `OPENCLAW_GATEWAY_TOKEN` nel `.env` dell'orchestratore DEVE essere lo stesso valore. Lo trovi con:
> `jq -r '.gateway.auth.token' ~/.openclaw/openclaw.json`

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

### STEP 4c — Browser DOM Plugin (automazione browser headless)

Il plugin `browser-dom` aggiunge tool di manipolazione DOM diretta (CSS selectors, XPath, JS evaluate) via Chrome DevTools Protocol. Permette all'agente di navigare siti web, compilare form, cliccare bottoni e fare screenshot senza i problemi di stale-ref del browser tool built-in.

```bash
su - jarvis

# 1. Copia il plugin nella directory extensions di OpenClaw
cp -r /opt/jarvis/extensions/browser-dom ~/.openclaw/extensions/browser-dom
cd ~/.openclaw/extensions/browser-dom
npm install

# 2. Installa il servizio Chrome headless (gestito da systemd)
sudo cp openclaw-chrome.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-chrome

# 3. Verifica che Chrome CDP sia attivo
curl -s http://127.0.0.1:18800/json/version | head -3
# Deve mostrare: { "Browser": "Chrome/xxx", ... }

# 4. Configura il plugin in openclaw.json
# (aggiungere queste sezioni alla configurazione esistente)
python3 -c "
import json
with open('$HOME/.openclaw/openclaw.json') as f:
    cfg = json.load(f)
cfg.setdefault('browser', {})['enabled'] = True
cfg['browser']['evaluateEnabled'] = True
cfg['browser']['defaultProfile'] = 'openclaw'
cfg['browser'].setdefault('profiles', {})['openclaw'] = {'cdpPort': 18800, 'color': '#4A90D9'}
cfg.setdefault('plugins', {}).setdefault('entries', {})['browser-dom'] = {
    'enabled': True,
    'config': {'cdpUrl': 'http://127.0.0.1:18800', 'defaultTimeoutMs': 15000}
}
allow = cfg.setdefault('tools', {}).get('alsoAllow', [])
if 'group:plugins' not in allow:
    allow.append('group:plugins')
    cfg['tools']['alsoAllow'] = allow
with open('$HOME/.openclaw/openclaw.json', 'w') as f:
    json.dump(cfg, f, indent=2)
print('browser-dom plugin configured')
"

# 5. Riavvia OpenClaw
sudo systemctl restart openclaw

# 6. Verifica che il plugin sia caricato
journalctl -u openclaw --no-pager -n 10 | grep browser-dom
# Deve mostrare: [browser-dom] 8 DOM tools registered successfully.
```

> **Boot order**: `openclaw-chrome.service` parte prima di `openclaw.service` (grazie a `Before=openclaw.service`). Se Chrome non e attivo, i tool dom_* falliranno con "fetch failed".

Per maggiori dettagli, vedi: [`extensions/browser-dom/README.md`](../../extensions/browser-dom/README.md)

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
| `OPENCLAW_WS_URL` | `ws://<IP-TAILSCALE-VPS>:18789` — stesso IP di `OPENCLAW_URL` ma con `ws://` |
| `OPENCLAW_TELEGRAM_BOT_TOKEN` | @BotFather su Telegram |
| `JARVIS_APPROVAL_BOT_TOKEN` | @BotFather (secondo bot, separato da OpenClaw) |
| `JARVIS_APPROVAL_CHAT_ID` | Scrivi al bot, poi `curl https://api.telegram.org/bot<TOKEN>/getUpdates` |
| `HASS_URL` | `http://100.x.x.x:8123` (IP Tailscale del tuo HA, senza `/api`) |
| `JARVIS_HASS_TOKEN` | HA → Profilo → Token di lunga durata |
| `ONTOLOGY_API_TOKEN` | (opzionale) `openssl rand -hex 32` — protegge l'API ontology |
| `WAKEWORD_SERVER_URLS` | (opzionale) JSON map `{"location_id": "http://<TAILSCALE_IP>:8200"}` — IP Tailscale dei wakeword-server locali |

> **Nota**: OpenClaw gira bare-metal, NON in Docker. Tailscale gira host-level, NON in Docker. Il `.env` viene letto solo dal container Docker (orchestrator). OpenClaw ha la sua configurazione in `~/.openclaw/`. Tailscale si autentica con `tailscale up --hostname=jarvis-cloud`.

### STEP 5b — Deploy Wakeword Server (locale, 1 per casa)

Il wakeword-server NON gira sul VPS. Va deployato su un LXC locale (Proxmox) nella stessa
LAN degli AtomS3R. Dopo il deploy, inserisci il suo IP Tailscale nel `.env` del VPS.

```bash
# Sul Proxmox HOST (non sul VPS)
sudo bash /opt/jarvis/cloud/scripts/deploy-wakeword.sh
```

Lo script crea un LXC container con Docker + Tailscale + wakeword-server.
Al termine, riporta l'IP Tailscale da inserire in `WAKEWORD_SERVER_URLS`.

Per i dettagli, vedi: [`infrastructure/README.md` STEP 2b](../infrastructure/README.md#step-2b--deploy-vm-wakeword-1-per-casa-opzionale)

### STEP 6 — Avvia OpenClaw + stack Docker

```bash
# 1. Verifica che Tailscale sia connesso (host-level, gia attivo dal boot)
tailscale status

# 2. Avvia OpenClaw (systemd)
sudo systemctl start openclaw

# 3. Verifica che OpenClaw sia attivo (bind=tailnet → usa IP Tailscale)
curl http://$(tailscale ip -4):18789/health

# 4. Avvia lo stack Docker (orchestrator + ontology-server)
cd /opt/jarvis/cloud
docker compose -f docker-compose.cloud.yml up -d
```

### STEP 7 — Verifica

```bash
# OpenClaw healthy? (bare-metal, bind=tailnet → IP Tailscale)
curl http://$(tailscale ip -4):18789/health

# Tailscale connesso alla tailnet? (host-level)
tailscale status

# Ontology Server healthy?
curl http://127.0.0.1:8100/health

# Orchestrator healthy?
curl http://localhost:5000/health

# L'orchestrator raggiunge OpenClaw? (network_mode: host, via IP Tailscale)
curl -s http://$(tailscale ip -4):18789/health

# HA raggiungibile?
curl -s -H "Authorization: Bearer <HASS_TOKEN>" \
  http://100.x.x.x:8123/api/ | head -c 100

# Wakeword server raggiungibile via Tailscale? (se deployato)
curl http://<TAILSCALE_IP_WAKEWORD>:8200/health

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

### STEP 8b — Exec Approvals (bottoni Telegram)

L'orchestrator si connette al gateway OpenClaw via WebSocket come operator client.
Quando un agente richiede l'esecuzione di un comando, l'approval arriva come
messaggio Telegram con bottoni inline (✅ Once, 🔓 Always, ❌ Deny) sul **JARVIS Approval Bot**.

**Prerequisiti (già nel .env):**
- `OPENCLAW_WS_URL` — URL WebSocket del gateway (`ws://<IP-TAILSCALE>:18789`)
- `OPENCLAW_GATEWAY_TOKEN` — token di autenticazione gateway
- `JARVIS_APPROVAL_BOT_TOKEN` — token del secondo bot Telegram (separato da OpenClaw)
- `JARVIS_APPROVAL_CHAT_ID` — chat ID per ricevere le notifiche

**Configurazione OpenClaw** (`~/.openclaw/config.json5`):
```json5
"approvals": {
  "exec": {
    "enabled": false  // disabilita il flow testuale nativo di OpenClaw
  }
}
```

**Exec Approvals Allowlist** (`~/.openclaw/exec-approvals.json`):
```bash
# Copia il file di default (auto-approva comandi sicuri, chiede per quelli pericolosi)
cp /opt/jarvis/cloud/exec-approvals.json ~/.openclaw/
```

Il file configura `security: "allowlist"` + `ask: "on-miss"` con glob patterns per comandi sicuri
(curl, bash, node, ls, cat, ecc.) e chiede approvazione Telegram per comandi pericolosi (rm, chmod, ecc.).

> **Nota**: Il bot Approval usa **long-polling** (getUpdates) per ricevere i callback
> dai bottoni inline. Non richiede URL pubblico o webhook. Verra migrato a webhook
> quando sara configurato un DNS pubblico (jarvis-pub.mintwork.it).

### STEP 9 — Nginx + SSL (certbot DNS Cloudflare)

Prerequisiti:
1. Crea un **API Token** su [Cloudflare](https://dash.cloudflare.com/profile/api-tokens) con permesso `Zone:DNS:Edit`
2. Crea due **record A** su Cloudflare che puntano all'IP Tailscale del VPS:
   - `jarvis.mintwork.it` -> `$(tailscale ip -4)`
   - `openclaw.mintwork.it` -> `$(tailscale ip -4)`

```bash
# Esegui lo script (da root)
sudo CLOUDFLARE_API_TOKEN=<il-tuo-token> bash /opt/jarvis/cloud/scripts/setup-nginx.sh
```

Lo script:
- Installa Nginx + certbot + plugin Cloudflare
- Configura i vhost per `jarvis.mintwork.it` (orchestratore) e `openclaw.mintwork.it` (dashboard)
- Genera i certificati SSL via DNS challenge (non serve esporre porte pubbliche)
- Configura auto-renewal

Verifica:
```bash
curl -k https://jarvis.mintwork.it/health
curl -k https://openclaw.mintwork.it/health
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

### OpenClaw Environment Variables (openclaw.env)

Il servizio systemd carica variabili aggiuntive da `~/.openclaw/openclaw.env` (opzionale, creato dallo script di setup).

```bash
nano ~/.openclaw/openclaw.env
```

| Variabile | Descrizione |
|-----------|-------------|
| `GOG_KEYRING_PASSWORD` | Password per il keyring gogcli |
| `GOG_ACCOUNT` | Email dell'account GOG |

Dopo aver modificato il file, riavvia OpenClaw:
```bash
sudo systemctl restart openclaw
```

### gogcli — Configurazione Credenziali

La skill `gogcli` richiede file di credenziali nella directory `~/.config/gog/` dell'utente jarvis.

**File da copiare manualmente:**
```
~/.config/gog/
  ├── credentials.json     # Credenziali GOG (generato da gogcli login)
  └── keyring/
      └── <email>/         # Directory con i file del keyring per il tuo account
          ├── key.json
          └── ...
```

**Setup:**
```bash
# La directory viene creata dallo script di setup. Copia i file dal tuo ambiente locale:
scp -r ~/.config/gog/credentials.json jarvis@<vps-ip>:~/.config/gog/
scp -r ~/.config/gog/keyring/ jarvis@<vps-ip>:~/.config/gog/

# Verifica
ls -la ~/.config/gog/
ls -la ~/.config/gog/keyring/
```

> **Nota**: Questi file contengono credenziali sensibili. Non committarli nel repository.

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
| 8100 | Ontology Server (Knowledge Graph) | Solo localhost (Docker, 127.0.0.1 bind) |
| 18789 | OpenClaw (bare-metal) | Solo localhost + Tailscale (NO Docker, NO internet) |
| 18800 | Chrome CDP (headless) | Solo localhost (browser-dom plugin) |
| 41641/udp | Tailscale NAT traversal | WAN (host-level, servizio systemd) |

---

## Monitoring

```bash
# Stato Ontology Server (Docker)
docker inspect --format='{{.State.Health.Status}}' jarvis_ontology
curl -s http://127.0.0.1:8100/health
docker compose -f docker-compose.cloud.yml logs --tail=20 ontology-server

# Stato Chrome headless (browser-dom)
systemctl status openclaw-chrome
curl -s http://127.0.0.1:18800/json/version | head -3

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

# Verifica che la porta sia raggiungibile dall'orchestrator (network_mode: host)
docker exec jarvis_orchestrator curl -s http://127.0.0.1:8100/health 2>/dev/null || \
  curl -s http://127.0.0.1:8100/health
```

### Chrome headless / browser-dom non funziona

```bash
# Verifica che il servizio Chrome sia attivo
sudo systemctl status openclaw-chrome
journalctl -u openclaw-chrome --no-pager -n 20

# Verifica CDP
curl -s http://127.0.0.1:18800/json/version

# Se Chrome non risponde, riavvialo
sudo systemctl restart openclaw-chrome

# Verifica che il plugin sia caricato
journalctl -u openclaw --no-pager -n 20 | grep browser-dom
# Deve mostrare: [browser-dom] 8 DOM tools registered successfully.

# Se il plugin non appare, verifica che sia installato
ls -la ~/.openclaw/extensions/browser-dom/
```

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

# 3. Aggiorna JARVIS (orchestrator + config + skill)
cd /opt/jarvis
git pull
cd cloud
docker compose -f docker-compose.cloud.yml down
docker compose -f docker-compose.cloud.yml build --no-cache
docker compose -f docker-compose.cloud.yml up -d

# 4. Se SKILL.md o skill.json sono cambiati, ricopiali per OpenClaw
cp /opt/jarvis/jarvis-orchestrator/skill/SKILL.md ~/.openclaw/workspace/skills/jarvis-orchestrator/
cp /opt/jarvis/jarvis-orchestrator/skill/skill.json ~/.openclaw/workspace/skills/jarvis-orchestrator/
# NOTA: la copia sovrascrive le credenziali in skill.json — riesegui lo script:
bash /opt/jarvis/cloud/scripts/configure-openclaw-skill.sh
# Skill ontology (se cambiata)
cp -r /opt/jarvis/ontology-server/skill/* ~/.openclaw/workspace/skills/ontology/
sudo systemctl restart openclaw

# 5. Aggiorna wakeword-server (sul Proxmox HOST, non sul VPS)
# pct exec <CT_ID> -- bash -c '
#   cd /opt/jarvis-wakeword && git pull --depth 1
#   cd wakeword-server && docker compose up -d --build
# '

# 6. Se exec-approvals.json e cambiato
cp /opt/jarvis/cloud/exec-approvals.json ~/.openclaw/
sudo systemctl restart openclaw

# 7. Se il plugin browser-dom e cambiato
cp -r /opt/jarvis/extensions/browser-dom/* ~/.openclaw/extensions/browser-dom/
cd ~/.openclaw/extensions/browser-dom && npm install
sudo systemctl restart openclaw-chrome openclaw
```

> **Nota**: l'aggiornamento di OpenClaw non richiede rebuild Docker. L'aggiornamento Docker non tocca OpenClaw. L'aggiornamento di Tailscale non tocca ne Docker ne OpenClaw. Se cambiano solo file Python dell'orchestrator, basta rebuild Docker. Se cambia la skill definition, serve anche la copia + restart OpenClaw. Il database ontology (`graph.db`) persiste nel volume Docker `ontology_data` ed e indipendente dal rebuild. Il wakeword-server si aggiorna direttamente sul Proxmox host (non sul VPS).

---

## Transizione Cloud → Locale

Dopo 2 settimane di testing cloud, per passare al deploy locale (con GPU):

1. Esporta i database: `cp data/jarvis_state.db ~/jarvis_backup.db` e `docker cp jarvis_ontology:/app/data/graph.db ~/ontology_backup.db`
2. Sul server locale, segui la guida in [`infrastructure/README.md`](../infrastructure/README.md)
3. Copia i database: `cp ~/jarvis_backup.db data/jarvis_state.db` e copia `ontology_backup.db` nel volume ontology
4. Cambia `AI_BACKEND=local` nel `.env` locale
5. Spegni il VPS cloud

La transizione e trasparente: il database, le location, gli utenti, le voci enrollate e la memoria sono tutti nel file SQLite.
