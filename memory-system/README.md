# JARVIS Memory System — Architecture & Implementation

> Complete documentation of the multi-layer memory system used by the JARVIS AI agent ecosystem.

This document describes the memory architecture **generically** (as an endpoint-based system) and then maps it to the **concrete Hermes Agent implementation**. The orchestrator treats the AI Agent as a black box — this documentation covers the agent-side memory internals.

---

## Table of Contents

1. [Overview](#overview)
2. [Memory Layers](#memory-layers)
3. [Layer 1: Session Memory (MEMORY.md + USER.md)](#layer-1-session-memory)
4. [Layer 2: Short-Term Context Bus (Redis)](#layer-2-short-term-context-bus)
5. [Layer 3: Long-Term Declarative Memory (Mem0)](#layer-3-long-term-declarative-memory)
6. [Layer 4: Long-Term Procedural Memory (ReasoningBank)](#layer-4-long-term-procedural-memory)
7. [Token Budget Per-Turn](#token-budget-per-turn)
8. [Write/Read Lifecycle](#writeread-lifecycle)
9. [Infrastructure Components](#infrastructure-components)
10. [Hermes Agent Mapping](#hermes-agent-mapping)
11. [Prompt Engineering](#prompt-engineering)

---

## Overview

The memory system is organized in **4 layers**, each serving a distinct purpose with different persistence, scope, and access patterns:

```
┌─────────────────────────────────────────────────────────────────┐
│                        AI AGENT                                 │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  LAYER 1: Session Memory (file-backed, per-session)       │  │
│  │  ├── MEMORY.md  — agent notes, environment facts          │  │
│  │  └── USER.md    — user profile, preferences               │  │
│  │  Scope: entire session │ Reset: session boundary           │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  LAYER 2: Short-Term Context Bus (Redis, cross-system)    │  │
│  │  ├── Agent writes: response summaries                     │  │
│  │  └── Agent reads: orchestrator/HA events                  │  │
│  │  Scope: 30 min TTL │ Bidirectional with orchestrator      │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  LAYER 3: Long-Term Declarative (Mem0)                    │  │
│  │  ├── Facts: "Ada is allergic to nuts"                     │  │
│  │  ├── Relations: marco → father_of → giorgio               │  │
│  │  └── Accounts: "Telegram is @croll83"                     │  │
│  │  Scope: permanent │ Write: session-end batch + on-demand  │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  LAYER 4: Long-Term Procedural (ReasoningBank)            │  │
│  │  ├── Strategies: "verify date on web search results"      │  │
│  │  ├── Patterns: "for HA control, resolve entity first"     │  │
│  │  └── Recovery: "if CDP fails, check browser process"      │  │
│  │  Scope: permanent │ Write: session-end induction          │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Design Principles

- **Layers don't compete** — each serves a different temporal scope and knowledge type
- **Minimal token overhead** — dynamic context is injected only when relevant (semantic search)
- **Separation of concerns** — declarative (what is true) vs. procedural (how to act)
- **Cross-system visibility** — Redis bus bridges agent and orchestrator without tight coupling
- **Graceful degradation** — circuit breakers on all external memory calls; agent works without memory

---

## Memory Layers

### Comparison Matrix

| Property | L1: Session | L2: Redis | L3: Mem0 | L4: ReasoningBank |
|---|---|---|---|---|
| **Type** | Session context | Cross-system events | Declarative facts | Procedural strategies |
| **Persistence** | Session lifetime | 30 min TTL | Permanent | Permanent |
| **Storage** | File (MEMORY.md) | Redis list | ChromaDB + Kuzu | ChromaDB (separate collection) |
| **Injection point** | System prompt (frozen) | User message (dynamic) | User message (dynamic) | User message (dynamic) |
| **Write trigger** | Agent tool call | Every turn (auto) | Session-end batch + on-demand | Session-end induction |
| **Read trigger** | Every turn (snapshot) | Every turn (prefetch) | Every turn (semantic search) | Every turn (semantic search) |
| **Content** | Curated notes | Event summaries (200 chars) | Extracted facts + graph | Generalized strategies |
| **Budget** | 2200 chars (memory) + 1375 chars (user) | Max 5 events | Max 10 results + 5 relations | Max 2 strategies |
| **Example** | "Orchestrator URL is ..." | "(orch) @kitchen light ON" | "Allergic to nuts" | "Always verify dates on web results" |

---

## Layer 1: Session Memory

### Generic Description

File-backed key-value memory with two stores:
- **MEMORY store**: Agent's personal notes (environment, config, lessons learned)
- **USER store**: User profile (preferences, communication style, relationships)

**API** (tool-based, called by the LLM):
```
memory(action="add", target="memory|user", content="...")
memory(action="replace", target="memory|user", old_text="...", content="...")
memory(action="remove", target="memory|user", old_text="...")
```

**Injection**: Content is snapshot-frozen at session start and injected into the **system prompt**. Mid-session writes update the file on disk but do NOT change the system prompt (preserves LLM prefix cache).

**Limits**:
- MEMORY store: 2,200 characters max
- USER store: 1,375 characters max
- Entry delimiter: `§` (section sign)

**Security**: Content is scanned for prompt injection patterns before acceptance (invisible unicode, role hijacking, exfiltration commands). See `_scan_memory_content()`.

### Hermes Mapping

- Files: `~/.hermes/profiles/{profile}/memories/MEMORY.md` and `USER.md`
- Provider: `BuiltinMemoryProvider` (always active, first provider)
- Tool: `memory` (agent-level intercepted, not in standard registry)
- Implementation: `tools/memory_tool.py` → `MemoryStore` class

---

## Layer 2: Short-Term Context Bus

### Generic Description

A Redis-backed event bus for cross-system short-term memory. Multiple systems write tagged events; each system reads only events from OTHER sources to avoid self-duplication.

**Data structure**: Per-user capped list
```
Key:    ctx:{user_id}:events
Value:  JSON list (max 20 items, LIFO)
TTL:    30 minutes (1800 seconds)
```

**Event schema**:
```json
{
  "ts": 1713800000.0,
  "source": "orchestrator|hermes|ha_memory",
  "text": "accendi luce cucina",
  "type": "command|response|state_change",
  "room": "kitchen",
  "entities": ["light.cucina"],
  "result": "luce accesa"
}
```

**Read filtering**: Each system excludes its own `source` tag. This means:
- Agent reads: orchestrator + HA events only
- Orchestrator reads: agent events only (for context) OR all events (for routing)

**Read limits**:
| Reader | Filter | Max events |
|---|---|---|
| Agent (prefetch) | `source != "hermes"` | 5 |
| Orchestrator (context) | `source != "orchestrator"` | 5 |
| Orchestrator (routing) | no filter | 9 (3 turns × 3) |

**Write content**: Summary truncated to 200 characters per event.

### Hermes Mapping

- Connection: `REDIS_URL` env var (e.g., `redis://<REDIS_HOST>:6379/0`)
- Write: `sync_turn()` → `_redis_push_event()` — after every agent response
- Read: `queue_prefetch()` → `_redis_get_external_context()` — before every turn
- Implementation: Inside `plugins/memory/mem0-selfhosted/__init__.py`
- Orchestrator side: `jarvis-orchestrator/context_bus.py` → `ContextBus` class

---

## Layer 3: Long-Term Declarative Memory

### Generic Description

Persistent fact store powered by the **Mem0** pipeline. Stores declarative knowledge (what is true about the world) with automatic fact extraction, deduplication, and knowledge graph building.

**Backend stack**:
- **Vector store**: ChromaDB — embedding-based semantic search
- **Graph store**: Kuzu — entity-relationship graph
- **Embedding model**: OpenAI-compatible API (e.g., `nomic-embed-text-v1.5`)
- **LLM**: OpenAI-compatible API for fact extraction + deduplication + graph extraction

**REST API** (Mem0 server):
```
POST   /add              — Full pipeline: extract facts → dedup → graph → store
POST   /search           — Semantic search (vector + graph)
GET    /memories/{uid}   — List all memories for a user
DELETE /memory/{id}      — Delete a specific memory
POST   /search_cross     — Search across all users
POST   /add_raw          — Bypass pipeline, direct vector insert
POST   /search_contextual — Search with metadata filters + optional LLM summary
```

**Pipeline on `/add`** (5 sequential LLM calls):
1. **Fact extraction** — LLM extracts discrete facts from conversation
2. **Deduplication** — LLM compares new facts vs existing, decides ADD/UPDATE/DELETE/NONE
3. **Graph extraction** — LLM identifies entity relationships
4. **Vector storage** — Facts embedded and stored in ChromaDB
5. **Result** — Returns list of extracted facts with events (ADD/UPDATE/etc.)

**Write triggers**:
- **Session-end batch**: Full conversation sent to `/add` (async, background thread)
- **On-demand**: User says "remember that..." → agent calls `mem0_save` tool → `/add`
- **Memory bridge**: When agent writes to MEMORY.md, content also sent to `/add`

**Read trigger**: Every turn via `queue_prefetch()` → `/search` with current user query

**Read limits**: Max 10 results + 5 graph relations per prefetch

**Custom prompts** (Italian):
- `CUSTOM_FACT_EXTRACTION_PROMPT` — Instructs LLM to extract personal facts in Italian, third person
- `CUSTOM_UPDATE_MEMORY_PROMPT` — Instructs LLM on ADD/UPDATE/DELETE/NONE decisions
- `CUSTOM_GRAPH_PROMPT` — Entity naming in original language, Italian relations

### What belongs in declarative memory

- Personal facts: name, birthday, relationships, allergies
- Preferences: food, communication style, timezone
- Accounts: email, phone, social handles
- Professional: role, company, projects
- Family/relationships: who is who, where they live

### What does NOT belong in declarative memory

- Infrastructure config (IPs, ports, endpoints) → belongs in Session Memory (MEMORY.md)
- Mutable state (PnL, trade count, win rate) → changes too frequently for permanent store
- Procedural knowledge (how to do things) → belongs in ReasoningBank (Layer 4)
- Task progress or session outcomes → ephemeral, not worth persisting

### Hermes Mapping

- Server: `MEM0_BASE_URL` env var (e.g., `http://<MEM0_HOST>:8200`)
- User ID: derived from agent profile name (e.g., `marco`, `ada`, `shared`)
- Tools exposed: `mem0_search`, `mem0_search_cross`, `mem0_save`
- Provider: `Mem0SelfHostedProvider` (external provider, registered via plugin)
- Implementation: `plugins/memory/mem0-selfhosted/__init__.py`
- Server: `jarvis/mem0-server/server.py` (FastAPI)

---

## Layer 4: Long-Term Procedural Memory (ReasoningBank)

### Generic Description

Persistent store for **generalized reasoning strategies** distilled from agent task trajectories. Based on the [ReasoningBank](https://github.com/google-research/reasoning-bank) paper (ICLR 2026).

Unlike declarative memory (facts about the world), procedural memory captures **how to act** — strategies that worked, patterns from recovered failures, and lessons learned from multi-step task execution.

**Backend**: ChromaDB collection `rb_strategies` (same ChromaDB instance as Mem0, separate collection).

**Strategy schema**:
```json
{
  "id": "rb_<uuid>",
  "text": "When searching the web, always verify the date of results before presenting them as current",
  "metadata": {
    "user_id": "marco",
    "type": "strategy",
    "context": "web_search tool usage",
    "source_signal": "success|failure_recovery",
    "induced_from_session": "session_id",
    "created_at": "2026-04-22T10:30:00Z",
    "use_count": 0,
    "last_used": null
  }
}
```

**Induction process** (called at session-end):
1. Full session transcript (user + assistant messages) is sent to the LLM
2. LLM analyzes the trajectory for generalizable strategies
3. Strategies are embedded and stored in ChromaDB `rb_strategies` collection
4. Max 3 strategies per session (quality over quantity)

**Induction prompt** — The LLM is asked to:
- Extract WHAT to do (not the specific case, but the general pattern)
- Identify WHEN to apply it (trigger context)
- Classify the source signal: learned from success or from failure recovery
- Output structured JSON

**Deduplication**: Before storing, new strategies are compared against existing ones via semantic similarity. If similarity > 0.85, the existing strategy is updated (use_count incremented) rather than duplicated.

**Read trigger**: Every turn via `queue_prefetch()` — semantic search against current query, max 2 results.

**Read filtering**: Only strategies with similarity score above threshold (0.3) are injected. If no strategy is relevant, 0 tokens are added.

**Write triggers**:
- **Session-end**: `on_session_end()` — full transcript → LLM induction → ChromaDB store
- **Only for substantial sessions**: Sessions with < 2 user turns are skipped (not enough signal)

### What belongs in procedural memory

- Task execution strategies: "For HA control, always resolve entity name first"
- Tool usage patterns: "web_search results should be date-verified"
- Error recovery patterns: "If CDP connection fails, check if browser process is running"
- Workflow optimizations: "For multi-file edits, read all files first before making changes"

### What does NOT belong in procedural memory

- Facts about people or things → Layer 3 (Mem0)
- Session-specific context → Layer 1 (MEMORY.md)
- Recent events → Layer 2 (Redis)

### Hermes Mapping

- Integrated into: `plugins/memory/mem0-selfhosted/__init__.py` (same provider)
- ChromaDB collection: `rb_strategies` (same ChromaDB instance, port 8000)
- LLM for induction: same LLM endpoint as Mem0 (`LLM_URL` / `LLM_MODEL`)
- Embedding: same embedding endpoint as Mem0 (`EMBED_URL` / `EMBED_MODEL`)
- No additional tools exposed (retrieval is automatic via prefetch)
- Optional tool: `rb_feedback` — agent can signal if a retrieved strategy was useful

---

## Token Budget Per-Turn

### System Prompt (frozen, cacheable)

| Component | Source | Tokens (approx) |
|---|---|---|
| SOUL.md (agent identity) | Config file | ~550 |
| Tool guidance + skill list | Generated | ~300 |
| MEMORY.md snapshot | Layer 1 | ~400 (2200 chars ≈ 550 tok max) |
| USER.md snapshot | Layer 1 | ~250 (1375 chars ≈ 340 tok max) |
| Mem0 tool instructions | Layer 3 (static) | ~80 |
| **Subtotal** | | **~1,580** |

### Dynamic Context (per-turn, in user message)

| Component | Source | Tokens (approx) | Condition |
|---|---|---|---|
| Mem0 prefetch (facts + graph) | Layer 3 | 100–200 | Always (if results found) |
| Redis events | Layer 2 | 50–100 | Only if events in last 30 min |
| ReasoningBank strategies | Layer 4 | 80–120 | Only if relevant strategy found |
| **Subtotal** | | **150–420** | |

### Total Memory Overhead

| Scenario | System | Dynamic | Total |
|---|---|---|---|
| First turn of day (cold) | ~1,580 | ~50 (only Mem0) | ~1,630 |
| Mid-session (warm) | ~1,580 | ~350 | ~1,930 |
| After gap > 30min | ~1,580 | ~200 (no Redis) | ~1,780 |

**ReasoningBank adds ~80-120 tokens per-turn** only when a relevant strategy exists (0 otherwise). Total overhead is < 10% of the memory budget.

---

## Write/Read Lifecycle

### Per-Turn Flow

```
USER MESSAGE ARRIVES
        │
        ├──→ queue_prefetch(query)          [async, background thread]
        │     ├── Mem0 /search              [Layer 3: semantic search]
        │     ├── Redis lrange + filter     [Layer 2: recent events]
        │     └── ChromaDB rb_strategies    [Layer 4: strategy search]
        │
        ├──→ SYSTEM PROMPT assembled
        │     ├── SOUL.md                   [static]
        │     ├── MEMORY.md snapshot        [Layer 1: frozen at session start]
        │     ├── USER.md snapshot          [Layer 1: frozen at session start]
        │     └── Mem0 instructions         [Layer 3: static tool guidance]
        │
        ├──→ prefetch() results collected   [join background thread]
        │     └── Injected as <memory-context> in user message
        │
        ├──→ LLM API CALL
        │     └── (tool calls, reasoning, response)
        │
        └──→ sync_turn(user_msg, assistant_response)
              └── Redis lpush (hermes summary)  [Layer 2: write]

SESSION ENDS (exit, /reset, timeout, scheduled)
        │
        ├──→ on_session_end(messages)
        │     ├── Mem0 /add (full transcript)    [Layer 3: fact extraction]
        │     └── RB induce (full transcript)    [Layer 4: strategy induction]
        │
        └──→ MEMORY.md / USER.md persisted on disk  [Layer 1: already saved per-write]
```

### Write Frequency Summary

| Store | Trigger | Frequency | Blocking? |
|---|---|---|---|
| MEMORY.md | Agent tool call `memory(add)` | Proactive (agent decides) | No (file write) |
| Redis | `sync_turn()` | Every turn | No (fire-and-forget) |
| Mem0 | `on_session_end()` | Once per session | No (background thread) |
| Mem0 | `mem0_save` tool | On user request | No (background thread) |
| ReasoningBank | `on_session_end()` | Once per session | No (background thread) |

---

## Infrastructure Components

| Component | Port | Technology | Role | Required? |
|---|---|---|---|---|
| **Redis** | 6379 | redis:7-alpine | Context bus (Layer 2) | Yes |
| **ChromaDB** | 8000 | chromadb/chroma | Vector store for Mem0 + RB | Yes |
| **Mem0 Server** | 8200 | FastAPI + mem0ai | Fact pipeline (Layer 3) | Yes |
| **Kuzu** | embedded | kuzu (in mem0) | Graph store (Layer 3) | Optional |
| **Embedding Server** | 11435 | fastembed (ONNX) | nomic-embed-text-v1.5 | Yes |
| **LLM Server** | 30000 | llama.cpp / vLLM | Fact extraction + strategy induction | Yes |

### Network Topology (generic)

```
┌──────────────────────────────────────────────────────┐
│  AGENT VM                                            │
│  ├── AI Agent process                                │
│  │   ├── mem0-selfhosted plugin                      │
│  │   │   ├── reads/writes Redis  ──→ <REDIS_HOST>    │
│  │   │   ├── reads/writes Mem0   ──→ <MEM0_HOST>     │
│  │   │   └── reads/writes ChromaDB (via Mem0 + direct)│
│  │   └── MEMORY.md / USER.md (local filesystem)      │
│  └── Agent config (SOUL.md, tools, skills)           │
│                                                      │
├──────────────────────────────────────────────────────┤
│  MEMORY VM                                           │
│  ├── Redis (:6379)                                   │
│  ├── ChromaDB (:8000)                                │
│  ├── Mem0 Server (:8200)                             │
│  │   ├── Kuzu (embedded graph)                       │
│  │   └── connects to Embedding + LLM servers         │
│  └── Embedding Server (:11435)                       │
│                                                      │
├──────────────────────────────────────────────────────┤
│  LLM SERVER                                          │
│  └── llama.cpp / vLLM (:30000)                       │
│      └── Model: <LLM_MODEL_NAME>                     │
└──────────────────────────────────────────────────────┘
```

---

## Hermes Agent Mapping

### Concrete Deployment

| Generic | Hermes Concrete |
|---|---|
| Agent VM | `<AGENT_HOST>` (bare-metal, Tailscale) |
| Memory VM | `<MEMORY_HOST>` (Docker Compose stack) |
| LLM Server | `<LLM_HOST>:30000` (llama.cpp, Qwen 3 35B MoE) |
| Redis | `<MEMORY_HOST>:6379` |
| ChromaDB | `<MEMORY_HOST>:8000` (via Mem0 server, network_mode: host) |
| Mem0 Server | `<MEMORY_HOST>:8200` (network_mode: host) |
| Embedding | `<MEMORY_HOST>:11435` (fastembed, CPU, ONNX) |
| Agent profiles | `~/.hermes/profiles/hermes-{marco,ada,shared,dark}/` |

### Provider Architecture

```
MemoryManager (run_agent.py)
├── BuiltinMemoryProvider (always active)
│   ├── system_prompt_block() → MEMORY.md + USER.md snapshot
│   ├── prefetch() → no-op (injected via system prompt)
│   └── tools: memory(add/replace/remove)
│
└── Mem0SelfHostedProvider (external, plugin)
    ├── system_prompt_block() → Mem0 tool instructions (static)
    ├── queue_prefetch() → Mem0 search + Redis read + RB search [async]
    ├── prefetch() → returns combined context block
    ├── sync_turn() → Redis push (hermes summary)
    ├── on_session_end() → Mem0 /add + RB induction
    ├── on_memory_write() → bridges MEMORY.md writes to Mem0
    └── tools: mem0_search, mem0_search_cross, mem0_save
```

### Environment Variables

```bash
# Mem0 Server
MEM0_BASE_URL=http://<MEM0_HOST>:8200
MEM0_USER_ID=hermes-user  # overridden by profile name

# Redis Context Bus
REDIS_URL=redis://<REDIS_HOST>:6379/0

# Mem0 Server internals (docker-compose env)
CHROMA_HOST=<CHROMA_HOST>
CHROMA_PORT=8000
EMBED_URL=http://<EMBED_HOST>:11435/v1
EMBED_MODEL=nomic-ai/nomic-embed-text-v1.5
EMBED_DIMS=768
LLM_URL=http://<LLM_HOST>:30000/v1
LLM_MODEL=<LLM_MODEL_NAME>
GRAPH_LLM_URL=http://<LLM_HOST>:30000/v1
GRAPH_LLM_MODEL=<LLM_MODEL_NAME>
KUZU_PATH=/data/kuzu/db
```

---

## Prompt Engineering

### Fact Extraction (Layer 3 — Mem0)

The fact extraction prompt instructs the LLM to:
- Extract personal facts in Italian, third person
- Categorize: preferences, personal details, plans, professional, health, misc
- Output `{"facts": ["...", "..."]}` JSON
- Skip greetings and trivial exchanges
- Include current date for temporal context

**Exclusions** (to avoid overlap with ReasoningBank):
- Infrastructure configuration (IPs, ports, endpoints)
- Mutable metrics (PnL, trade counts, win rates)
- Procedural knowledge (how to do things, strategies)
- Session-specific task progress

### Strategy Induction (Layer 4 — ReasoningBank)

The induction prompt instructs the LLM to:
- Analyze the full session transcript
- Extract generalizable strategies (not case-specific facts)
- For each strategy: WHAT to do, WHEN to apply, source signal
- Output `{"strategies": [{"text": "...", "context": "...", "signal": "success|failure_recovery"}]}`
- Max 3 strategies per session
- Must be applicable beyond the specific session

See `REASONING_BANK_INDUCTION_PROMPT` in the plugin source code.

---

## References

- [ReasoningBank paper](https://arxiv.org/abs/2509.25140) — ICLR 2026
- [Mem0 documentation](https://docs.mem0.ai/)
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) — Agent framework
- [Mem0 + Hermes Setup Guide](../mem0-hermes-setup-guide.md) — Deployment guide (in parent directory)
