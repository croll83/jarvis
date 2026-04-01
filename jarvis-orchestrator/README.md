# JARVIS Orchestrator

Il modulo Skill/Executor dell'architettura JARVIS: un FastAPI server che espone 9 endpoint REST per OpenClaw e gestisce domotica multi-location, voice processing, speaker identification, memoria stratificata e sicurezza L1-L4.

## Architettura

```
┌──────────────────────────────────────────────────────────────┐
│  OpenClaw (VM separata / bare-metal)                         │
│  Gemini 3 Pro — Reasoning, web search, Telegram, multi-turn │
│  Chiama JARVIS Orchestrator come Skill via REST              │
│  :18789 (raggiungibile via Tailscale o LAN)                  │
└──────────────────────────┬───────────────────────────────────┘
                           │ REST /api/tools/*
                           ▼
┌──────────────────────────────────────────────────────────────┐
│  JARVIS Docker Stack (VM GPU / VPS)                          │
│                                                               │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  SKILL — JARVIS Orchestrator (FastAPI :5000)           │  │
│  │  ┌─────────────┐ ┌─────────────┐ ┌──────────────┐     │  │
│  │  │ Home Control│ │ Speaker ID  │ │  Security    │     │  │
│  │  │ Multi-HA    │ │ Resemblyzer │ │  L1-L4       │     │  │
│  │  └─────────────┘ └─────────────┘ └──────────────┘     │  │
│  │  ┌─────────────┐ ┌─────────────┐ ┌──────────────┐     │  │
│  │  │ Memory      │ │ TTS/Entity  │ │ Admin /admin │     │  │
│  │  │ Stratificata│ │ Resolve     │ │ Dashboard    │     │  │
│  │  └─────────────┘ └─────────────┘ └──────────────┘     │  │
│  └───────────────────────┬────────────────────────────────┘  │
│                          │                                    │
│      ┌───────────────────┼──────────────────┐                │
│      ▼                   ▼                  ▼                │
│  ┌──────────┐    ┌──────────────┐   ┌──────────────┐        │
│  │ Ollama   │    │Parakeet STT │   │Home Assistant │        │
│  │ :11434   │    │  GX10:7865  │   │   :8123       │        │
│  │ Qwen 3B  │    │ (Tailscale) │   │ (N locations) │        │
│  └──────────┘    └──────────────┘   └──────────────┘        │
│                                                               │
│  ┌──────────────┐                                            │
│  │ ChromaDB     │  Shared vector store (:8000)               │
│  │ :8000        │  Used by orchestrator + ha-memory-service  │
│  │ HttpClient   │  + ontology-bridge plugin                  │
│  └──────────────┘                                            │
│                                                               │
│  JARVIS Approval Bot — Telegram bot separato per conferme L3 │
│  (locks, alarm, cameras) — canale isolato da OpenClaw        │
│                                                               │
│  OpenClaw WS Operator Client — WebSocket conn a gateway       │
│  Riceve exec approval events, invia bottoni inline Telegram   │
│  (Once/Always/Deny), risolve approvazioni via WS              │
└──────────────────────────────────────────────────────────────┘
```

**Ruoli:**

| Componente | Ruolo | Modello |
|------------|-------|---------|
| **OpenClaw** | Brain: reasoning, web search, Telegram, multi-turn (VM separata, bare-metal) | Gemini 3 Pro |
| **JARVIS Orchestrator** | Skill/Executor: domotica, voice, speaker ID, security | FastAPI |
| **Qwen 7B Q4** | Pre-routing locale per domotica fast path + offline fallback | Ollama |

---

## Pre-Routing a 3 Vie

Ogni richiesta viene classificata in una delle 3 categorie prima di raggiungere il modello AI:

```
Input (voce / Telegram / OpenClaw)
        │
        ▼
┌──────────────────┐
│  PRE-ROUTING     │  Qwen 7B Q4 locale (~50ms)
│  Classificazione │  oppure regex fast-path (~1ms)
└───────┬──────────┘
        │
        ├─── DOMOTICA_CERTA ───▶ Qwen locale → Home Assistant (bypass OpenClaw)
        │    conf > 0.90          Latenza totale: <200ms
        │
        ├─── DOMOTICA_INCERTA ──▶ OpenClaw con hint domotico
        │    conf 0.50-0.90       OpenClaw decide se usare jarvis_home_control
        │
        └─── ALTRO ────────────▶ OpenClaw (reasoning, search, chat, etc.)
             conf < 0.50          Nessun hint domotico
```

