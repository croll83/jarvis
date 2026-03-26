# Trading Engine v2 — Always-On Swarm Daemon

Base path: `/home/jarvis/.openclaw/workspace/skills/trading-engine-v2`

All commands below run from this directory.

## Daemon Control

```bash
# Start (dry-run)
./scripts/start-daemon.sh --dry-run

# Start (live trading)
./scripts/start-daemon.sh

# Stop
./scripts/stop-daemon.sh

# Restart (stop + start dry-run)
./scripts/stop-daemon.sh && ./scripts/start-daemon.sh --dry-run

# Check if running
ps aux | grep market-daemon | grep -v grep

# Tail live logs
tail -f state/daemon.log
```

## CLI Tools

### Daily Report
```bash
node daemon/daily-report.mjs                    # send to Telegram (last 24h)
node daemon/daily-report.mjs --days 7           # last 7 days
node daemon/daily-report.mjs --stdout           # print only, no Telegram
node daemon/daily-report.mjs --days 3 --stdout  # combine
```

### Strategy Learner
```bash
node daemon/strategy-learner.mjs           # evaluate (default: last 7 days)
node daemon/strategy-learner.mjs --days 14 # custom window
node daemon/strategy-learner.mjs --json    # machine-readable
```

### Strategy Status
```bash
node daemon/strategy-status.mjs                # list all
node daemon/strategy-status.mjs --status active
node daemon/strategy-status.mjs --json
```

### Trade Query
```bash
node daemon/trade-query.mjs                        # last 10 trades
node daemon/trade-query.mjs --coin BTC             # filter by coin
node daemon/trade-query.mjs --strategy Scalping     # filter by strategy
node daemon/trade-query.mjs --mode dry-run          # only dry-run
node daemon/trade-query.mjs --last 20 --days 3      # custom limit + window
node daemon/trade-query.mjs --json
```

### Token Report
```bash
node daemon/token-report.mjs           # today + yesterday
node daemon/token-report.mjs --days 7  # last 7 days
node daemon/token-report.mjs --json
```

## State Files

All in `state/` relative to base path:

- `daemon.log` — daemon console log
- `trade-log.jsonl` — all trades (live + dry-run), one JSON per line
- `token-usage.json` — daily LLM token costs per model
- `strategies.json` — cached strategies from ontology
- `geopolitics.json` — cached Grok geopolitics analysis
- `sentiment.json` — latest sentiment scan (from cron)
- `newsletter.json` — latest newsletter digest

## Architecture

```
WebSocket → SignalRouter (deterministic) → pre-LLM filters
  → N × Scout (Haiku) in parallel per coin
  → 10s buffer → 1 × Analyst (Sonnet) batch call
  → per-trade: Challenger (Gemini) → Risk (Haiku) → Execute
```

**Swarm agents (5):**
- **Scout** (Haiku) — runs in parallel per signal, cheap pre-filter. If SKIP → nothing enters buffer
- **Analyst** (Sonnet) — receives batched Scout results every 10s, sees all signals together for correlation-aware decisions
- **Challenger** (Gemini Flash Lite) — devil's advocate per approved trade, can veto
- **Risk Manager** (Haiku) — margin/sizing validation per trade
- **News Analyst** (Grok) — geopolitics + crypto social sentiment, batch poll every 2h for all 26 assets

**Assets (26):** 15 crypto + 11 XYZ (commodities, equities)

**Pre-LLM filters:** canTrade(coin) → isRelevant(signalType) → scoreSignal(≥40) → no duplicate positions

**Batch window:** 10s sliding window. Scout results buffer until flush. Sonnet called once per batch with all pending signals.

**Scheduled:** geopolitics every 2h, strategy reload every 30m, daily report 09:00/21:00 CET, learner every 1h

## Conversational Commands (via Burry agent)

When the user asks Burry conversational questions, map them to the appropriate tool:

| User says (Italian) | Action |
|---|---|
| "come va il trading?" / "status" | Dashboard API: /api/overview + /api/trades |
| "bilancio" / "equity" / "balance" | Dashboard API: /api/account |
| "posizioni aperte" | Dashboard API: /api/trades → openPositions |
| "quanto ho speso in token?" | Dashboard API: /api/tokens |
| "PnL di oggi/settimana" | CLI: daily-report.mjs --stdout |
| "come vanno le strategie?" | Dashboard API: /api/strategies |
| "storico trade BTC" | CLI: trade-query.mjs --coin BTC --json |
| "riavvia il daemon" | PM2 restart (ask confirmation first) |
| "metti Sentiment in live" | Strategy status change (ask confirmation first) |
| "chiudi la posizione su ETH" | Close position (ask confirmation first) |
| "fai girare il learner" | CLI: strategy-learner.mjs --json |
| "aggiungi ATOM al trading" | Edit ALL_COINS + restart (ask confirmation first) |
| "report giornaliero" | CLI: daily-report.mjs --stdout |
| "geopolitica" | Dashboard API: /api/geopolitics |
