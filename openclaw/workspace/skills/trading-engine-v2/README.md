# Trading Engine v2

Always-on multi-model swarm daemon for **Hyperliquid perpetual futures** trading across crypto assets and XYZ real-world assets (commodities, equities). WebSocket-driven with deterministic signal detection (zero LLM cost for signals) and a 5-model AI swarm pipeline for trade decisions.

Managed via **PM2** (process name: `trading-v2`).

---

## Table of Contents

- [Architecture](#architecture)
- [Signal Pipeline](#signal-pipeline)
- [LLM Integration](#llm-integration)
- [Models](#models)
- [News and Geopolitics](#news-and-geopolitics)
- [Analyst Memory](#analyst-memory)
- [Strategy System](#strategy-system)
- [Strategy Learner](#strategy-learner)
- [Traded Assets](#traded-assets)
- [Directory Structure](#directory-structure)
- [Environment Variables](#environment-variables)
- [External Systems](#external-systems)
- [Dashboard API](#dashboard-api)
- [Deployment](#deployment)
- [OpenClaw Patches (ACP only)](#openclaw-patches-acp-only)
- [Estimated Token Costs](#estimated-token-costs)

---

## Architecture

```
Hyperliquid WebSocket (allMids, trades, candles, user fills/orders)
       |
  SignalRouter -- deterministic: RSI, EMA, MACD, BB, whale, volume, funding, OI
       |            10 signal types, zero LLM tokens
       |
  Scout Buffer -- pre-filter + cooldown (60s per coin)
       |
  N x Scout (Haiku) -- 1 call per coin per flush
       |                  PASS -> analyst buffer, SKIP -> discard
       |
  Analyst Timer (10s) -- batches all PASS signals
       |
  1 x Analyst (Sonnet) -- batch BUY/SELL/HOLD per signal x strategy
       |                    has tools: query_trade_history
       |                    receives rolling window of recent closes (4h)
       |
  Per approved trade:
       +-- Challenger (Gemini Flash Lite) -- devil's advocate, can veto
       +-- Risk Manager (Haiku) -- margin/sizing, can reject
       +-- Execute on Hyperliquid or dry-run log
```

The pipeline is designed so that the most expensive models (Sonnet) only see signals that have already passed deterministic filters and a cheap LLM gate (Haiku). The challenger and risk manager add independent verification before any capital is committed.

---

## Signal Pipeline

All signal detection is **deterministic** -- no LLM tokens are consumed until the Scout stage.

1. **WebSocket ingestion** -- `ws-manager.mjs` maintains persistent connections to Hyperliquid for `allMids`, `trades`, `candles`, and user `fills`/`orders`.
2. **SignalRouter** (`signal-router.mjs`) -- evaluates 10 signal types: RSI divergence, EMA crossover, MACD crossover, Bollinger Band squeeze/breakout, whale activity, volume spike, funding rate extremes, open interest shifts, and composite signals.
3. **Indicators** (`indicators.mjs`) -- pure math: SMA, EMA, RSI, MACD, Bollinger Bands, ATR, VWAP, Stochastic.
4. **Scout Buffer** -- aggregates signals, enforces a 60-second cooldown per coin to avoid flooding the LLM pipeline.
5. **Pre-LLM filters** -- before any signal reaches Scout, the system checks: `canTrade` (asset tradeable), `isRelevant` (matches at least one active strategy), `scoreSignal >= 40` (composite quality threshold), and no duplicate open positions.

---

## LLM Integration

### LLMDirectClient

A zero-dependency client (`lib/llm-direct-client.mjs`) that calls provider APIs directly. It does **not** use OpenClaw's `runEmbeddedPiAgent` -- all calls are native HTTP requests to provider endpoints.

**Supported providers:**

| Provider | Auth Method |
|----------|-------------|
| Anthropic | OAuth token via `Bearer` + `anthropic-beta` header |
| Google Gemini | API key |
| xAI (Grok) | API key |
| OpenRouter | API key |

**Secret resolution:** All API keys are resolved from the TPM vault at init time via the `tpm-secret-resolver.sh` script (JSON stdin/stdout protocol). Required vault keys: `anthropic_token`, `google_api_key`, `xai_api_key`, `openrouter_api_key`.

**Multi-turn tool use:** The analyst agent uses `callWithTools()` for multi-turn conversations with tool invocations (e.g., querying trade history mid-analysis).

---

## Models

| Agent | Model | Provider | Role | Max Tokens |
|-------|-------|----------|------|------------|
| Scout | `claude-haiku-4-5` | Anthropic | Per-coin signal quality filter | 200 |
| Analyst | `claude-sonnet-4-6` | Anthropic | Portfolio-level trade decisions (with tools) | 2048 |
| Challenger | `gemini-3.1-flash-lite-preview` | Google | Devil's advocate, can veto trades | 200 |
| Risk Manager | `claude-haiku-4-5` | Anthropic | Margin/sizing validation | 150 |
| News Analyst | `grok-4-1-fast-non-reasoning` | xAI | Geopolitics + news sentiment (every 2h) | 4096 |

---

## News and Geopolitics

The `news-fetcher.mjs` module scrapes **9 public Telegram channels** for real-time headlines:

**Crypto channels:**
- `cointelegraph`, `unfolded`, `unfolded_defi`, `WatcherGuru`, `cryptobriefing`

**Macro / Geopolitics channels:**
- `financialjuice`, `SkyNews`, `AGI_it`, `toporlive`

**Parameters:**
- Maximum 40 headlines per fetch
- Maximum 10 headlines per channel
- Maximum headline age: 6 hours

Headlines are fed to the **News Analyst** (Grok) every 2 hours, which produces per-asset sentiment scores stored in `state/sentiment.json` and `state/geopolitics.json`.

---

## Analyst Memory

- **Rolling window:** The last 4 hours of closed trades are injected directly into the analyst prompt for immediate context.
- **Tool: `query_trade_history`** -- The analyst can actively query the full trade history for any coin and timeframe during its reasoning process.
- **Scout memory:** Per-coin decision history is persisted in `state/scout-memory/` to avoid re-evaluating identical signal patterns.
- **Analyst memory:** Aggregated in `state/analyst-memory.json`.

---

## Strategy System

Strategies are stored in the **ontology server** (entity type: `Strategy`) and reloaded every 30 minutes.

### Strategy Properties

Each strategy defines:
- **Weights** -- relative importance of technical, sentiment, and whale signals
- **Hold time** -- expected trade duration
- **Leverage** -- position leverage
- **TP/SL** -- take-profit and stop-loss percentages
- **Target assets** -- which assets the strategy applies to

### Strategy Status

| Status | Behavior |
|--------|----------|
| `active` | Trades live on Hyperliquid |
| `dry-run` | Evaluated through the full pipeline (consumes LLM tokens) but not executed |
| `paused` | Completely skipped, zero LLM cost |

### Manual Override

The `_manualOverride` flag prevents the strategy learner from automatically demoting a strategy. Useful for strategies under manual supervision or testing.

---

## Strategy Learner

The strategy learner (`strategy-learner.mjs`) runs **hourly** and evaluates closed trades from `state/trade-log.jsonl`.

**Capabilities:**

- **Demotion:** Failing strategies are automatically moved to `dry-run` (unless `_manualOverride` is set).
- **Promotion:** Dry-run strategies are promoted to `active` if they achieve PF >= 1.2 and WR >= 45% over 20+ trades.
- **Cooldown re-activation:** Demoted strategies can be re-activated after a 3-day cooldown period (non-overridden only).
- **Variant spawning:** Generates variant strategies from failing ones with adjusted weights and widened stops.
- **Memory updates:** Updates Burry agent memory files (`lessons.md`, `performance.md`).

---

## Traded Assets

**26 assets** across two categories:

### Crypto (15)

BTC, ETH, HYPE, SOL, XRP, SUI, DOGE, BNB, PAXG, AVAX, LINK, AAVE, ENA, kPEPE, SEI

### XYZ / Real-World (11)

| Symbol | Asset |
|--------|-------|
| CL | Crude Oil |
| SILVER | Silver |
| BRENTOIL | Brent Oil |
| GOLD | Gold |
| NATGAS | Natural Gas |
| COPPER | Copper |
| TSLA | Tesla |
| HOOD | Robinhood |
| NVDA | Nvidia |
| COIN | Coinbase |
| ORCL | Oracle |

---

## Directory Structure

```
daemon/
  market-daemon.mjs       Entry point: WebSocket, signal buffering, timers
  swarm.mjs               5-model pipeline, prompts, trade execution
  token-counter.mjs       Per-model daily token/cost tracking
  strategy-learner.mjs    Self-learning: promote/demote/tweak strategies
  daily-report.mjs        Performance reports sent to Telegram
  dashboard-server.mjs    HTTP API on port 18800
  strategy-status.mjs     CLI: strategy overview
  trade-query.mjs         CLI: query trade log
  token-report.mjs        CLI: token consumption report

lib/
  llm-direct-client.mjs   Zero-dep LLM client (Anthropic OAuth, Google, xAI, OpenRouter)
  signal-router.mjs       Deterministic signal detection (10 types)
  indicators.mjs          SMA, EMA, RSI, MACD, BB, ATR, VWAP, Stochastic
  hl-client.mjs           Hyperliquid SDK wrapper
  ws-manager.mjs          WebSocket auto-reconnect
  strategy-manager.mjs    Strategy CRUD via ontology API
  position-manager.mjs    Position tracking and management
  news-fetcher.mjs        Telegram channel scraper (9 channels)
  balance-utils.mjs       Balance/position helpers
  llm-task-client.mjs     (legacy, unused)
  llm-client.mjs          (legacy, unused)

cron/                     (empty -- timers are in daemon now)

dashboard/
  index.html              Web monitoring UI

scripts/
  start-daemon.sh         PM2 start script
  stop-daemon.sh          PM2 stop script

state/                    Runtime state (gitignored)
  trade-log.jsonl           Append-only trade log
  token-usage.json          Per-model token consumption
  geopolitics.json          Latest geopolitical analysis
  sentiment.json            Per-asset sentiment scores
  strategies.json           Cached strategy snapshot
  scout-memory/             Per-coin Scout decision history
  analyst-memory.json       Analyst context memory
  strategy-metrics.json     Strategy performance metrics
  daemon.log                Daemon output log
  dry-run-signals.jsonl     Dry-run evaluated signals
```

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `HYPERLIQUID_ADDRESS` | Yes | Wallet address for WebSocket subscriptions |
| `JARVIS_WALLET` | Live only | Private key for signing trades (resolved from TPM vault) |
| `ONTOLOGY_URL` | No | Ontology server endpoint (default: `http://127.0.0.1:8100`) |
| `ONTOLOGY_API_TOKEN` | Yes | Ontology API authentication token (resolved from TPM vault) |
| `DRY_RUN` | No | Set to `true` for dry-run mode (no real trades) |
| `TELEGRAM_BOT_TOKEN` | No | For daily report delivery (resolved from TPM vault) |

All sensitive values are resolved from the TPM vault at runtime. No secrets are stored on disk.

---

## External Systems

| System | Purpose |
|--------|---------|
| **TPM Vault** | Secrets management via `tpm-secret-resolver.sh` (JSON stdin/stdout protocol) |
| **Ontology Server** | Knowledge graph for strategy storage and retrieval (speaker-based ACL) |
| **Hyperliquid** | DEX for perpetual futures execution (WebSocket + REST API) |
| **Telegram Channels** | Public channels scraped for news headlines (read-only) |
| **PM2** | Process manager for daemon lifecycle |

---

## Dashboard API

The dashboard server runs on **port 18800** and exposes the following endpoints:

| Endpoint | Description |
|----------|-------------|
| `GET /api/overview` | System status, active strategies, open positions |
| `GET /api/account` | Account balance and margin info |
| `GET /api/trades` | Recent trade log |
| `GET /api/strategies` | All strategies with status and metrics |
| `GET /api/tokens` | LLM token consumption breakdown |
| `GET /api/geopolitics` | Latest geopolitical analysis |
| `GET /api/sentiment` | Per-asset sentiment scores |
| `GET /api/log` | Recent daemon log entries |

A web UI is available at `http://localhost:18800/` (served from `dashboard/index.html`).

---

## Deployment

1. **Clone or copy** the project to the target machine.

2. **Install dependencies:**
   ```bash
   npm install
   ```
   Only `ws` and `hyperliquid` are required runtime dependencies.

3. **Ensure the TPM vault** contains the required keys:
   - `anthropic_token`, `google_api_key`, `xai_api_key`, `openrouter_api_key`
   - `ontology_api_token`
   - `wallet_private_key` (for live trading)

4. **Ensure the ontology server** is running and accessible at the configured `ONTOLOGY_URL`.

5. **Start the daemon in dry-run mode:**
   ```bash
   pm2 start ecosystem.config.cjs
   ```

6. **Verify** via dashboard:
   ```bash
   curl http://localhost:18800/api/overview
   ```

7. **Go live** (when ready):
   - Set `DRY_RUN=false` in `ecosystem.config.cjs`
   - `pm2 restart trading-v2`

---

## OpenClaw Patches (ACP only)

A patch script is maintained for applying necessary modifications after OpenClaw updates.

**Only 2 patches are required:**

| Patch | Purpose |
|-------|---------|
| Part 1 | Process group kill for orphan cleanup |
| Part 2 | Deterministic ACP session keys for persistence |

Parts 3-5 were removed after migrating to `LLMDirectClient` (direct API calls). The daemon no longer depends on OpenClaw's embedded agent runner for LLM calls.

---

## Estimated Token Costs

Typical daily consumption under normal market conditions:

| Model | Agent(s) | Estimated Daily Cost |
|-------|----------|---------------------|
| Haiku | Scout + Risk Manager | ~$1.50 |
| Sonnet | Analyst | ~$1.50 |
| Gemini Flash Lite | Challenger | ~$0.25 |
| Grok | News Analyst | ~$0.00 |
| **Total** | | **~$3.25/day** |

The deterministic signal detection layer (SignalRouter) consumes zero LLM tokens. Cost scales primarily with market volatility (more signals = more Scout calls).

---

## License

Private and proprietary. Not for redistribution.
