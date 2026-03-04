# JARVIS - Smart Home AI Assistant

> Multi-location smart home AI assistant with voice control, reasoning, speaker identification, and security enforcement.

---

## Architecture Overview

```
                           INPUTS
              +--------------+--------------+
              |  AtomS3R     |   Telegram    |
              |  (voice)     |   (text)      |
              +------+-------+------+--------+
                     |              |
         +-----------v--------------v-------------------+
         |        JARVIS ORCHESTRATOR                   |
         |        (FastAPI - :5000)                     |
         |                                              |
         |  +------------+   +--------------+           |
         |  | Whisper     |   | Resemblyzer  |          |
         |  | large-v3-   |   | (speaker ID) |          |
         |  | turbo :9000 |   | biometric    |          |
         |  +-----+------+   +------+-------+          |
         |        |                  |                  |
         |        v                  v                  |
         |  +-------------------------------------+     |
         |  |     Qwen 2.5 3B (Ollama :11434)     |     |
         |  |     Pre-routing / tool calling       |     |
         |  |     Tools: web_search, web_fetch,    |     |
         |  |       memory_search, home_status     |     |
         |  +----------+--------------------------+     |
         |             |                                |
         |   +---------+----------+                     |
         |   |         |          |                     |
         |   v         v          v                     |
         | HOME     OPENCLAW    CHAT                    |
         | CONTROL  (Brain)    (OpenClaw)                |
         |   |         |          |                     |
         +---+---------+----------+---------------------+
             |         |          |
             v         v          v
     +------------+ +-------------------+  +------------------+
     |   Home     | |   OpenClaw +      |  |  Ontology Server |
     | Assistant  | |  Gemini 3 Pro     |  |  Knowledge Graph |
     | (per loc.) | |  (Brain)          |  |  (:8100)         |
     +------------+ +-------------------+  +------------------+

     +--------------------------------------------+
     |           AI / MEDIA SERVICES               |
     |  XTTSv2       | nomic-embed-text            |
     |  (TTS voice   | (embeddings, 768-dim)       |
     |  cloning)     |                              |
     |  :8890        | Brave Search (web tool)      |
     +--------------------------------------------+

     +--------------------------------------------+
     |         DATA LAYER                          |
     |  Chroma         | PostgreSQL | MongoDB      |
     |  (embedded,     | (side      | (side        |
     |   vector store) | projects)  | projects)    |
     +--------------------------------------------+

     +--------------------------------------------+
     |         PUBLIC ACCESS                       |
     |  Nginx + Cloudflare Tunnel (LXC-JARVIS)    |
     +--------------------------------------------+
```

---

## Components

| Component | Role | Details |
|-----------|------|---------|
| **OpenClaw + Gemini 3 Pro** | Brain | Reasoning, web search, Telegram chat, multi-turn conversations |
| **JARVIS Orchestrator** | Skill / Executor | Voice processing, home control (single + bulk), speaker ID, security enforcement |
| **Qwen 2.5 3B** | Pre-router + Tool calling | Local Ollama model for domotics fast path, tool calling (web_search, web_fetch, memory_search, home_status), offline fallback |
| **Whisper large-v3-turbo** | Speech-to-Text | Local model (custom Blackwell image, int8_float16), low-latency transcription |
| **XTTSv2** | Text-to-Speech | GPU-accelerated voice cloning (custom Blackwell image, Italian voice) |
| **Resemblyzer** | Speaker ID | Voice biometric identification (embedded in orchestrator) |
| **Ontology Server** | Knowledge Graph | Entity/relation graph with speaker-based ACL, SQLite + FastAPI |
| **nomic-embed-text** | Embeddings | 768-dim embeddings for orchestrator, ha-memory-service, and OpenClaw |
| **Brave Search** | Web Search Tool | Web search API used by Qwen tool calling |
| **Chroma (embedded)** | Vector store | Long-term memory, semantic search, hybrid retrieval (embedded in orchestrator via chroma/ dir) |
| **PostgreSQL** | Database | Side projects (relational store) |
| **MongoDB** | Database | Side projects (document store) |
| **Home Assistant** | Domotics core | One instance per location, connected via WebSocket |
| **AtomS3R** | Voice input | ESP32-S3 devices with wake word "Jarvis", one per room |

---

## Docker Services

| Service | Image / Build | Port | GPU | Purpose |
|---------|---------------|------|-----|---------|
| `ollama` | ollama/ollama | 11434 | Yes | Qwen 2.5 3B + nomic-embed-text |
| `whisper` | jarvis/whisper-blackwell | 9000 | Yes | Local STT (large-v3-turbo, int8_float16) |
| `xtts` | jarvis/xtts-blackwell | 8890 | Yes | TTS voice cloning (XTTSv2, Italian) |
| `orchestrator` | ./jarvis-orchestrator | 5000 | No | Core FastAPI app + Resemblyzer + Chroma + Admin UI (host network) |
| `ontology-server` | ./ontology-server | 127.0.0.1:8100 | No | Knowledge Graph API (SQLite + ACL) |
| `postgres` | postgres:16-alpine | 5432 | No | Relational database (side projects) |
| `mongo` | mongo:7 | 27017 | No | Document database (side projects) |

> **Note**: OpenClaw runs bare-metal on a dedicated LXC (`100.116.99.9`), not in this Docker stack. Chroma is embedded in the orchestrator process (no separate container).

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
       |
       +---- Home Assistant "ALBANI" (Milano)  :8123
       |
       +---- Home Assistant "WAGMI"  (Napoli)  :8123
