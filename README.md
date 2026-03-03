# JARVIS - Smart Home AI Assistant

> Multi-location smart home AI assistant with voice control, reasoning, speaker identification, and security enforcement.

---

## Architecture Overview

```
                           INPUTS
              ┌──────────────┬──────────────┐
              │  AtomS3R     │   Telegram    │
              │  (voice)     │   (text)      │
              └──────┬───────┴──────┬────────┘
                     │              │
         ┌───────────▼──────────────▼───────────┐
         │        JARVIS ORCHESTRATOR            │
         │        (FastAPI - :5000)               │
         │                                        │
         │  ┌────────────┐   ┌────────────────┐  │
         │  │  Whisper    │   │  Resemblyzer   │  │
         │  │  (local)    │   │  (speaker ID)  │  │
         │  │  STT        │   │  biometric     │  │
         │  └─────┬──────┘   └───────┬────────┘  │
         │        │                  │            │
         │        ▼                  ▼            │
         │  ┌─────────────────────────────────┐   │
         │  │     Qwen 7B Q4 (Ollama)         │   │
         │  │     Pre-routing / fast path      │   │
         │  │     Offline fallback             │   │
         │  └──────────┬──────────────────────┘   │
         │             │                          │
         │   ┌─────────┼─────────┐                │
         │   │         │         │                │
         │   ▼         ▼         ▼                │
         │ HOME    REASONING   CHAT               │
         │ CONTROL (OpenClaw)  (OpenClaw)          │
         │   │         │         │                │
         └───┼─────────┼─────────┼────────────────┘
             │         │         │
             ▼         ▼         ▼
     ┌────────────┐ ┌──────────────────┐
     │   Home     │ │    OpenClaw +    │
     │ Assistant  │ │   Gemini 3 Pro   │
     │ (per loc.) │ │   (Brain)        │
     └────────────┘ └──────────────────┘

     ┌──────────────────────────────────┐
     │         DATA LAYER               │
     │  PostgreSQL    │   ChromaDB      │
     │  (main DB)     │   (vectors /    │
     │                │    long-term    │
     │                │    memory)      │
     └──────────────────────────────────┘
```

---

## Components

| Component | Role | Details |
|-----------|------|---------|
| **OpenClaw + Gemini 3 Pro** | Brain | Reasoning, web search, Telegram chat, multi-turn conversations |
| **JARVIS Orchestrator** | Skill / Executor | Voice processing, home control (single + bulk), speaker ID, security enforcement |
| **Qwen 7B Q4** | Pre-router | Local Ollama model for domotics fast path and offline fallback |
| **Whisper** | Speech-to-Text | Local model, low-latency transcription |
| **Resemblyzer** | Speaker ID | Voice biometric identification (embedded in orchestrator) |
| **PostgreSQL** | Main database | Users, locations, entity maps, preferences, audit log |
| **ChromaDB** | Vector store | Long-term memory, semantic search, hybrid retrieval |
| **Home Assistant** | Domotics core | One instance per location, connected via WebSocket |
| **AtomS3R** | Voice input | ESP32-S3 devices with wake word "Jarvis", one per room |

---

## Docker Services

| Service | Image / Build | Port | GPU | Purpose |
|---------|---------------|------|-----|---------|
| `ollama` | ollama/ollama | 11434 | Yes | Qwen3.5 4B for pre-routing |
| `whisper` | faster-whisper | 9000 | Yes | Local speech-to-text |
| `orchestrator` | ./orchestrator | 5000 | No | Core FastAPI app + Resemblyzer + Admin UI |
| `openclaw` | openclaw | 8080 | No | Brain (Gemini 3 Pro gateway) |
| `postgres` | postgres:16 | 5432 | No | Main relational database |
| `chromadb` | chromadb | 8000 | No | Vector store for long-term memory |

---

## Security Model (L1 - L4)

JARVIS enforces four security levels based on action risk:

