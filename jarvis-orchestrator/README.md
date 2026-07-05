# JARVIS Orchestrator

Il modulo Skill/Executor dell'architettura JARVIS: un FastAPI server che espone 9 endpoint REST per AI Agent (Hermes/OpenClaw/others) e gestisce domotica multi-location, voice processing, speaker identification, memoria stratificata e sicurezza L1-L4.

## Architettura

```
┌──────────────────────────────────────────────────────────────┐
│  AI Agent (VM separata / bare-metal)                         │
│  Cloud LLM (via AI Agent) — Reasoning, web search, Telegram, multi-turn │
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
│  ┌──────────────┐  ┌────────────────────┐                    │
│  │ Redis :6379  │  │ mem0-stack (ext.)  │                    │
│  │ Context Bus  │  │ MEM0_BASE_URL      │                    │
│  │ (short-term) │  │ semantic+procedural│                    │
│  │              │  │ croll83/mem0-stack │                    │
│  └──────────────┘  └────────────────────┘                    │
│                                                               │
│  JARVIS Approval Bot — Telegram bot separato per conferme L3 │
│  (locks, alarm, cameras) — canale isolato da AI Agent        │
│                                                               │
│  AI Agent WS Operator Client — WebSocket conn a gateway       │
│  Riceve exec approval events, invia bottoni inline Telegram   │
│  (Once/Always/Deny), risolve approvazioni via WS              │
└──────────────────────────────────────────────────────────────┘
```

**Ruoli:**

| Componente | Ruolo | Modello |
|------------|-------|---------|
| **AI Agent** | Brain: reasoning, web search, Telegram, multi-turn (VM separata, bare-metal) | Cloud LLM (via AI Agent) |
| **JARVIS Orchestrator** | Skill/Executor: domotica, voice, speaker ID, security | FastAPI |
| **Qwen 7B Q4** | Pre-routing locale per domotica fast path + offline fallback | Ollama |

---

## Pre-Routing a 3 Vie

Ogni richiesta viene classificata in una delle 3 categorie prima di raggiungere il modello AI:

```
Input (voce / Telegram / AI Agent)
        │
        ▼
┌──────────────────┐
│  PRE-ROUTING     │  Qwen 7B Q4 locale (~50ms)
│  Classificazione │  oppure regex fast-path (~1ms)
└───────┬──────────┘
        │
        ├─── DOMOTICA_CERTA ───▶ Qwen locale → Home Assistant (bypass AI Agent)
        │    conf > 0.90          Latenza totale: <200ms
        │
        ├─── DOMOTICA_INCERTA ──▶ AI Agent con hint domotico
        │    conf 0.50-0.90       AI Agent decide se usare jarvis_home_control
        │
        └─── ALTRO ────────────▶ AI Agent (reasoning, search, chat, etc.)
             conf < 0.50          Nessun hint domotico
```

### Dettaglio Categorie

| Categoria | Confidenza | Esempio | Percorso |
|-----------|------------|---------|----------|
| `DOMOTICA_CERTA` | > 0.90 | "Accendi la luce del salotto" | Qwen locale -> HA diretto |
| `DOMOTICA_INCERTA` | 0.50-0.90 | "Cerca il telecomando e apri le tapparelle" | AI Agent + hint |
| `ALTRO` | < 0.50 | "Che tempo fa domani a Roma?" | AI Agent puro |

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

Bot Telegram separato da quello di AI Agent, dedicato esclusivamente alle conferme L3. Canale isolato per prevenire prompt injection nel flusso di approvazione.

