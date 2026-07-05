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
         |  +-----+------+   +--------------+           |
         |  | Resemblyzer |   | fastembed    |          |
         |  | (speaker ID)|   | :11435 (CPU) |          |
         |  | biometric   |   | ONNX embed   |          |
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
         | HOME     AI AGENT   CHAT                    |
         | CONTROL  (Brain)    (AI Agent)               |
         |   |         |          |                     |
         +---+---------+----------+---------------------+
             |         |          |
             v         v          v
     +------------+ +-------------------+  +------------------+
     |   Home     | |   AI Agent +      |  |  Ontology Server |
     | Assistant  | |  Cloud LLM        |  |  Knowledge Graph |
     | (per loc.) | |  (Brain)          |  |  (:8100)         |
     +------------+ +-------------------+  +------------------+

     +--------------------------------------------+
     |    GX10 DGX Spark (via Tailscale)           |
     |  Canary STT :9000    | CosyVoice3 :9880    |
     |  (nvidia/canary-1b-  | (0.5B, zero-shot     |
     |   v2, forced IT)     |  voice cloning, IT)  |
     |  Brave Search (web tool)                    |
     +--------------------------------------------+

     +--------------------------------------------+
     |         DATA LAYER                          |
     |                                             |
     |  L1 HOT      SQLite chat_memory             |
     |              (raw + meta JSON: route,       |
     |               ha_entity_id, ha_action,...)  |
     |                                             |
     |  L2 SHORT    Redis :6379 (context bus,      |
     |              cross-system, TTL 30m)         |
     |                                             |
     |  L3 LONG     mem0-stack (esterno)           |
     |              MEM0_BASE_URL                  |
     |              croll83/mem0-stack             |
     |              ↑ popolato da habit_extraction |
     |                                             |
     |  PostgreSQL     | MongoDB                   |
     |  (side projects)| (side projects)           |
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
| **AI Agent (Hermes/OpenClaw/others)** | Brain | Reasoning, web search, Telegram chat, multi-turn conversations |
| **JARVIS Orchestrator** | Skill / Executor | Voice processing, home control (single + bulk), speaker ID, security enforcement |
| **Qwen 2.5 3B** | Pre-router + Tool calling | Local Ollama model for domotics fast path, tool calling (web_search, web_fetch, memory_search, home_status), offline fallback |
| **Canary STT** | Speech-to-Text | nvidia/canary-1b-v2 on GX10 DGX Spark (:9000), forced Italian via source_lang (Parakeet's auto-LID misdetected IT→RU on short audio), ~130-180ms per phrase |
| **CosyVoice3** | Text-to-Speech | Fun-CosyVoice3-0.5B on GX10, zero-shot voice cloning, Italian text normalization via num2words |
| **Resemblyzer** | Speaker ID | Voice biometric identification (embedded in orchestrator) |
| **Ontology Server** | Knowledge Graph | Entity/relation graph with speaker-based ACL, SQLite + FastAPI |
| **fastembed (nomic-embed-text-v1.5)** | Embeddings | 768-dim CPU-only ONNX embeddings (Ollama-compatible API :11435) for orchestrator, ha-memory-service, and AI Agent |
| **Brave Search** | Web Search Tool | Web search API used by Qwen tool calling |
| **SQLite `chat_memory`** | L1 HOT memory (in orchestrator) | Raw rows with `meta` JSON (route, payload, ha_entity_id, ha_action, ha_params, ha_status). Source for the nightly habit-extraction job |
| **Redis** | L2 Context Bus | Cross-system short-term memory (TTL 30min). Shared between orchestrator, HA memory service, and Hermes. Per-user event lists with source filtering |
| **mem0-stack** (esterno) | L3 Long-term semantic + procedural memory | Servizio esterno (repo `croll83/mem0-stack`). API HTTP `/search`, `/search_contextual`, `/add`, `/memories/*`, `/reasoning_bank/*`. Consumato via `MEM0_BASE_URL`. Popolato dal job notturno `habit_extraction` (ibrido SQL + LLM, `agent_id=jarvis-habit-extractor`) |
| **PostgreSQL** | Database | Side projects (relational store) |
| **MongoDB** | Database | Side projects (document store) |
| **Home Assistant** | Domotics core | One instance per location, connected via WebSocket |
| **AtomS3R** | Voice input | ESP32-S3 devices with wake word "Jarvis", one per room |

---

## Docker Services

| Service | Image / Build | Port | GPU | Purpose |
|---------|---------------|------|-----|---------|
| `ollama` | ollama/ollama | 11434 | Yes | Qwen 2.5 3B (LLM only) |
| `fastembed` | ./infrastructure/fastembed | 11435 | No | nomic-embed-text-v1.5 embeddings (CPU ONNX) |
| `orchestrator` | ./jarvis-orchestrator | 5000 | No | Core FastAPI app + Resemblyzer + Admin UI (host network, TTS via CosyVoice3@GX10) |
| `redis` | redis:7-alpine | 6379 | No | Cross-system context bus (on LXC Jarvis) |
| `ontology-server` | ./ontology-server | 127.0.0.1:8100 | No | Knowledge Graph API (SQLite + ACL) |
| `postgres` | postgres:16-alpine | 5432 | No | Relational database (side projects) |
| `mongo` | mongo:7 | 27017 | No | Document database (side projects) |

> **Note**: AI Agent runs on a dedicated host (`100.116.99.9`), not in this Docker stack.
> **Note**: STT (Canary `:9000`, systemd unit `parakeet-stt` — historical name) and TTS (CosyVoice3 `:9880`) run on GX10 DGX Spark (`100.98.187.12`) as systemd services, reachable via Tailscale. Port `:7865` hosts ACE-Step 1.5 (music generation, lazy pipeline) — not part of the voice pipeline. STT has always listened on `:9000` and ACE-Step on `:7865`: no port swap ever happened (only the STT backend model changed inside the same wrapper).

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
| `your-agent-host` | Nginx TLS on AI Agent host (Tailscale only) | AI Agent gateway API |

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
|   +-- ai_engines.py          # Pre-routing (Qwen) + AI Agent dispatch
|   +-- tools_api.py           # AI Agent skill endpoints (11 REST tools incl. entity_bulk)
|   +-- integrations.py        # Home Assistant, Telegram, audio feedback
|   +-- voice_recognition.py   # Resemblyzer speaker ID
|   +-- security_levels.py     # L1-L4 enforcement, domain/channel security
|   +-- context_builder.py     # Hybrid context (SQLite + Redis)
|   +-- context_bus.py         # Redis context bus (cross-system short-term memory)
|   +-- memory_jobs.py         # Daily scheduler: habit_extraction → mem0 + chat_memory HOT cleanup
|   +-- habit_extraction.py    # Hybrid SQL (HOME_CONTROL aggregation) + LLM (preference/topic) → mem0
|   +-- multi_ha.py            # Multi-location HA manager (single + bulk ops)
|   +-- internal_tts.py        # TTS backend (CosyVoice3 on GX10 / Kokoro cloud)
|   +-- admin_api.py           # Admin dashboard API
|   +-- templates/             # Admin UI (HTML/JS)
+-- ha_memory_service/         # HA location memory (events → Redis + SQLite summaries + mem0)
|   +-- main.py                # Event ingestion, summaries, Redis push, mem0 daily extraction
|   +-- context_bus.py         # Redis context bus (shared module with orchestrator)
+-- ontology-server/           # Knowledge Graph API (SQLite + FastAPI + ACL)
|   +-- api.py                 # FastAPI endpoints (12 routes)
|   +-- ontology.py            # Core graph logic + speaker-based ACL
|   +-- helpers.py             # Query helpers
|   +-- schema.yaml            # Entity/relation schema definitions
+-- infrastructure/            # Infra-as-code
|   +-- whisper-custom-deprecated/  # Custom Whisper Dockerfile (DEPRECATED — STT on GX10)
|   +-- xtts-custom-deprecated/    # Custom XTTSv2 Dockerfile (DEPRECATED — TTS on GX10)
|   +-- gb10/                  # GX10 DGX Spark documentation
|   +-- terraform/             # Terraform configs
|   +-- ansible/               # Ansible playbooks
+-- wakeword-server/           # Wake word model training / serving
+-- config/
|   +-- router_system_prompt.txt  # Qwen router system prompt (loaded as SYSTEM_RULES)
+-- speakers/                  # WAV reference files for voice cloning (legacy XTTSv2)
+-- docker-compose.yml         # Full local stack (GPU)
+-- .env.example               # Environment variable template
```

---

## Key Design Decisions

- **AI Agent as Brain**: All reasoning, web search, and conversational intelligence is handled by the AI Agent (Hermes/OpenClaw/others) backed by a Cloud LLM. The AI_AGENT intent routes complex queries, uncertain domotics, and general conversation to the brain.
- **Qwen 2.5 3B with tool calling**: Fast local pre-routing for domotics commands plus tool calling capabilities (web_search via Brave API, web_fetch, memory_search, home_status). Falls back to offline responses when cloud is unreachable.
- **Brave Search API**: Web search tool available to both Qwen (via tool calling) and the AI Agent (via skill), providing real-time web information.
- **fastembed for all embeddings**: Single 768-dim embedding model (nomic-embed-text-v1.5 via ONNX, CPU-only) served by a dedicated container on port 11435 with Ollama-compatible API. Runs on CPU to avoid CUDA context switching with Qwen on the GPU, reducing routing latency from ~3.5s to ~0.5s.
- **Three-layer memory (decoupled)**:
  1. **L1 HOT — SQLite `chat_memory`** in the orchestrator (raw, last ~30 min). Each row carries a `meta` JSON column with the routing decision (`route`, `confidence`, `payload`) and, for `HOME_CONTROL`, the HA outcome (`ha_entity_id`, `ha_action`, `ha_params`, `ha_status`, …). No more hourly/daily SQL summaries — those layers were removed.
  2. **L2 Short-term — Redis context bus** (`ctx:{user_id}:events`, TTL 30 min, capped 20, source-filtered) shared between orchestrator, `ha_memory_service`, and Hermes.
  3. **L3 Long-term — mem0-stack** (external, repo `croll83/mem0-stack`, accessed via `MEM0_BASE_URL`). Populated by the nightly `habit_extraction` job, which uses a **hybrid SQL + LLM** pipeline: deterministic SQL aggregation over `chat_memory.meta` for domotics habits (entity + action + time window + value), Qwen LLM only for preferences/topics on non-HOME_CONTROL messages. Records are tagged `agent_id=jarvis-habit-extractor` for filtering in the Hermes mem0 dashboard.
- **Redis context bus**: Shared between orchestrator, HA memory service, and Hermes. Each system writes events tagged with its source and reads only events from other sources, preventing self-duplication. Per-user event lists (`ctx:{user_id}:events`), capped at 20, TTL 30 minutes.
- **Canary STT on GX10**: nvidia/canary-1b-v2 on GX10 DGX Spark (128 GB unified memory). Replaced Parakeet-TDT v3 (jul 2026): Parakeet's transcribe() exposes no language kwarg and its auto-LID misdetected short Italian audio as Russian; Canary is a multitask model with native source_lang/target_lang forcing (~130-180ms/phrase). A Cyrillic guard in the orchestrator discards residual misdetections and asks the user to repeat (optional Groq whisper rescue if GROQ_API_KEY is set).
- **CosyVoice3 on GX10**: Fun-CosyVoice3-0.5B with zero-shot voice cloning from reference audio. Server-side Italian text normalization (num2words) for correct number/unit pronunciation. Replaces Qwen3-TTS (deprecated). OpenAI-compatible API, ~0.6x RTF, ~3.6 GiB VRAM.
- **Nginx + Cloudflare Tunnel**: Public endpoints (Telegram webhook, health) served via Cloudflare Tunnel with no port forwarding. Internal services accessible only through Tailscale mesh.
- **Speaker biometrics**: Resemblyzer runs inside the orchestrator process -- no separate container needed.
- **Ontology Server**: Centralized knowledge graph with speaker-based ACL, serving as the single source of truth for entities and relations across the agent ecosystem.
