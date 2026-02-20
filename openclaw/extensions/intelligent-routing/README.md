# Intelligent Routing Plugin for OpenClaw

Automatic model routing based on task complexity. Classifies incoming messages and routes MEDIUM+ tasks to stronger models, while SIMPLE tasks stay on the default (Haiku).

## What It Does

Every incoming message is intercepted by the `before_model_resolve` hook and classified into one of five complexity tiers:

| Tier | Model | Thinking | Use Case |
|------|-------|----------|----------|
| **SIMPLE** | *(default — Haiku)* | — | Chat, greetings, status checks, short replies |
| **MEDIUM** | Sonnet | — | Research, writing, moderate debugging |
| **COMPLEX** | Sonnet | `on` | Multi-file features, architecture, complex debugging |
| **REASONING** | Opus | `high` | Formal proofs, deep reasoning, strategic analysis |
| **CRITICAL** | Opus | `high` | Security audits, production releases, financial ops |

**Key design:** SIMPLE tasks produce **no model override** — the gateway default (Haiku) handles them as-is. This means zero overhead for the most common case.

## Classification Pipeline

```
Message → Fast-Path (keyword) → Full Classifier (intelligent-router-hook.js) → Decision
           ~0ms                   ~1-8s (Python subprocess)
```

### Stage 1: Fast-Path (keyword matching)
Instant classification for obvious SIMPLE tasks:
- Short messages (≤15 chars)
- Greetings: "ciao", "hello", "hi", "buongiorno", etc.
- Confirmations: "ok", "sì", "no", "grazie", "done", etc.
- Emoji-only messages
- Common patterns (lol, haha, etc.)

### Stage 2: Full Classifier
For anything that doesn't match fast-path:
- Calls `intelligent-router-hook.js` via `execSync`
- The hook invokes a Python classifier (`router.py`)
- Returns: `{ tier, model, fallbacks, thinking, confidence }`
- On timeout/error: falls back to SIMPLE (safe default)

## File Structure

```
intelligent-routing/
├── openclaw.plugin.json   # Plugin manifest (hooks, config schema)
├── index.ts               # Entry point — hook registration & logic
├── package.json           # Package metadata
└── README.md              # This file
```

**External dependencies** (not included, must exist on the system):
- `/home/jarvis/.openclaw/workspace/skills/intelligent-router/intelligent-router-hook.js`
- `/home/jarvis/.openclaw/workspace/skills/intelligent-router/config.json`
- `/home/jarvis/.openclaw/workspace/skills/intelligent-router/scripts/router.py`

## Installation

### 1. Copy plugin files

```bash
mkdir -p ~/.openclaw/extensions/intelligent-routing
# Unzip or copy all files into the directory
cp openclaw.plugin.json index.ts package.json README.md \
   ~/.openclaw/extensions/intelligent-routing/
```

### 2. Enable the plugin

```bash
openclaw plugins enable intelligent-routing
```

### 3. (Optional) Configure

```bash
openclaw plugins config intelligent-routing \
  --set routerScriptPath="/path/to/intelligent-router-hook.js" \
  --set classifierTimeoutMs=10000
```

### 4. Restart the gateway

```bash
openclaw gateway restart
```

## Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `routerScriptPath` | string | `.../skills/intelligent-router/intelligent-router-hook.js` | Path to the classifier script |
| `routingLogPath` | string | `.../state/routing-log.jsonl` | Path to the JSONL routing log |
| `classifierTimeoutMs` | number | `8000` | Subprocess timeout in ms |
| `enableFastPath` | boolean | `true` | Enable keyword-based SIMPLE detection |
| `dryRun` | boolean | `false` | Log decisions but don't override models |

## Logging

Every routing decision is appended to `state/routing-log.jsonl` as a single JSON line:

```json
{
  "timestamp": "2026-02-19T18:30:00.000Z",
  "source": "plugin:intelligent-routing",
  "agentId": "main",
  "taskDescription": "analizza questo documento e fammi un riassunto...",
  "tier": "MEDIUM",
  "modelSelected": "anthropic/claude-sonnet-4-6",
  "fallbacks": ["google/gemini-3-flash-preview", "xai/grok-4-1-fast-reasoning"],
  "confidence": 0.87,
  "executionTimeMs": 1523,
  "success": true,
  "classificationMethod": "full-classifier",
  "notes": ""
}
```

### Log Fields

| Field | Description |
|-------|-------------|
| `source` | Always `"plugin:intelligent-routing"` |
| `agentId` | Agent that received the message (`main`, `family:ada`, etc.) |
| `classificationMethod` | `fast-path`, `full-classifier`, or `fallback-on-error` |
| `modelSelected` | `"(default)"` for SIMPLE, actual model ID for MEDIUM+ |
| `executionTimeMs` | Total classification time including subprocess |
| `success` | `false` if classifier failed/timed out |

### Querying Logs

Use the existing routing-stats script:

```bash
# All plugin routing decisions
cat state/routing-log.jsonl | jq 'select(.source == "plugin:intelligent-routing")'

# Stats by tier
node scripts/routing-stats.mjs --source "plugin:intelligent-routing"

# Filter by agent
node scripts/routing-stats.mjs --agent "family:ada"
```

## Troubleshooting

### Classifier always times out
- Check that `intelligent-router-hook.js` exists and is executable
- Check that `router.py` (Python classifier) is working: `node intelligent-router-hook.js "test message"`
- Increase `classifierTimeoutMs` in plugin config

### All messages classified as SIMPLE
- The fast-path might be too aggressive for your messages
- Disable fast-path: set `enableFastPath: false` in plugin config
- Check classifier output directly: `node intelligent-router-hook.js "your message here"`

### Model override not working
- Check `dryRun` is `false`
- Verify the model ID in `config.json` matches what the gateway supports
- Check gateway logs for hook execution errors

### Plugin not loading
- Verify `openclaw plugins list` shows `intelligent-routing` as enabled
- Check that `openclaw.plugin.json` is valid JSON
- Restart gateway: `openclaw gateway restart`

### Logging not working
- Ensure `state/` directory exists and is writable
- The plugin creates parent directories automatically, but check permissions
- Logging failures are silent (they never break the routing pipeline)

## Architecture Notes

- **Fail-safe:** If anything goes wrong (classifier crash, timeout, parse error), the plugin falls back to SIMPLE (no override). This means the worst case is always "use Haiku" — never an error.
- **Zero overhead for SIMPLE:** Fast-path classification runs in <1ms with no subprocess spawn.
- **Compatible with routing-logger.mjs:** Log entries use the same schema as the existing routing logger, so all stats tools work out of the box.
- **Non-blocking logging:** Log writes never throw — if the file is locked or missing, the decision proceeds anyway.