```
AI Agent ──▶ jarvis_home_control (L3 action)
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

## AI Agent Skill API

10 endpoint REST su `/api/tools/`, autenticati via Bearer token (`AI_AGENT_TOKEN`).

| # | Endpoint | Metodo | Descrizione |
|---|----------|--------|-------------|
| 1 | `/api/tools/home_control` | POST | Controlla entita HA con enforcement sicurezza L1-L4 |
| 2 | `/api/tools/speaker_id` | POST | Identifica speaker da audio via Resemblyzer |
| 3 | `/api/tools/user_context` | GET | Profilo utente, location attiva, preferenze |
| 4 | `/api/tools/security` | POST | Azioni sicurezza (privacy mode, allarme) |
| 6 | `/api/tools/entity_resolve` | POST | Risolvi friendly name -> entity_id HA |
| 7 | `/api/tools/entity_discover` | POST | Scopri entita per stanza, zona, piano, dominio |
| 8 | `/api/tools/entity_bulk` | POST | **Query/azione bulk** su gruppi di entita (room/zone/floor/domain) |
| 9 | `/api/tools/tts` | POST | Text-to-speech via Alexa/smart speaker o speaker interno AtomS3R (CosyVoice3/Kokoro) |
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

## Sistema di Memoria

Architettura a 3 layer **disaccoppiati**, ciascuno con un compito ben preciso:

```
┌─────────────────────────────────────────────────────────────────┐
│  L1  HOT       SQLite locale  chat_memory  (raw, 30 min)        │
│      └─ ogni riga porta meta JSON: route, payload,              │
│         ha_entity_id, ha_action, ha_params, ha_status, ...      │
│                                                                  │
│  L2  SHORT-TERM Redis ctx:{user_id}:events  (max 20, TTL 30m)   │
│      └─ context bus cross-system (orchestrator / Hermes /       │
│         ha_memory_service); ognuno tagga `source`               │
│                                                                  │
│  L3  LONG-TERM  mem0-stack (esterno, croll83/mem0-stack)        │
│      └─ semantic + procedural + reasoning_bank;                 │
│         popolato dal job notturno `habit_extraction`            │
│         (ibrido SQL + LLM) con `agent_id=jarvis-habit-extractor`│
└─────────────────────────────────────────────────────────────────┘
```

> I vecchi layer **WARM / COLD / LONG-TERM SQL** (summary orari/giornalieri + fatti estratti locali) sono stati **rimossi**: la memoria semantica long-term vive ora solo in mem0-stack. L'orchestrator non gira più job di summary orari/giornalieri.

### Layer 1 — SQLite HOT con meta strutturato

Tabella `chat_memory` (retention `CHAT_MEMORY_MAX_AGE_HOURS`, default 24h ma usata come HOT-only — gli ultimi ~30 min per il routing/reasoning):

| Colonna | Tipo | Note |
|---------|------|------|
| `id`, `timestamp`, `role`, `content` | base | riga raw |
| `source`, `speaker_id`, `speaker_name` | base | provenienza + speaker biometrico |
| `meta` | JSON nullable | arricchimento per habit extraction (vedi sotto) |

Schema di `meta` per le righe utente (popolato live durante `/process_input` in `main.py`):

```jsonc
{
  "route": "HOME_CONTROL" | "SIMPLE_CHAT" | "AI_AGENT" | ...,
  "confidence": 0.93,
  "payload": { ... },                  // router_data.payload (entity, action ipotizzati)

  // Solo per route=HOME_CONTROL, aggiunto dopo l'esecuzione HA:
  "ha_mode": "single" | "bulk",
  "ha_entity_id": "light.salotto",     // (single)
  "ha_entity_ids": ["...", "..."],     // (bulk)
  "ha_domain": "light",
  "ha_action": "turn_on",
  "ha_params": {"brightness": 200},
  "ha_status": "ok" | "partial" | "error",
  "ha_error": null,
  "ha_location": "casa"
}
```

API in `database.py`:
- `save_chat_message(role, content, source, speaker_id, speaker_name, meta=None) -> int` — ritorna `chat_id`
- `update_chat_meta(chat_id, patch)` — shallow merge sul JSON esistente

Indice `idx_chat_memory_speaker_ts(speaker_id, timestamp)` per le aggregazioni del job notturno.

### Layer 2 — Redis Context Bus

Redis condiviso tra orchestrator, `ha_memory_service` e Hermes (`REDIS_URL` env).

- Ogni sistema scrive eventi con il proprio `source` tag e legge solo eventi da ALTRI sistemi (no auto-duplicazione)
- Struttura: `ctx:{user_id}:events` → lista JSON cappata (max 20 eventi, TTL 30 min)
- L'orchestrator legge max 3-5 eventi recenti per arricchire il contesto delle risposte

### Layer 3 — mem0-stack: habit extraction ibrida (SQL + LLM)

Job notturno `habit_extraction.run_habit_extraction_job` (schedulato da `memory_jobs.memory_scheduler` all'ora `MEMORY_DAILY_TRIGGER_HOUR`).

Per ogni utente mappato in `SPEAKER_USER_MAP` (es. `1:marco,2:ada`):

1. **Domotica → SQL deterministica.** Query con `json_extract(meta, '$.route') = 'HOME_CONTROL'`, GROUP BY `(ha_entity_id, ha_action)`:
   - filtra `count >= HABIT_MIN_OCCURRENCES`, `span_days >= HABIT_MIN_SPAN_DAYS`
   - finestra oraria: mode delle ore di esecuzione (>=60% delle occorrenze)
   - frequenza: `daily / weekday / weekend / weekly / sporadic` derivata da `count/span` + distribuzione weekday
   - valore più frequente da `ha_params` (`temperature`, `brightness`, `position`, …)
   - confidence = `0.55 + 0.4 * density + bonus_consistenza_finestra` (max 0.99)
   - descrizione natural-language **deterministica** (no LLM)
2. **Preferenze / topic → LLM (Qwen).** Solo sui messaggi `route != HOME_CONTROL` (o legacy senza `meta`), prompt scoped a `kind ∈ {preference, topic}`.
3. **Upsert su mem0** (`MEM0_BASE_URL`):
   - match per `(entity, action)` o `kind + descrizione`
   - se non esiste → `POST /add` (`agent_id=jarvis-habit-extractor`, `metadata.type=habit`, content prefisso `[Habit] …`)
   - se esiste **con drift** (frequency change / time-window shift > `HABIT_DRIFT_THRESHOLD` / value change) → `PUT /memories/{id}` con `version + 1`
   - altrimenti refresh di `last_seen` e `sample_size`

I record sono filtrabili nella **dashboard mem0 su Hermes** via `agent_id=jarvis-habit-extractor` e distinguibili dalle memorie conversazionali (che hanno altro `agent_id`).

Env rilevanti (`config.py`):

```
HABIT_LOOKBACK_DAYS       = 30        # finestra di analisi
HABIT_MIN_OCCURRENCES     = 5         # eventi minimi per essere habit
HABIT_MIN_SPAN_DAYS       = 14        # span minimo del pattern
HABIT_CONFIDENCE_FLOOR    = 0.4       # taglio
HABIT_DRIFT_THRESHOLD     = 0.05      # ~72 min sulla finestra HH:MM
MEMORY_DAILY_TRIGGER_HOUR = 3         # ora del job
```

### Sicurezza: memory_search lato Qwen tool calling

Il tool `memory_search` esposto al router Qwen (vedi `web_tools.execute_memory_search`) usa di default `user_id="shared"` se lo speaker non è autenticato, ed effettua mapping `speaker_id → user_id mem0` tramite `speaker_to_user_id()` (`SPEAKER_USER_MAP`). **Non c'è più fallback a `user_id=1` (=marco)**, eliminando il rischio di memory-leak cross-utente. Endpoint usato: `POST {MEM0_BASE_URL}/search_contextual?summarize=false` (~110ms, senza re-ranker LLM).

### Previous Intent Tracking

Il router Qwen mantiene continuita conversazionale tramite tracking dell'intent precedente:

- `save_last_intent(speaker_id, intent)` — salva l'intent dopo ogni routing (in-memory)
- `get_last_intent(speaker_id)` — recupera l'ultimo intent (finestra di 15 min, `ROUTER_MEMORY_WINDOW_SECONDS=900`)
- Il prompt di routing include una sezione `[INTENT PRECEDENTE]` per dare al router contesto sulla conversazione in corso
- Previene oscillazioni di routing (es. domanda follow-up su domotica che verrebbe classificata come ALTRO)

### HA Memory Sidecar

Ogni istanza Home Assistant ha un sidecar che traccia state changes, genera summary orari/giornalieri, pubblica contesto real-time su Redis e estrae pattern long-term su mem0.

---

## Speaker Interno (AtomS3R Mobile)

Per device AtomS3R in mobilita (con batteria accessoria), le risposte TTS vengono riprodotte
direttamente sullo speaker integrato del device (ES8311 DAC + NS4150B amp) invece di passare
da un media_player Home Assistant (Alexa/Echo).

### Flusso audio

```
AI Response text
  ↓