### Dettaglio Categorie

| Categoria | Confidenza | Esempio | Percorso |
|-----------|------------|---------|----------|
| `DOMOTICA_CERTA` | > 0.90 | "Accendi la luce del salotto" | Qwen locale -> HA diretto |
| `DOMOTICA_INCERTA` | 0.50-0.90 | "Cerca il telecomando e apri le tapparelle" | OpenClaw + hint |
| `ALTRO` | < 0.50 | "Che tempo fa domani a Roma?" | OpenClaw puro |

Il pre-routing locale garantisce che i comandi domotici puri non tocchino mai la rete esterna, mantenendo latenza sotto 200ms e funzionando anche offline.

---

## Modello di Sicurezza L1-L4

Ogni azione domotica viene classificata per livello di rischio e filtrata in base al canale di origine:

| Livello | Domini HA | Voice (L3) | Telegram (L2) | Email (L4) |
|---------|-----------|------------|----------------|------------|
| **L1** | lights, sensors, switches | pass | pass | blocked |
| **L2** | covers, climate, fans | pass | context check | blocked |
| **L3** | locks, alarm, cameras | pass | approval bot | blocked |
| **L4** | -- | -- | -- | always blocked |

### Canali e Trust

- **Voice** -- certificata da Resemblyzer speaker ID, trusted fino a L3
- **Telegram** -- trusted fino a L2; azioni L3 richiedono conferma via JARVIS Approval Bot
- **Email / sorgenti non certificate** -- L4, sempre bloccate

### JARVIS Approval Bot

Bot Telegram separato da quello di OpenClaw, dedicato esclusivamente alle conferme L3. Canale isolato per prevenire prompt injection nel flusso di approvazione.

```
OpenClaw ──▶ jarvis_home_control (L3 action)
                     │
                     ▼
              ┌─────────────┐
              │  Security   │ Livello L3 + source=telegram
              │  Enforcer   │ → richiede approval
              └──────┬──────┘
                     │
                     ▼
              ┌─────────────┐
              │  Approval   │ Messaggio su bot separato
              │  Bot (TG)   │ con callback approve/deny
              └──────┬──────┘
                     │
                     ▼
              Utente conferma → Azione eseguita su HA
```

---

## OpenClaw Skill API

10 endpoint REST su `/api/tools/`, autenticati via Bearer token (`OPENCLAW_GATEWAY_TOKEN`).

| # | Endpoint | Metodo | Descrizione |
|---|----------|--------|-------------|
| 1 | `/api/tools/home_control` | POST | Controlla entita HA con enforcement sicurezza L1-L4 |
| 2 | `/api/tools/speaker_id` | POST | Identifica speaker da audio via Resemblyzer |
| 3 | `/api/tools/user_context` | GET | Profilo utente, location attiva, preferenze |
| 4 | `/api/tools/security` | POST | Azioni sicurezza (privacy mode, allarme) |
| 5 | `/api/tools/memory_query` | POST | Query memoria stratificata (SQL + vector) |
| 6 | `/api/tools/entity_resolve` | POST | Risolvi friendly name -> entity_id HA |
| 7 | `/api/tools/entity_discover` | POST | Scopri entita per stanza, zona, piano, dominio |
| 8 | `/api/tools/entity_bulk` | POST | **Query/azione bulk** su gruppi di entita (room/zone/floor/domain) |
| 9 | `/api/tools/tts` | POST | Text-to-speech via Alexa/smart speaker o speaker interno AtomS3R (Qwen3-TTS/Kokoro) |
| 10 | `/api/tools/locations` | GET | Lista location con stato health HA |
| 11 | `/api/tools/audit_log` | POST | Registra evento nel trail di audit |
| 12 | `/api/recent_turns` | GET | Ultimi N turni conversazione (`?max_turns=N`) — usato dal plugin ontology-bridge |

La definizione completa della skill e dei parametri e in `skill/SKILL.md` e `skill/skill.json`.

### entity_bulk — Query e azioni di gruppo

Nuovo endpoint per eliminare il problema N+1 delle query multi-entita.

**Prima**: "quali luci sono accese?" -> `entity_discover` + N x `entity_resolve` = N+1 chiamate API
**Dopo**: "quali luci sono accese?" -> 1 x `entity_bulk` (internamente: 1 query DB + 1 REST HA)

