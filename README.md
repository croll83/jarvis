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
     |    GX10 DGX Spark (via Tailscale)           |
     |  Parakeet STT :7865  | Qwen3-TTS :9880     |
     |  (nvidia/parakeet-   | (voice cloning,      |
     |   tdt-0.6b-v3)       |  IT/EN voices)       |
     |  Brave Search (web tool)                    |
     +--------------------------------------------+

     +--------------------------------------------+
     |         DATA LAYER                          |
     |  ChromaDB :8000 | PostgreSQL | MongoDB      |
     |  (shared server | (side      | (side        |
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
| **Parakeet STT** | Speech-to-Text | nvidia/parakeet-tdt-0.6b-v3 on GX10 DGX Spark, multilingual auto-detection, 20x realtime |
| **Qwen3-TTS** | Text-to-Speech | Qwen3-TTS-12Hz-1.7B on GX10, voice cloning, Italian/English voices (sofia, marco, emma, james) |
| **Resemblyzer** | Speaker ID | Voice biometric identification (embedded in orchestrator) |
| **Ontology Server** | Knowledge Graph | Entity/relation graph with speaker-based ACL, SQLite + FastAPI |
| **fastembed (nomic-embed-text-v1.5)** | Embeddings | 768-dim CPU-only ONNX embeddings (Ollama-compatible API :11435) for orchestrator, ha-memory-service, and OpenClaw |
| **Brave Search** | Web Search Tool | Web search API used by Qwen tool calling |
| **ChromaDB (shared server)** | Vector store | Long-term memory, semantic search, hybrid retrieval. Shared server on :8000, used by orchestrator, ha-memory-service, and ontology-bridge plugin (fallback to embedded PersistentClient if unreachable) |
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
| `orchestrator` | ./jarvis-orchestrator | 5000 | No | Core FastAPI app + Resemblyzer + Admin UI (host network) |
| `chromadb` | chromadb/chroma:0.6.3 | 127.0.0.1:8000 | No | Shared vector store (used by orchestrator, ha-memory-service, ontology-bridge) |
| `ontology-server` | ./ontology-server | 127.0.0.1:8100 | No | Knowledge Graph API (SQLite + ACL) |
| `postgres` | postgres:16-alpine | 5432 | No | Relational database (side projects) |
| `mongo` | mongo:7 | 27017 | No | Document database (side projects) |

> **Note**: OpenClaw runs bare-metal on a dedicated LXC (`100.116.99.9`), not in this Docker stack.
> **Note**: STT (Parakeet `:7865`) and TTS (Qwen3-TTS `:9880`) run on GX10 DGX Spark (`100.98.187.12`) as systemd services, reachable via Tailscale.

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
|   +-- vector_store.py        # ChromaDB HttpClient (shared :8000) with embedded fallback
|   +-- memory_jobs.py         # Scheduled summarization + fact extraction
|   +-- multi_ha.py            # Multi-location HA manager (single + bulk ops)
|   +-- internal_tts.py        # TTS backend (Qwen3-TTS on GX10 / Kokoro cloud)
|   +-- admin_api.py           # Admin dashboard API
|   +-- templates/             # Admin UI (HTML/JS)
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
+-- openclaw/                  # OpenClaw deployment config
|   +-- xtts-proxy/            # TTS proxy (systemd on OpenClaw LXC, now points to Qwen3-TTS)
|   +-- skills/                # OpenClaw skill definitions
|   +-- extensions/            # OpenClaw extensions
+-- wakeword-server/           # Wake word model training / serving
+-- config/
|   +-- router_system_prompt.txt  # Qwen router system prompt (loaded as SYSTEM_RULES)
+-- speakers/                  # WAV reference files for voice cloning (legacy XTTSv2)
+-- docker-compose.yml         # Full local stack (GPU)
+-- .env.example               # Environment variable template
```

---

## Key Design Decisions

- **OpenClaw + Gemini 3 Pro as Brain**: All reasoning, web search, and conversational intelligence is handled by OpenClaw backed by Gemini 3 Pro. The OPENCLAW intent routes complex queries, uncertain domotics, and general conversation to the brain.
- **Qwen 2.5 3B with tool calling**: Fast local pre-routing for domotics commands plus tool calling capabilities (web_search via Brave API, web_fetch, memory_search, home_status). Falls back to offline responses when cloud is unreachable.
- **Brave Search API**: Web search tool available to both Qwen (via tool calling) and OpenClaw (via skill), providing real-time web information.
- **fastembed for all embeddings**: Single 768-dim embedding model (nomic-embed-text-v1.5 via ONNX, CPU-only) served by a dedicated container on port 11435 with Ollama-compatible API. Runs on CPU to avoid CUDA context switching with Qwen on the GPU, reducing routing latency from ~3.5s to ~0.5s.
- **ChromaDB shared server**: Vector store runs as a dedicated container on :8000 (`chromadb/chroma:0.6.3`), shared by orchestrator, ha-memory-service, and the ontology-bridge plugin. Clients use `HttpClient` with automatic fallback to embedded `PersistentClient` if the server is unreachable.
- **Parakeet STT on GX10**: nvidia/parakeet-tdt-0.6b-v3 running on GX10 DGX Spark (128 GB VRAM). Multilingual auto-detection, 20x realtime, no initial prompt needed. Replaces Whisper (deprecated) to free VRAM on Atomman for router upgrades.
- **Qwen3-TTS on GX10**: Qwen3-TTS-12Hz-1.7B with voice cloning, Italian/English preset voices (sofia, marco, emma, james). Replaces XTTSv2 (deprecated). OpenAI-compatible API.
- **Nginx + Cloudflare Tunnel**: Public endpoints (Telegram webhook, health) served via Cloudflare Tunnel with no port forwarding. Internal services accessible only through Tailscale mesh.
- **Speaker biometrics**: Resemblyzer runs inside the orchestrator process -- no separate container needed.
- **Ontology Server**: Centralized knowledge graph with speaker-based ACL, serving as the single source of truth for entities and relations across the agent ecosystem.