TTS Engine (CosyVoice3 su GX10 / Kokoro cloud) → PCM 24kHz streaming
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
- Le risposte TTS vengono generate server-side (CosyVoice3 su GX10 o Kokoro cloud, in base a `TTS_ENGINE`)
- L'audio viene codificato in frame Opus e inviato al device via WebSocket
- Il firmware decodifica e riproduce direttamente (nessuna modifica firmware richiesta)
- Speaker suppress HA non viene attivato (non necessario)
- Multi-turn funziona normalmente via `trigger_listen`

### Dipendenze

- `cosyvoice3-tts` service su GX10 (Fun-CosyVoice3-0.5B, porta 9880) — via Tailscale
- `kokoro-tts` container (Kokoro-FastAPI, porta 8890) — deploy cloud CPU
- `opuslib` (Python, gia presente)
- `scipy` (Python, gia presente — resample 24kHz → 16kHz)

### Modulo

`internal_tts.py` — chiama CosyVoice3 o Kokoro via HTTP (selezionabile via `TTS_ENGINE`), resampla PCM 24→16kHz, codifica Opus, invia frame via `ws_audio_handler.send_tts_frame()`.

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
# AI AGENT
# ============================
AI_AGENT_URL=https://your-agent-host:18789  # AI Agent gateway URL (VM separata via Tailscale/LAN)
AI_AGENT_TOKEN=xxx                  # AI Agent gateway token (condiviso AI Agent <-> Orchestrator)

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
# REDIS (cross-system context bus)
# ============================
REDIS_URL=redis://your_redis_host:6379/0