| Modalita | Esempio | Cosa fa |
|----------|---------|---------|
| `query` | `{"mode":"query","domain":"light","room":"soggiorno"}` | Ritorna stati live + attributi chiave (brightness, temperatura, ecc.) |
| `action` | `{"mode":"action","domain":"light","action":"turn_off","zone":"Zona Giorno"}` | Esegue servizio su tutte le entita del gruppo in una singola chiamata HA |

Filtri combinabili: `domain`, `room`, `zone`, `floor`, `search`, `entity_ids`. Sicurezza L1-L4 applicata per dominio; L3 (lock, camera, alarm) esclusi dal bulk.

---

## Memoria Stratificata

Il sistema usa una memoria ibrida a 4 livelli con PostgreSQL (strutturato) + ChromaDB shared server :8000 (semantico, `HttpClient` con fallback a `PersistentClient` embedded).

| Strato | Retention | Contenuto | Creazione |
|--------|-----------|-----------|-----------|
| **HOT** | 30 minuti | Messaggi raw (role, content, speaker) | Real-time |
| **WARM** | 24 ore | Summary orario via LLM | Job ogni ora |
| **COLD** | 7 giorni | Summary giornaliero via LLM | Job alle 03:00 |
| **LONG-TERM** | Permanente | Fatti/preferenze + vector embeddings | Job alle 03:00 |

### Vector Search con Recency Boost

```
final_score = vector_similarity * recency_factor
recency_factor = 1 / (1 + age_hours * 0.05)
```

Embedding model: `nomic-embed-text` via Ollama. Threshold: >= 0.3 per messaggi, >= 0.4 per fatti.

### Previous Intent Tracking

Il router Qwen mantiene continuita conversazionale tramite tracking dell'intent precedente:

- `save_last_intent(speaker_id, intent)` — salva l'intent dopo ogni routing (in-memory)
- `get_last_intent(speaker_id)` — recupera l'ultimo intent (finestra di 15 min, `ROUTER_MEMORY_WINDOW_SECONDS=900`)
- Il prompt di routing include una sezione `[INTENT PRECEDENTE]` per dare al router contesto sulla conversazione in corso
- Previene oscillazioni di routing (es. domanda follow-up su domotica che verrebbe classificata come ALTRO)

### HA Memory Sidecar

Ogni istanza Home Assistant ha un sidecar che traccia state changes, genera summary orari/giornalieri e espone API per vector search contestuale alla location.

---

## Speaker Interno (AtomS3R Mobile)

Per device AtomS3R in mobilita (con batteria accessoria), le risposte TTS vengono riprodotte
direttamente sullo speaker integrato del device (ES8311 DAC + NS4150B amp) invece di passare
da un media_player Home Assistant (Alexa/Echo).

### Flusso audio

```
AI Response text
  ↓
TTS Engine (Qwen3-TTS su GX10 / Kokoro cloud) → PCM 24kHz streaming
  ↓
scipy resample → PCM 16kHz mono int16
  ↓
Opus encode (320 samples/frame, 20ms)
  ↓
WS binary frames → Wakeword relay → Device
  ↓
Device: opus_decode → jarvis_codec_write → I2S → Speaker
  ↓
Server invia tts_done → Device: BUSY → IDLE
```

### Configurazione

Dalla dashboard admin (tab Voice Devices), attivare il checkbox **Speaker Interno (mobile)**.
Quando attivato:
- L'output speaker HA diventa opzionale (non necessario)
- Le risposte TTS vengono generate server-side (Qwen3-TTS su GX10 o Kokoro cloud, in base a `TTS_ENGINE`)
- L'audio viene codificato in frame Opus e inviato al device via WebSocket
- Il firmware decodifica e riproduce direttamente (nessuna modifica firmware richiesta)
- Speaker suppress HA non viene attivato (non necessario)
- Multi-turn funziona normalmente via `trigger_listen`

### Dipendenze

- `qwen3-tts` service su GX10 (Qwen3-TTS-12Hz-1.7B, porta 9880) — via Tailscale
- `kokoro-tts` container (Kokoro-FastAPI, porta 8890) — deploy cloud CPU
- `opuslib` (Python, gia presente)
- `scipy` (Python, gia presente — resample 24kHz → 16kHz)

### Modulo

`internal_tts.py` — chiama Qwen3-TTS o Kokoro via HTTP (selezionabile via `TTS_ENGINE`), resampla PCM 24→16kHz, codifica Opus, invia frame via `ws_audio_handler.send_tts_frame()`.

