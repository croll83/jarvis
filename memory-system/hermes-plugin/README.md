# Hermes Agent — `mem0-selfhosted` plugin

Custom Hermes Agent memory plugin that wires together the three persistent
layers of the JARVIS memory system:

- **Layer 2** — Redis context bus (short-term, cross-system)
- **Layer 3** — Mem0 (long-term declarative: facts, relations, graph)
- **Layer 4** — ReasoningBank (long-term procedural: induced strategies)

See `../README.md` for the full architecture.

## Runtime location

This source lives at:
```
memory-system/hermes-plugin/mem0-selfhosted/__init__.py
```

It must be deployed to the Hermes Agent host as:
```
/opt/hermes-agent/plugins/memory/mem0-selfhosted/__init__.py
```

The upstream Hermes Agent repository
(`https://github.com/NousResearch/hermes-agent`) ships only the base
plugin scaffolding; this file is our local extension and is **not**
tracked in the upstream repo (it appears as `??` in `git status`).

## Deploy

```bash
# from this repo (croll83/jarvis) on the dev workstation
scp memory-system/hermes-plugin/mem0-selfhosted/__init__.py \
    jarvis@<hermes-host>:/opt/hermes-agent/plugins/memory/mem0-selfhosted/__init__.py

# on the Hermes host: restart the gateway to pick up changes
ssh jarvis@<hermes-host> 'systemctl restart hermes-agent'   # or: hermes_cli.main gateway run --replace
```

## Environment variables

### Mem0 connection
| Var | Default | Purpose |
|---|---|---|
| `MEM0_BASE_URL` | `http://localhost:8200` | Mem0 server URL |
| `MEM0_USER_ID` | `hermes-user` | Memory namespace |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis context bus |

### ReasoningBank — local LLM (fallback)
| Var | Default | Purpose |
|---|---|---|
| `RB_LLM_URL` | `http://{chroma_host}:30000/v1` | OpenAI-compat endpoint |
| `RB_LLM_MODEL` | `dark-opus` | Model name on the endpoint |
| `RB_EMBED_URL` | `http://{chroma_host}:11435/v1` | fastembed endpoint |
| `RB_EMBED_MODEL` | `nomic-ai/nomic-embed-text-v1.5` | Embedding model |

### ReasoningBank — Anthropic-compatible proxy (preferred, default ON)
| Var | Default | Purpose |
|---|---|---|
| `RB_USE_ANTHROPIC` | `1` | Toggle (`0` → fallback to local LLM) |
| `RB_ANTHROPIC_BASE_URL` | `http://100.116.99.9:18801` | In-house Anthropic-compat router |
| `RB_ANTHROPIC_API_KEY` | `dummy` | Set to a real key if the router enforces auth |
| `RB_ANTHROPIC_MODEL` | `claude-sonnet-4-5` | Induction model |

The Anthropic path uses plain text output (no tool_use) — the induction
prompt asks for a strict JSON response, parsed with the same
fence-stripping logic as the OpenAI path.

## Rationale for the Anthropic path

- Strategy induction is a high-level abstraction task. Local quantized
  models produce banal/redundant strategies (e.g. *"Rispondere con 'Hey'
  per verificare la connessione"*). Sonnet 4.5 yields properly
  generalizable rules with precise context tags.
- Volume is tiny (1-2 calls/day at session end) → cost ≈ $1-2/month.
- Embeddings stay local (fastembed/nomic) — no LLM calls, no concern.
- Eliminates one more source of traffic to the local llama.cpp backend
  (which has known GPU-memory leaks on parse-failure paths).

## Dependencies

The Hermes Agent venv must contain the `anthropic` SDK (≥0.89). The
plugin imports it lazily inside `_rb_init` and falls back to the local
LLM if the import fails.