# ============================
# MEM0 (long-term behavioral memory)
# ============================
MEM0_BASE_URL=http://your_mem0_host:8200

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
| CosyVoice3 | GX10 systemd | 9880 | TTS zero-shot voice cloning (Fun-CosyVoice3-0.5B, via Tailscale) |
| `orchestrator` | build locale | 5000 | JARVIS Skill (questo progetto) |
| `redis` | redis:7-alpine | 6379 | Context bus cross-system (su LXC Jarvis) |
| `mem0` | mem0 server | 8200 | Long-term behavioral memory (su LXC Jarvis) |
| `tailscale` | tailscale/tailscale | - | VPN mesh per HA remoti |
| `postgres` | postgres:16-alpine | 5432 | Database principale |
| `mongo` | mongo:7 | 27017 | Database side-projects |

**AI Agent** gira bare-metal su VM separata (non in Docker) per isolamento di sicurezza. Porta 18789.

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
| **Memory** | Chat HOT count (totale + per-utente), info mem0-stack esterno, retention chat + habit extraction config |
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
chat_memory (id, timestamp, role, content, source, speaker_id, speaker_name, meta)
  -- HOT only (retention CHAT_MEMORY_MAX_AGE)
  -- meta JSON: {route, confidence, payload, ha_entity_id, ha_action, ha_params, ha_status, ...}
  -- index: idx_chat_memory_speaker_ts(speaker_id, timestamp)