---

## Speaker Identification

Riconoscimento vocale biometrico tramite Resemblyzer (deep speaker embeddings):

```
Audio ──▶ Parakeet STT (GX10) ──▶ testo
  │
  └─────▶ Resemblyzer ──▶ embedding 256-dim ──▶ cosine similarity ──▶ user_id
                                                    threshold: 0.75
```

- Minimo 3 campioni per enrollment
- Similarita > 75% = identificato, < 75% = sconosciuto
- Il profilo vocale determina il livello di trust per il modello di sicurezza

---

## Multi-Location Home Assistant

JARVIS supporta N istanze Home Assistant (es. casa principale + casa vacanze).

### Risoluzione Location

1. **Esplicita nel comando**: "Accendi le luci a Wagmi" -> keyword match
2. **Device**: MAC address AtomS3R -> lookup DB -> location
3. **Telegram**: sticky location dell'utente
4. **Default**: prima location abilitata

### Entity Map

Struttura gerarchica Zone -> Area -> Room -> EntityType -> Entities con mapping automatico friendly name -> entity_id. Importabile da JSON o sincronizzabile da HA via `ha-sync`.

---

## Configurazione

Riferimento: `.env` nella root del progetto (vedi `docker-compose.yml`).

```bash
# ============================
# AI BACKEND
# ============================
AI_BACKEND=local                    # "local" (Ollama) o "api" (Cloud)
OLLAMA_URL=http://ollama:11434
STT_URL=http://100.98.187.12:7865

# API keys (solo se AI_BACKEND=api)
GROQ_API_KEY=gsk_xxx                # STT cloud
OPENROUTER_API_KEY=sk-or-xxx        # Routing cloud
GEMINI_API_KEY=AIza_xxx             # Reasoning + immagini

# ============================
# OPENCLAW
# ============================
OPENCLAW_URL=http://jarvis-openclaw:18789  # VM separata via Tailscale/LAN
OPENCLAW_GATEWAY_TOKEN=xxx          # Token condiviso OpenClaw <-> Orchestrator

# ============================
# HOME ASSISTANT
# ============================
HASS_URL=http://homeassistant:8123
JARVIS_HASS_TOKEN=your_long_lived_token

# ============================
# JARVIS APPROVAL BOT (L3)
# ============================
JARVIS_APPROVAL_BOT_TOKEN=xxx       # Bot Telegram separato per conferme L3
JARVIS_APPROVAL_CHAT_ID=xxx

# ============================
# SMTP (OTP, notifiche)
# ============================
JARVIS_SMTP_HOST=smtp.gmail.com
JARVIS_SMTP_PORT=587
JARVIS_SMTP_USER=your_email@gmail.com
JARVIS_SMTP_PASSWORD=your_app_password

# ============================
# CHROMADB (shared vector store)
# ============================
CHROMA_HOST=localhost                # ChromaDB server host (default: localhost)
CHROMA_PORT=8000                     # ChromaDB server port (default: 8000)

# ============================
# POSTGRESQL
# ============================
POSTGRES_USER=jarvis
POSTGRES_PASSWORD=xxx
POSTGRES_DB=main

# ============================
# SECURITY (WebAuthn)
# ============================
JARVIS_WEBAUTHN_RP_ID=localhost
JARVIS_WEBAUTHN_RP_NAME=JARVIS
JARVIS_WEBAUTHN_ORIGIN=http://localhost:8000
```

---

## Docker Services

Definiti in `docker-compose.yml` nella root del progetto:

| Servizio | Immagine | Porta | Ruolo |
|----------|----------|-------|-------|
| `ollama` | ollama/ollama | 11434 | Qwen 7B Q4 + nomic-embed-text (GPU) |
| Parakeet STT | GX10 systemd | 7865 | Speech-to-text (nvidia/parakeet-tdt-0.6b-v3, via Tailscale) |
| Qwen3-TTS | GX10 systemd | 9880 | TTS voice cloning IT/EN (Qwen3-TTS-12Hz-1.7B, via Tailscale) |
| `orchestrator` | build locale | 5000 | JARVIS Skill (questo progetto) |
| `chromadb` | chromadb/chroma:0.6.3 | 8000 | Shared vector store (HttpClient, fallback embedded) |
| `tailscale` | tailscale/tailscale | - | VPN mesh per HA remoti |
| `postgres` | postgres:16-alpine | 5432 | Database principale |
| `mongo` | mongo:7 | 27017 | Database side-projects |

