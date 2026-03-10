# ontology-bridge v2.0

OpenClaw plugin that provides unified memory context to agents by combining three sources: ontology knowledge graph (keyword match), ChromaDB shared server (semantic search), and orchestrator recent messages.

## What it does

### 1. Context Injection (`before_prompt_build`)

On every agent prompt, the plugin gathers context from three sources:

1. **Ontology keyword match** — Maintains an in-memory cache of all ontology entities (refreshed every 5 min). Matches entity names in the prompt and fetches full details (properties + relations) via REST API.
2. **ChromaDB semantic search** — Queries the shared ChromaDB server (:8000) for semantically relevant past messages and facts using nomic-embed-text embeddings via fastembed (:11435).
3. **Orchestrator recent messages** — Fetches the last N conversation turns from the orchestrator's `/api/recent_turns` endpoint for immediate conversational context.

Injects a compact `[Ontology Context]` block into the prompt — only when relevant.

**Result:** Zero tokens added on unrelated prompts. Rich multi-source context when entities or topics are mentioned. Always fresh, never stale.

### 2. Fact Extraction (`agent_end`)

After each agent session, the plugin:

1. Scans the conversation for **storable patterns**:
   - "Ricordati che..." / "Remember that..."
   - "Ho deciso..." / "We decided..."
   - "Preferisco..." / "From now on..."
   - Task status changes
2. **Writes extracted facts** to the ontology as `Note` or `Preference` entities (tagged `auto-extracted`)
3. **Skips extraction** if the agent already made 3+ ontology API calls (avoids duplicates)

### 3. Tool Tracking (`after_tool_call`)

Monitors Bash calls to the ontology URL to track whether agents are already using the ontology directly.

## Configuration

In `openclaw.json` under `plugins.entries.ontology-bridge`:

```json
{
  "enabled": true,
  "config": {
    "ontologyUrl": "http://127.0.0.1:8100",
    "chromaUrl": "http://127.0.0.1:8000",
    "orchestratorUrl": "http://127.0.0.1:5000",
    "embeddingUrl": "http://127.0.0.1:11435",
    "defaultSpeakerId": "jarvis-agent",
    "cacheRefreshIntervalMs": 300000,
    "maxContextTokens": 600,
    "recentMessagesCount": 6,
    "extractionEnabled": true,
    "skipAgents": ["personal-relay"],
    "logPath": "/home/jarvis/.openclaw/workspace/state/ontology-bridge.jsonl"
  }
}
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ontologyUrl` | `http://127.0.0.1:8100` | Ontology server URL |
| `chromaUrl` | `http://127.0.0.1:8000` | ChromaDB shared server URL |
| `orchestratorUrl` | `http://127.0.0.1:5000` | Orchestrator API URL (for `/api/recent_turns`) |
| `embeddingUrl` | `http://127.0.0.1:11435` | Embedding server URL (fastembed, Ollama-compatible) |
| `defaultSpeakerId` | `jarvis-agent` | Speaker ID for ontology API calls |
| `cacheRefreshIntervalMs` | `300000` (5 min) | Entity cache refresh interval |
| `maxContextTokens` | `600` | Max tokens for injected context block |
| `recentMessagesCount` | `6` | Number of recent orchestrator messages to fetch |
| `extractionEnabled` | `true` | Enable automatic fact extraction |
| `skipAgents` | `["personal-relay"]` | Agent IDs to skip entirely |
| `logPath` | `.../ontology-bridge.jsonl` | Bridge activity log |

## Architecture

```
┌─────────────────────────────────────────────┐
│          ontology-bridge v2.0               │
│                                             │
│  before_prompt_build:                       │
│  ┌──────────────┐ ┌──────────────┐          │
│  │ Ontology API │ │ ChromaDB     │          │
│  │ :8100        │ │ :8000        │          │
│  │ keyword match│ │ semantic     │          │
│  └──────┬───────┘ └──────┬───────┘          │
│         │                │                  │
│         │  ┌─────────────┴──────┐           │
│         │  │ fastembed :11435   │           │
│         │  │ nomic-embed-text   │           │
│         │  └────────────────────┘           │
│         │                │                  │
│  ┌──────┴────────────────┴──────┐           │
│  │  Merge + deduplicate         │           │
│  │  → [Ontology Context] block  │           │
│  └──────────────┬───────────────┘           │
│                 │                           │
│  ┌──────────────┴───────────────┐           │
│  │ Orchestrator /api/recent_turns│          │
│  │ :5000 (last N messages)       │          │
│  └───────────────────────────────┘          │
└─────────────────────────────────────────────┘
```

## Changes from v1.0

| Aspect | v1.0 | v2.0 |
|--------|------|------|
| Context sources | Ontology keyword match only | Ontology + ChromaDB semantic + orchestrator recent messages |
| ChromaDB | Not used | Shared server :8000 (semantic search) |
| Embeddings | Not used | fastembed :11435 (nomic-embed-text-v1.5) |
| Orchestrator integration | None | `/api/recent_turns` for conversation context |
| Config params | 7 | 11 (added `chromaUrl`, `orchestratorUrl`, `embeddingUrl`, `recentMessagesCount`) |
| Token cost | 0-400 tokens | 0-600 tokens (richer context from 3 sources) |

## Dependencies

- Ontology server running at configured URL (:8100)
- ChromaDB shared server running at configured URL (:8000)
- fastembed embedding server at configured URL (:11435)
- Orchestrator with `/api/recent_turns` endpoint (:5000)
- `ONTOLOGY_API_TOKEN` environment variable set
- OpenClaw 2026.3.7+ (plugin hooks API)

## Files

```
extensions/ontology-bridge/
├── index.ts                 # Plugin source
├── openclaw.plugin.json     # Plugin manifest (v2.0)
├── package.json             # Package metadata
└── README.md                # This file
```