-- Long-term semantic memory: mem0-stack (MEM0_BASE_URL), popolata da habit_extraction
-- (job notturno ibrido SQL + LLM, agent_id=jarvis-habit-extractor)
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

-- ===== EXTERNAL MEMORY =====
-- Redis: ctx:{user_id}:events (short-term cross-system, TTL 30min, max 20)
-- mem0-stack (esterno): long-term semantic + procedural memory
--   Repo: croll83/mem0-stack — consumato via MEM0_BASE_URL (HTTP /search, /add, ...)
```

---

## Latenze Target

| Scenario | Target | Percorso |
|----------|--------|----------|
| Domotica certa (voce) | <500ms | Parakeet STT + Resemblyzer + Qwen locale + HA |
| Domotica certa (Telegram) | <200ms | Qwen locale + HA |
| Domotica incerta | 1-3s | AI Agent + jarvis_home_control |
| Chat / reasoning | 2-10s | AI Agent (Cloud LLM) |
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

## Novità luglio 2026 (voce & risoluzione)

### STT: Canary-1b-v2 (via Parakeet)
Parakeet-TDT v3 non espone kwargs lingua (`transcribe()` fa solo auto-LID) e su
audio corti trascriveva l'italiano come russo. Il server su GX10 (`:9000`,
unit `parakeet-stt` per ragioni storiche) ora carica `nvidia/canary-1b-v2` e
inoltra `language=it` come `source_lang/target_lang` (nativo del multitask).
Difese residue in `integrations._transcribe_local`: transcript in cirillico con
lingua forzata → sentinella `__LANG_MISMATCH__` → il WS handler risponde
"puoi ripetere?" (mai None: il device resterebbe in speaking state); rescue
trasparente via Groq whisper se `GROQ_API_KEY` è configurata.

### Risoluzione entità (ordine attuale)
1. **B0 — nome esatto globale** (con dominio, `exact_only`): un friendly name
   univoco vince sempre sull'estrazione stanza dal testo ("accendi filtraggio
   piscina" non deve diventare il boiler dell'area Piscina).
2. Path A (testo utente: stanza/zona/piano, union room∪area∪zone).
3. B1 exact con room-hint, **con retry globale** se la stanza del microfono
   nasconde nomi di altre stanze; poi discovery, parole, semantic (i comandi
   risolvono solo entità `visible`; le query info cercano su tutto l'indice).
Il fallback sintetico `<domain>.<nome>` esiste solo con dominio valido; senza,
risposta onesta "non ho trovato".

### Azioni normalizzate per dominio risolto
`_map_action_for_domain` è module-level e usato anche dal path di approvazione:
`press/open/close` diventano il servizio giusto per cover/lock/switch; i
`button` si premono e basta; gli **script si eseguono sempre** (`turn_off` su
uno script è "ferma", non "esegui").

### Musica (Music Assistant)
`action=play_music` → `music_assistant.play_media` con ricerca libera
(media_type solo playlist/radio), player per stanza (`_MUSIC_PLAYERS`):
soundbar MASS in salotto, Echo altrove (provider alexa di MA + skill
"la mia radio"). Fire-and-forget (la ricerca può superare il timeout HA);
niente smart-cache sulle azioni.

### Scenari & riti vocali
Script-ponte (`automation.trigger` con `skip_condition`) per gli scenari senza
trigger (Rientro/Esco/Buongiorno/Buonanotte/Privacy-Sicurezza telecamere):
visibili nella mappa LLM, su Android Auto e nei preferiti dashboard (con
conferma). "Buonanotte/Buongiorno [Jarvis]" secchi bypassano il router
(shortcut deterministico); "disattiva privacy/sicurezza telecamere" flippa
sullo script opposto.

### Sicurezza: override per entità
`ENTITY_LEVEL_OVERRIDES` in `security_levels.py`: gli apricancello BTicino
(`button.bticino_hometouch_unlock*`) sono L3 come le serrature, non L1 del
dominio button — da canali agent (L2) serve l'approvazione sul bot.