**OpenClaw** gira bare-metal su VM separata (non in Docker) per isolamento di sicurezza. Porta 18789.

Profilo opzionale `tools` per Adminer (DB web UI) sulla porta 8080.

Security stack (Frigate + DoubleTake) in `security/docker-compose.security.yml`.

---

## Dashboard Admin

Accessibile via `http://jarvis:5000/admin` (richiede login).

### Autenticazione

- Setup wizard al primo avvio
- Login email/password + Passkeys (WebAuthn)
- Reset password via OTP email
- Sessioni persistenti 30 giorni

### Sezioni

| Tab | Funzionalita |
|-----|--------------|
| **Dashboard** | Setup progress, health servizi, metriche live |
| **Utenti** | CRUD utenti, enrollment vocale, test riconoscimento |
| **Location** | Multi-HA: CRUD locations, entity maps, sync da HA, test connessione |
| **Sistema** | Preferenze editabili, config read-only, system state, backup/restore |
| **Audit Log** | Log eventi con filtri categoria/speaker |
| **Dispositivi** | Device failures, voice devices (AtomS3R), configurazione speaker interno |
| **Cache** | Query cache, statistiche hit/miss |
| **Memory** | Stats ChromaDB vectors, SQL summaries, per-user breakdown, HA Memory |
| **Access Log** | HTTP access log, auth attempts, anomaly detection |

SSE real-time updates con fallback a polling. Dark/light theme.

---

## Database Schema (PostgreSQL)

```sql
-- ===== CORE =====
users (id, name, email, password_hash, role, voice_enrolled, voice_model_path, ...)
sessions (id, user_id, created_at, expires_at, ip_address, user_agent)
passkey_credentials (id, user_id, credential_id, public_key, sign_count, ...)
otp_tokens (id, user_id, email, token_hash, purpose, expires_at, used)
system_state (key, value)
audit_log (id, timestamp, category, message, speaker_id, speaker_name)

-- ===== DOMOTICA =====
locations (id, name, city, hass_url, hass_token, has_security, enabled, keywords, ...)
user_locations (user_id, location_id, source, updated_at)
entity_maps (id, location_id, zone, area, room, entity_type, entity_name, entity_id)
voice_devices (device_id, friendly_name, location_id, output_speaker, fallback_speaker, use_internal_speaker, ...)
device_failures (entity_id, count, last_error, timestamp)

-- ===== MEMORIA =====
chat_memory (id, timestamp, role, content, source, speaker_id, speaker_name)
user_memory_hourly (id, user_id, hour_start, summary, message_count)
user_memory_daily (id, user_id, date, summary, hourly_count, message_count)
user_memory_longterm (id, user_id, fact, category, confidence, source, ...)
query_cache (query_hash, query_text, response, hit_count, last_used, auto_learned)
query_frequency (query_normalized, count, last_response, response_consistent)

-- ===== TELEGRAM =====
telegram_links (telegram_id, user_id, first_name, linked_at)
telegram_streams (source_id, message_id, last_update)

-- ===== PREFERENZE =====
user_preferences (user_id, pref_key, pref_value)
global_preferences (pref_key, pref_value)

-- ===== SECURITY =====
access_log (id, timestamp, method, path, status_code, ip_address, user_agent, ...)
auth_attempts (id, timestamp, email, ip_address, user_agent, success, failure_reason)
pending_actions (action_id, payload, requester_id, source_channel, security_level, ...)

-- ===== CHROMADB (vector store, separato) =====
-- user_messages     (embedding 7 giorni)
-- user_facts        (embedding permanente)
-- location_events_* (embedding per location, 7 giorni)
```

---

## Latenze Target

| Scenario | Target | Percorso |
|----------|--------|----------|
| Domotica certa (voce) | <500ms | Parakeet STT + Resemblyzer + Qwen locale + HA |
| Domotica certa (Telegram) | <200ms | Qwen locale + HA |
| Domotica incerta | 1-3s | OpenClaw + jarvis_home_control |
| Chat / reasoning | 2-10s | OpenClaw (Gemini 3 Pro) |
| Cache hit | <1ms | PostgreSQL lookup |

---

## Avvio

### Development

```bash
cd jarvis-orchestrator
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 5000
```

### Docker

```bash
# Dalla root del progetto
docker compose up -d

# Solo security stack
cd security && docker compose -f docker-compose.security.yml up -d
```
