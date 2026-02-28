---
name: trading-engine
version: 3.0.0
description: Multi-strategy Hyperliquid perpetual futures trading engine
requires:
  env:
    - JARVIS_WALLET
    - HYPERLIQUID_ADDRESS
  binary:
    - node
invocable: true
---

# Trading Engine Skill

Multi-strategy trading engine for Hyperliquid perpetual futures. Strategies are tracked in ontology, execution is agentic.

## Architecture

The agent (trader) uses these CLI tools via `exec`. Each tool is standalone and outputs structured data.

### Tools

| Tool | Purpose | Usage |
|------|---------|-------|
| `hl-account.mjs` | Account info | `node hl-account.mjs balance\|positions\|fills\|orders` |
| `hl-trade.mjs` | Execute trades | `node hl-trade.mjs market-buy\|market-sell\|close --coin X --size Y` |
| `hl-market.mjs` | Market data | `node hl-market.mjs price\|candles\|funding\|overview` |
| `signal-analyze.mjs` | Technical signals | `node signal-analyze.mjs --coin X [--timeframe 5m] [--json]` |
| `whale-monitor.mjs` | Whale tracking | `node whale-monitor.mjs refresh\|scan\|list` |
| `risk-check.mjs` | Pre-trade risk | `node risk-check.mjs --coin X --direction long --size Y --leverage Z` |
| `strategy-ops.mjs` | Strategy CRUD | `node strategy-ops.mjs list\|get\|update\|perf\|init` |
| `daily-report.mjs` | Portfolio report | `node daily-report.mjs [--json]` |
| `postmortem.mjs` | Trade analysis | `node postmortem.mjs [--days N] [--json]` |

### Environment Variables

All tools read from environment:
- `JARVIS_WALLET` — Private key (injected by TPM preboot, never stored on disk)
- `HYPERLIQUID_ADDRESS` — Wallet address
- `ONTOLOGY_URL` — Ontology REST API endpoint (default: `http://127.0.0.1:8100`)
- `ONTOLOGY_API_TOKEN` — Ontology auth token
- `ONTOLOGY_SPEAKER` — Speaker ID for ACL (default: `jarvis-agent`)

### Scripts Location

All scripts are in the `scripts/` directory of this skill:
```
cd ~/.openclaw/workspace/skills/trading-engine/scripts && node <tool>.mjs <command>
```

## Trading Workflow

### 1. Market Scan (every 5 min)
```
1. Load active strategies from ontology (strategy-ops list)
2. For each strategy's coin list:
   a. Run signal-analyze for technical score
   b. If score above threshold → evaluate trade
3. Check whale positions (whale-monitor scan)
4. Combine signals with strategy weights
5. If trade signal: run risk-check → if approved → execute via hl-trade
```

### 2. Sentiment Analysis (every 4h, browser-based)
```
1. Open browser tabs for Twitter accounts (via browser tool)
2. Extract recent tweets from each profile
3. Analyze sentiment per coin using reasoning
4. Factor into strategy decisions
```

### 3. Whale List Refresh (every 5 days)
```
node whale-monitor.mjs refresh
```

### 4. Daily Report (10:00 + 22:00)
```
node daily-report.mjs
```

### 5. Postmortem (21:00 daily)
```
node postmortem.mjs --days 1
```

## Safety Guidelines

- **Always run risk-check before any trade**
- **Never exceed strategy's budget_limit allocation**
- **Log every trade decision with reasoning**
- **Sentiment strategy starts in dry-run until validated**
- **Whale list: portfolio $5K-$100K, positive PnL on 2+ timeframes**

## Strategies

### Scalping (70% technical, 25% sentiment, 5% whales)
Fast in/out, 1-2% targets, 5m timeframe, 5-60 min hold, dynamic leverage 5-10x.

### Sentiment (70% sentiment, 20% whales, 10% technical)
Narrative-driven, 5-10% targets, 15m-1h timeframe, 1-24h hold, 3-5x leverage.
**Starts paused — activate after sentiment pipeline is validated.**

### Copytrading (70% whales, 25% technical, 10% sentiment)
Follow top whale traders, 10% targets, 1h-4h timeframe, 1-48h hold, mirror leverage.
