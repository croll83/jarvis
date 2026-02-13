# JARVIS HA Memory

Location memory service for JARVIS. Ingests Home Assistant events via WebSocket, generates hourly and daily summaries using LLM, and provides semantic search via ChromaDB.

## How it works

1. **Event Ingestion** — Connects to Home Assistant via WebSocket and captures all `state_changed` events
2. **Filtering** — Ignores noisy entities (updates, battery, signal strength)
3. **Raw Storage** — Stores events in SQLite (30 min rolling window)
4. **Vector Indexing** — Indexes events in ChromaDB for semantic search
5. **Hourly Summary** — Every hour (at minute 5), generates a natural language summary of house activity
6. **Daily Summary** — At 03:00, generates a daily report with patterns, anomalies, and long-term facts
7. **REST API** — Exposes endpoints for the JARVIS orchestrator to query memory

## Configuration

### AI Backend

Choose between two modes:

- **`local`** — Uses Ollama for both summarization and embeddings. Requires Ollama reachable via network (e.g., via Tailscale).
- **`api`** — Uses OpenRouter for summarization and Gemini for embeddings. No Ollama needed — runs entirely on cloud APIs.

### Location ID

Must match the location ID configured in your JARVIS orchestrator database. This is how the orchestrator knows which memory service to query.

### Local mode setup

1. Set AI Backend to `local`
2. Set Ollama URL to your Ollama server (e.g., `http://100.x.x.x:11434` via Tailscale)
3. Ensure `nomic-embed-text` and `qwen2.5:3b` models are pulled in Ollama

### API mode setup (cloud)

1. Set AI Backend to `api`
2. Enter your OpenRouter API key (get one at https://openrouter.ai/keys)
3. Enter your Gemini API key (get one at https://aistudio.google.com/app/apikey)

## API Endpoints

The add-on exposes port **8100** with the following endpoints:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/memory` | Stratified memory (hot/warm/cold/longterm) |
| POST | `/memory/search` | Semantic search over events |

## Memory layers

| Layer | Window | Source |
|-------|--------|--------|
| **Hot** | Last 30 min | Raw events from SQLite |
| **Warm** | Last 24 hrs | Hourly LLM summaries |
| **Cold** | Last 7 days | Daily LLM summaries with patterns |
| **Long-term** | Permanent | Extracted facts and habits |

## Resource usage

- **RAM**: ~200-300 MB (ChromaDB is the main consumer)
- **Disk**: ~500 MB for ChromaDB after a week of events
- **CPU**: Minimal — mostly idle, spikes during hourly summary
- **Network**: WebSocket connection to HA + periodic LLM calls

No GPU required — all LLM inference is done remotely (Ollama or cloud API).

## Switching between modes

You can switch from `api` to `local` (or vice versa) at any time by changing the AI Backend option. SQLite data (summaries) persists across switches. ChromaDB vectors from the old embedding provider will be naturally purged after 7 days.

## Troubleshooting

Check the add-on logs for connection status. Common issues:

- **"HA WebSocket auth failed"** — The add-on cannot authenticate with HA. Try restarting the add-on.
- **"Ollama embedding error"** — Ollama is unreachable. Check Tailscale connectivity and that the model is pulled.
- **"OpenRouter error"** — API key issue or rate limit. Check your OpenRouter dashboard.
- **"Gemini embedding error"** — Gemini API key issue. Verify at https://aistudio.google.com.
