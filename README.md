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
         |  |  ROUTER — catena configurabile       |     |
         |  |  1. GLiNER (locale) p50 ~70ms        |     |
         |  |     gliner-router :11436, GPU        |     |
         |  |     classificazione su vocabolari    |     |
         |  |     chiusi + regole in router_model  |     |
         |  |            | passa la mano           |     |
         |  |            v                         |     |
         |  |  2. Jev (TypeSafe, cloud) ~470ms     |     |
         |  |     opzionale, a consumo             |     |
         |  |            | passa la mano           |     |
         |  |            v                         |     |
         |  |  3. Qwen 3.5 4B Q6_K  ~1200ms        |     |
         |  |     llama-server :30000 — ULTIMO     |     |
         |  |     ANELLO, mai disattivabile:       |     |
         |  |     e' il solo che GENERA testo      |     |
         |  |     libero, e regge l'offline        |     |
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
     |  Parakeet STT :9000  | CosyVoice3 :9880    |
     |  (0.6B, auto-LID +   | (0.5B, zero-shot     |
     |   enhance+diarize)   |  voice cloning, IT)  |
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
| **GLiNER 2.5 multi-v1** | Primary router | Local encoder classifier (287M, mDeBERTa-v3) running as its own service (`gliner-router` :11436, ~1.65 GiB VRAM). Decides over **closed vocabularies** built from `router_model` + the live HA entity map, plus Italian-language rules in code. Measured on a 426-case bench: intent 90.8%, target 84.0%, action 88.6%, payload 74.5%, **p50 68ms**. Active when `GLINER_URL` is set |
| **Jev** (TypeSafe System One) | Optional router stage | Cloud, non-generative: one call returns typed+calibrated answers for 12 questions evaluated in parallel. p50 ~470ms. Same bench: intent 89.2%, target 73.2%, payload 66.4%. Active when `JEV_API_KEY` is set — sits between GLiNER and Qwen |
| **Qwen 3.5 4B Q6_K** | Last stage, never removed + Tool calling | llama-server :30000. Takes over whenever the stages above pass (free-text slot, whole-house command, low confidence) or are unreachable. Also does TTS preprocessing, habit summarisation and tool calling. Keeps the house working with no WAN. Replaced Qwen 2.5 7B: measured better on the same bench (payload 71.4% vs 68.3%) while freeing 2.2 GB of VRAM. **It is a reasoning model** — see `ROUTER_CHAT_TEMPLATE_KWARGS` |
| **Parakeet STT** | Speech-to-Text | nvidia/parakeet-tdt-0.6b-v3 on GX10 DGX Spark (:9000), auto-LID + Cyrillic guard (swapped from Canary back on 2026-09-12). Extended 2026-09-25 with DeepFilterNet3 enhance + Nemotron 3 Diarization upstream — see wiki `audiofront-service` |
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
| _(host)_ `llama-router.service` | llama.cpp turboquant | 30000 | Yes | Qwen 3.5 4B Q6_K — systemd sull'host, non in Docker |
| _(host)_ `gliner-router.service` | venv `~/gliner-eval` | 11436 | Yes | GLiNER 2.5 multi-v1 — systemd sull'host: il container ha torch **CPU**, e su CPU il bersaglio costa 1370ms contro 34ms in GPU |
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
|   +-- ROUTING.md            # DOVE SI AGISCE: regole, etichette, proprieta' del router
|   +-- router_model.py        # FONTE UNICA: intenti, azioni, domini, grandezze + regole di lingua
|   +-- gliner_engine.py       # Decisore primario GLiNER (etichette e ORDINE — rimisurare se si tocca)
|   +-- jev_engine.py          # Decisore opzionale Jev (12 domande tipizzate)
|   +-- render_flat.py         # Genera il prompt di Qwen da router_model (dietro ROUTER_PROMPT_GENERATO)
|   +-- ai_engines.py          # Percorre config.ROUTING_CHAIN + AI Agent dispatch
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
+-- gliner-router/             # Servizio di scoring GLiNER (systemd sull'host, :11436)
+-- wakeword-server/           # Wake word model training / serving
+-- config/
|   +-- router_system_prompt.txt  # Qwen router system prompt (loaded as SYSTEM_RULES)
|                                 # generabile da router_model: render_flat.py, dietro ROUTER_PROMPT_GENERATO
+-- speakers/                  # WAV reference files for voice cloning (legacy XTTSv2)
+-- docker-compose.yml         # Full local stack (GPU)
+-- .env.example               # Environment variable template
```

---

## Router — misure

Every routing claim in this README comes from a **shared bench of 426 real
cases** (`/home/jarvis/router-eval/`, deliberately outside this public repo: it
contains real household voice commands, people's names and the full entity map).
Cases come from production logs plus known STT corruptions; 141 are marked
*difficult*.

Two things to know before reading any number:

- **The measurement noise floor is 0.4-1.1 points** (two identical runs of the
  same baseline). Differences below ~2 points mean nothing.
- **Labelling error is ~7%**, measured on three 60-case samples. The ceiling is
  therefore ~93%, not 100%.

### The chain, stage by stage

Same bench, same denominators (intent over all 426; target/action/payload over
the 265 `HOME_CONTROL` cases):

| | GLiNER | Jev | Qwen 4B |
|---|---|---|---|
| intent | **90.8%** | 89.2% | 88.7% |
| target | **84.0%** | 73.2% | 71.7% |
| action | **88.6%** | 86.8% | 87.2% |
| payload (all three right) | **74.5%** | 66.4% | 71.4% |
| p50 latency | **68 ms** | 470 ms | 1171 ms |

⚠️ **Denominators are a trap here.** Jev's payload reads 70.2% over all 426
cases — non-`HOME_CONTROL` ones count as correct because there is no payload to
get wrong — but **66.4% over the 265 that actually have one**. Comparing the two
makes any engine look better than it is.

### What was measured and rejected

Kept here because they all sound obvious and are all wrong:

| Idea | Result |
|---|---|
| use the named device type (`"tapparella"` → cover) to pick the target | target 83.0% → **68.3%** — "le luci della cucina" wants the *scope*, not a device |
| give up when the action has no compatible domain in the target | 25 fallbacks in 265, and **15 of them were right** thanks to `normalizza_azione` |
| derive the action from the verb instead of the model | 77.0% vs **87.9%** |
| ask the model for the answer *source* instead of a code rule | 42.5% vs **90.8%** |
| split the target vocabulary into groups of ≤30 labels | monotonically worse: 20 → 62.7%, 30 → 66.2%, 50 → 70.4%, all → 77.7% |
| detect coreference as a GLiNER class | 0/34 — it is invisible to embedding similarity; 7/7 in code |

### Prompt work (Qwen stage)

| variant | tokens | intent | payload | target | collective |
|---|---|---|---|---|---|
| hand-written baseline | 5315 | 88.5% | 65.3% | 64.8% | 58.2% |
| generated from `router_model` | 2121 | **89.0%** | 61.3% | 59.6% | 67.1% |
| **surgical C (deployed)** | ~5000 | 88.7% | **68.3%** | **67.2%** | **79.5%** |

The generated prompt matches the hand-written one on intent with **half the
tokens** and gains 9 points on collective commands, but loses 7 points of
payload. Hence `ROUTER_PROMPT_GENERATO=false`: `render_flat.py` is in the repo,
off by default, with the numbers written next to it. Those measurements date
from when Qwen was the *primary* router — it is now the last stage and sees only
what GLiNER passes, a different and harder subset, on which the comparison has
**not** been repeated.

---

## Key Design Decisions

- **AI Agent as Brain**: All reasoning, web search, and conversational intelligence is handled by the AI Agent (Hermes/OpenClaw/others) backed by a Cloud LLM. The AI_AGENT intent routes complex queries, uncertain domotics, and general conversation to the brain.
- **The routing chain is configuration, not code**: a stage joins the chain if it is *configured* — `GLINER_URL` set → GLiNER; `JEV_API_KEY` set → Jev; neither → straight to Qwen. Qwen always closes the chain and cannot be disabled: it is the only stage that can **generate** free text (a search query, a song title, the body of an email). Each stage returns "not confident" and passes the ball. The order lives in `config.ROUTING_CHAIN`, is logged at startup, and `get_routing()` *walks* it rather than rebuilding it from nested `if`s. See `.env.example`, section *CATENA DI ROUTING*.
- **`router_model.py` is the single source of truth**: every enumerable thing — intents, actions, domains, measured quantities, answer sources — is declared once and read by all three stages. Before, these lived in two places (Qwen's prompt and `jev_engine`) with different wording, and they contradicted each other: the prompt sent weather to `SIMPLE_CHAT`, Jev sent it to `AI_AGENT`. Rooms and devices are never written down at all — they come from the HA entity map at runtime. **See [`jarvis-orchestrator/ROUTING.md`](jarvis-orchestrator/ROUTING.md) for where to act on any given change.**
- **Some rules belong in code, not in a model**: the boundary between a command and a question is *syntactic*, not semantic — "accendi la luce della cucina" and "quali luci sono accese in cucina?" name the same things with the same words and differ only in the verb's mood. A classifier scoring embedding similarity cannot see that. So `router_model` carries a small set of Italian-language rules: `natura()` (command vs question, 90.4% on the bench), courtesy commands ("puoi spegnere X?" *is* a command), `serve_strumento_esterno()` for `AI_AGENT` (21/21 with 0 false positives in 405), `stanza_nel_testo()` to reject targets that live in another room (+5.7 points), and coreference (`"ora spegnila"` → the last **executed** action, 7/7 with 0 false positives, 0.08ms, no model call).
- **Closed vocabularies beat extraction**: GLiNER's strong primitive is classification over a closed set, not entity extraction. Resolving the target by classifying over the house vocabulary scores 84.0% against 49.2% for extract-a-span-then-fuzzy-match. Its labels are injected into the same prompt as the text, so **their order matters**: on the target the score ranges from 63.1% to 75.8% purely by permutation. The orders in `gliner_engine.py` are the best measured — change them only with a re-measurement.
- **Prompt injection as an independent question**: asking the router to police the very prompt an injection is attacking is weak by construction. Jev scores it as a separate `noul`, in parallel, at no latency cost; GLiNER carries it as its own intent label.
- **Brave Search API**: Web search tool available to both Qwen (via tool calling) and the AI Agent (via skill), providing real-time web information.
- **fastembed for all embeddings**: Single 768-dim embedding model (nomic-embed-text-v1.5 via ONNX, CPU-only) served by a dedicated container on port 11435 with Ollama-compatible API. Runs on CPU to avoid CUDA context switching with Qwen on the GPU, reducing routing latency from ~3.5s to ~0.5s.
- **Three-layer memory (decoupled)**:
  1. **L1 HOT — SQLite `chat_memory`** in the orchestrator (raw, last ~30 min). Each row carries a `meta` JSON column with the routing decision (`route`, `confidence`, `payload`) and, for `HOME_CONTROL`, the HA outcome (`ha_entity_id`, `ha_action`, `ha_params`, `ha_status`, …). No more hourly/daily SQL summaries — those layers were removed.
  2. **L2 Short-term — Redis context bus** (`ctx:{user_id}:events`, TTL 30 min, capped 20, source-filtered) shared between orchestrator, `ha_memory_service`, and Hermes.
  3. **L3 Long-term — mem0-stack** (external, repo `croll83/mem0-stack`, accessed via `MEM0_BASE_URL`). Populated by the nightly `habit_extraction` job, which uses a **hybrid SQL + LLM** pipeline: deterministic SQL aggregation over `chat_memory.meta` for domotics habits (entity + action + time window + value), Qwen LLM only for preferences/topics on non-HOME_CONTROL messages. Records are tagged `agent_id=jarvis-habit-extractor` for filtering in the Hermes mem0 dashboard.
- **Redis context bus**: Shared between orchestrator, HA memory service, and Hermes. Each system writes events tagged with its source and reads only events from other sources, preventing self-duplication. Per-user event lists (`ctx:{user_id}:events`), capped at 20, TTL 30 minutes.
- **Parakeet STT on GX10**: nvidia/parakeet-tdt-0.6b-v3 on GX10 DGX Spark (128 GB unified memory). Swapped to Canary in jul 2026 (Parakeet's transcribe() exposes no language kwarg and its auto-LID misdetected short Italian audio as Russian; Canary offered native source_lang/target_lang forcing), then swapped back to Parakeet on 2026-09-12 (VRAM 8.42->4.14 GiB) with a Cyrillic guard in the orchestrator instead of native language forcing (discards residual misdetections, asks the user to repeat; optional Groq whisper rescue if GROQ_API_KEY is set). Extended 2026-09-25 with DeepFilterNet3 speech enhancement + Nemotron 3 Diarization upstream of transcription — see wiki `audiofront-service`.
- **CosyVoice3 on GX10**: Fun-CosyVoice3-0.5B with zero-shot voice cloning from reference audio. Server-side Italian text normalization (num2words) for correct number/unit pronunciation. Replaces Qwen3-TTS (deprecated). OpenAI-compatible API, ~0.6x RTF, ~3.6 GiB VRAM.
- **Nginx + Cloudflare Tunnel**: Public endpoints (Telegram webhook, health) served via Cloudflare Tunnel with no port forwarding. Internal services accessible only through Tailscale mesh.
- **Speaker biometrics**: Resemblyzer runs inside the orchestrator process -- no separate container needed.
- **Ontology Server**: Centralized knowledge graph with speaker-based ACL, serving as the single source of truth for entities and relations across the agent ecosystem.