```

Location resolution priority:
1. **Explicit** -- keyword in command ("turn on lights in Milan")
2. **Voice device** -- AtomS3R `device_id` maps to a location
3. **Telegram sticky** -- user selects location via inline keyboard
4. **Fallback** -- ask user to choose

Each location has its own entity map, memory sidecar, and HA token stored in the database.

---

## Public Access

| Domain | Method | Scope |
|--------|--------|-------|
| `jarvis.mintwork.it` | Nginx + SSL (Tailscale only) | Internal services, admin UI |
| `jarvis-pub.mintwork.it` | Cloudflare Tunnel | Telegram webhook, health endpoint |
| `openclaw.mintwork.it` | Nginx TLS on OpenClaw LXC (Tailscale only) | OpenClaw gateway API |

- **No port forwarding** -- all public traffic routes through Cloudflare Tunnel
- Internal services are accessible only via Tailscale mesh network
- Nginx handles TLS termination and reverse proxying on both LXCs

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
+-- jarvis-orchestrator/       # Core FastAPI app
|   +-- main.py                # Routing, voice pipeline, Telegram webhook, WS operator client
|   +-- config.py              # Service URLs, timeouts, security rules
|   +-- database.py            # PostgreSQL: users, locations, entities, memory
|   +-- ai_engines.py          # Pre-routing (Qwen) + OpenClaw dispatch
|   +-- tools_api.py           # OpenClaw skill endpoints (11 REST tools incl. entity_bulk)
|   +-- integrations.py        # Home Assistant, Telegram, audio feedback
|   +-- voice_recognition.py   # Resemblyzer speaker ID
|   +-- security_levels.py     # L1-L4 enforcement, domain/channel security
|   +-- context_builder.py     # Hybrid context (PostgreSQL + Chroma)
|   +-- vector_store.py        # Chroma vector store + nomic-embed-text embeddings
|   +-- memory_jobs.py         # Scheduled summarization + fact extraction
|   +-- multi_ha.py            # Multi-location HA manager (single + bulk ops)
|   +-- internal_tts.py        # Dual TTS backend (XTTSv2 local / Kokoro cloud)
|   +-- admin_api.py           # Admin dashboard API
|   +-- chroma/                # Embedded Chroma data directory
|   +-- templates/             # Admin UI (HTML/JS)
+-- ontology-server/           # Knowledge Graph API (SQLite + FastAPI + ACL)
|   +-- api.py                 # FastAPI endpoints (12 routes)
|   +-- ontology.py            # Core graph logic + speaker-based ACL
|   +-- helpers.py             # Query helpers
|   +-- schema.yaml            # Entity/relation schema definitions
+-- infrastructure/            # Infra-as-code
|   +-- whisper-custom/        # Custom Whisper Dockerfile (Blackwell GPU)
|   +-- xtts-custom/           # Custom XTTSv2 Dockerfile (Blackwell GPU)
|   +-- terraform/             # Terraform configs
|   +-- ansible/               # Ansible playbooks
+-- openclaw/                  # OpenClaw deployment config
|   +-- xtts-proxy/            # XTTS OpenAI-compat proxy (systemd on OpenClaw LXC)
|   +-- skills/                # OpenClaw skill definitions
|   +-- extensions/            # OpenClaw extensions
+-- wakeword-server/           # Wake word model training / serving
+-- config/
|   +-- router_system_prompt.txt  # Qwen router system prompt (loaded as SYSTEM_RULES)
+-- speakers/                  # WAV reference files for XTTSv2 voice cloning
+-- docker-compose.yml         # Full local stack (GPU)
+-- .env.example               # Environment variable template
```

---

## Key Design Decisions

- **OpenClaw + Gemini 3 Pro as Brain**: All reasoning, web search, and conversational intelligence is handled by OpenClaw backed by Gemini 3 Pro. The OPENCLAW intent routes complex queries, uncertain domotics, and general conversation to the brain.
- **Qwen 2.5 3B with tool calling**: Fast local pre-routing for domotics commands plus tool calling capabilities (web_search via Brave API, web_fetch, memory_search, home_status). Falls back to offline responses when cloud is unreachable.
- **Brave Search API**: Web search tool available to both Qwen (via tool calling) and OpenClaw (via skill), providing real-time web information.
- **nomic-embed-text for all embeddings**: Single 768-dim embedding model (via Ollama) used across orchestrator, ha-memory-service, and OpenClaw for consistent semantic search.
- **Chroma embedded in orchestrator**: Vector store runs in-process (no separate container), simplifying deployment and reducing resource overhead.
- **XTTSv2 for TTS**: GPU-accelerated voice cloning with custom Blackwell image. Italian voice via WAV reference files. Cloud fallback to Kokoro-82M (CPU).
- **Custom Dockerfiles for GPU services**: Whisper and XTTS use custom images (`jarvis/whisper-blackwell`, `jarvis/xtts-blackwell`) built for CUDA 12.9 / Blackwell sm_120 support.
- **Nginx + Cloudflare Tunnel**: Public endpoints (Telegram webhook, health) served via Cloudflare Tunnel with no port forwarding. Internal services accessible only through Tailscale mesh.
- **Speaker biometrics**: Resemblyzer runs inside the orchestrator process -- no separate container needed.
- **Ontology Server**: Centralized knowledge graph with speaker-based ACL, serving as the single source of truth for entities and relations across the agent ecosystem.
