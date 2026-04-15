# Unified Memory Architecture — Deployment Instructions

## Overview

This migration introduces:
1. **Shared ChromaDB server** (new container, port 8000) replacing 3 separate embedded instances
2. **Previous intent tracking** for Qwen multi-turn routing continuity
3. **Router window expansion** from 5 min to 15 min
4. **Ontology-bridge plugin v2.0** with ChromaDB semantic search + orchestrator recent messages

## Files Modified

### Infrastructure
- `/opt/jarvis/docker-compose.yml` — Added `chromadb` service + `chroma_data` volume
- `/opt/jarvis/cloud/docker-compose.cloud.yml` — Added `chromadb` service + `chroma_data` volume
- `/opt/jarvis/jarvis.sh` — Added chromadb to Fase 3 startup with `wait_for`

### Orchestrator (`/opt/jarvis/jarvis-orchestrator/`)
- `config.py` — Added `CHROMA_HOST`, `CHROMA_PORT`; changed `ROUTER_MEMORY_WINDOW_SECONDS` 300→900
- `vector_store.py` — `PersistentClient` → `HttpClient` (with fallback)
- `database.py` — Added `save_last_intent()`, `get_last_intent()` (in-memory)
- `main.py` — Inject `previous_intent` into router context; save intent after routing; added `/api/recent_turns` endpoint
- `ai_engines.py` — `_build_routing_prompt()` includes `[INTENT PRECEDENTE]` section

### Router System Prompt
- `/opt/jarvis/config/router_system_prompt.txt` — Added CONTINUITÀ CONVERSAZIONE rules

### HA Memory Service (`/opt/jarvis/ha_memory_service/`)
- `main.py` — `PersistentClient` → `HttpClient` (with fallback); added `CHROMA_HOST`, `CHROMA_PORT` env vars

### AI Agent Plugin (`extensions/ontology-bridge/`)
- `index.ts` — Rewritten v2.0: keyword match + ChromaDB semantic search + orchestrator recent messages
- Plugin config — Updated config schema (added `chromaUrl`, `orchestratorUrl`, `embeddingUrl`, `recentMessagesCount`)
- `package.json` — Version bump to 2.0.0

## Deployment Steps

### Step 1: Pull ChromaDB image (can do while stack is running)
```bash
docker pull chromadb/chroma:0.6.3
```

### Step 2: Stop the stack
```bash
cd /opt/jarvis
./jarvis.sh stop
```

### Step 3: Migrate existing ChromaDB data
```bash
# Start only the ChromaDB container first
docker compose up -d chromadb
sleep 5

# Dry run — see what will be migrated
python3 ~/new_memory/migrate_chromadb.py --dry-run

# Actual migration
python3 ~/new_memory/migrate_chromadb.py

# If HA memory service has separate ChromaDB data:
# python3 ~/new_memory/migrate_chromadb.py --ha-path /path/to/ha/chroma
```

### Step 4: Start the full stack
```bash
./jarvis.sh start
```

### Step 5: Verify
```bash
# Check ChromaDB is healthy
curl -s http://localhost:8000/api/v1/heartbeat

# Check orchestrator connected to ChromaDB server (not PersistentClient)
docker logs jarvis_core 2>&1 | grep -i "chroma\|vector store"

# Check recent_turns API endpoint
curl -s http://localhost:5000/api/recent_turns?max_turns=1

# Check routing includes previous_intent
# (send two requests in sequence, check logs for [INTENT PRECEDENTE])
docker logs jarvis_core 2>&1 | grep "previous_intent\|INTENT PRECEDENTE"
```

### Step 6: Update AI Agent plugin config
Add to the AI Agent config under `plugins.entries.ontology-bridge.config`:
```json
{
  "chromaUrl": "http://127.0.0.1:8000",
  "orchestratorUrl": "http://127.0.0.1:5000",
  "embeddingUrl": "http://127.0.0.1:11435",
  "recentMessagesCount": 6
}
```
Then restart OpenClaw gateway.

### Step 7: Kill stale cron
The old ONTOLOGY_REMOTE.md sync cron is no longer needed:
```bash
# Find and delete the cron
# Cron ID: cabf0de9-fd1a-4dc9-95c7-ba34c181d3d3
```

## Rollback

If issues arise, the orchestrator and HA memory service have **automatic fallback**:
if the ChromaDB server is unreachable, they fall back to `PersistentClient` (local embedded).

To fully rollback:
1. Stop the stack: `./jarvis.sh stop`
2. Revert the git changes: `cd /opt/jarvis && git checkout -- .`
3. Start the stack: `./jarvis.sh start`

## Environment Variables (new)

| Variable | Default | Used by |
|----------|---------|---------|
| `CHROMA_HOST` | `localhost` | orchestrator |
| `CHROMA_PORT` | `8000` | orchestrator |
| `CHROMA_HOST` | `localhost` | ha_memory_service |
| `CHROMA_PORT` | `8000` | ha_memory_service |

These are set automatically since both services use `network_mode: host` and the
ChromaDB container exposes port 8000 on localhost.

## Architecture Diagram

```
                    ┌─────────────────┐
                    │  ChromaDB :8000 │
                    │  (shared server) │
                    └────┬───┬───┬────┘
                         │   │   │
         ┌───────────────┘   │   └───────────────┐
         │                   │                   │
    ┌────▼────┐        ┌─────▼─────┐      ┌──────▼──────┐
    │Orchestr.│        │ HA Memory │      │  OpenClaw    │
    │  :5000  │        │  Service  │      │ontology-     │
    │         │◄───────│           │      │bridge plugin │
    │HttpClient│       │HttpClient │      │REST API query│
    └────┬────┘        └───────────┘      └──────┬──────┘
         │                                       │
         │  /api/recent_turns ◄──────────────────┘
         │
    ┌────▼────┐     ┌──────────┐
    │  Qwen   │     │ Ontology │
    │ Router  │     │  :8100   │
    │+prev_int│     │(entities)│
    └─────────┘     └──────────┘
```