| Level | Name | Actions | Enforcement |
|-------|------|---------|-------------|
| **L1** | Auto-approve | Lights on/off, sensor reads, simple chat | Immediate execution |
| **L2** | Log-only | Climate changes, cover control | Executed + audit logged |
| **L3** | Confirm | Lock/unlock, alarm, cover open/close | Requires Telegram approval |
| **L4** | Blocked | Payments, deletions, credential access | Always rejected |

Additional protections:
- **Speaker ID**: Resemblyzer biometric voice matching (threshold > 75%)
- **Prompt injection detection**: Commands containing meta-instructions trigger `SECURITY_ALERT`
- **Telegram whitelist**: Per-user `telegram_id` linking
- **Pending action timeout**: Unconfirmed L3 actions expire after 1 hour
- **Audit log**: Every action is logged with speaker, source, location, and timestamp

---

## Multi-Location Support

JARVIS manages multiple Home Assistant instances (e.g., Milan apartment + Naples villa):

```
JARVIS Orchestrator
       │
       ├──── Home Assistant "ALBANI" (Milano)  :8123
       │
       └──── Home Assistant "WAGMI"  (Napoli)  :8123
```

Location resolution priority:
1. **Explicit** -- keyword in command ("turn on lights in Milan")
2. **Voice device** -- AtomS3R `device_id` maps to a location
3. **Telegram sticky** -- user selects location via inline keyboard
4. **Fallback** -- ask user to choose

Each location has its own entity map, memory sidecar, and HA token stored in PostgreSQL.

---

## Quick Start

1. Copy the environment template and fill in your credentials:
   ```bash
   cp .env.example .env
   ```

2. Follow the full setup guide:
   ```
   See infrastructure/README.md (locale) or cloud/README.md (VPS)
   ```

3. Start the stack:
   ```bash
   docker compose up -d
   ```

4. Open the admin dashboard at `http://jarvis:5000/admin` to:
   - Enroll family voice profiles
   - Sync entity maps from Home Assistant
   - Configure locations and preferences

---

## Project Structure

```
jarvis/
├── orchestrator/           # Core FastAPI app
│   ├── main.py             # Routing, voice pipeline, Telegram webhook, WS operator client
│   ├── config.py           # Service URLs, timeouts, security rules
│   ├── database.py         # PostgreSQL: users, locations, entities, memory
│   ├── ai_engines.py       # Pre-routing (Qwen) + OpenClaw/Gemini calls
│   ├── tools_api.py        # OpenClaw skill endpoints (11 REST tools incl. entity_bulk)
│   ├── integrations.py     # Home Assistant, Telegram, audio feedback
│   ├── voice_recognition.py# Resemblyzer speaker ID
│   ├── security_levels.py  # L1-L4 enforcement, domain/channel security
│   ├── context_builder.py  # Hybrid context (PostgreSQL + ChromaDB)
│   ├── vector_store.py     # ChromaDB vector store + embeddings
│   ├── memory_jobs.py      # Scheduled summarization + fact extraction
│   ├── multi_ha.py         # Multi-location HA manager (single + bulk ops)
│   ├── admin_api.py        # Admin dashboard API
│   └── templates/          # Admin UI (HTML/JS)
├── config/
│   └── system_prompt.txt   # Optimized system prompt
├── docker-compose.yml      # Full local stack
└── .env.example            # Environment variable template
```

---

## Key Design Decisions

- **OpenClaw + Gemini 3 Pro as Brain**: All reasoning, web search, and conversational intelligence is handled by OpenClaw backed by Gemini 3 Pro. No local reasoning model needed.
- **Qwen 7B Q4 as pre-router only**: Fast local intent classification for domotics commands. Falls back to offline responses when cloud is unreachable.
- **No external search APIs**: Web search is handled natively by Gemini 3 Pro through OpenClaw.
- **PostgreSQL over SQLite**: Production-grade relational store for all structured data.
- **ChromaDB for memory**: Semantic vector search enables long-term contextual recall across conversations.
- **Speaker biometrics**: Resemblyzer runs inside the orchestrator process -- no separate container needed.
