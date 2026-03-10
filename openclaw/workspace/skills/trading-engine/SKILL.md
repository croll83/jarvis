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
| `market-precheck.mjs` | **Pre-trade data collection** | `node market-precheck.mjs [--json]` |
| `save-sentiment.mjs` | Save sentiment to memory | `node save-sentiment.mjs --data '{...}'` |
| `save-scan-output.mjs` | Save scan to buffer (JSONL) | `node save-scan-output.mjs --data '{...}'` |
| `hourly-digest.mjs` | Read scan buffer for digest | `node hourly-digest.mjs [--clear]` |
| `save-hourly-digest.mjs` | Save hourly digest to memory | `node save-hourly-digest.mjs --data '...'` |
| `hl-account.mjs` | Account info | `node hl-account.mjs balance\|positions\|fills\|orders` |
| `hl-trade.mjs` | Execute trades | `node hl-trade.mjs market-buy\|market-sell\|close --coin X --size Y --strategy Z` |
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

### 1. Market Scan (every 3 min, Sonnet)
```
1. Collect all data: node market-precheck.mjs
   → strategies, signals, whales, balance, positions (with duration),
     margin, last 3 sentiment scans, MEMORY.md lessons
2. Analyze and decide:
   - Weight signals per strategy
   - MARGIN RULE: if margin>90%, actively evaluate closing weak positions
     to free margin for stronger signals
   - Output: "close [X,Y] and open [Z,W]: reasoning..."
3. Execute: risk-check → hl-trade (for each trade)
4. Save output to buffer: node save-scan-output.mjs --data '{...}'
```
Total: 2-3 LLM turns. Buffer consumed hourly by Haiku digest.

### 1b. Hourly Digest (every 1h, Haiku)
```
1. Read buffer: node hourly-digest.mjs
2. Summarize last hour (max 15 lines)
3. Save digest: node save-hourly-digest.mjs --data '...'
4. Clear buffer: node hourly-digest.mjs --clear
```
Keeps daily memory compact (~2-5KB/day vs 47KB raw).

### 2. Sentiment Analysis (every 4h, browser-based)
```
1. Open browser tabs for Twitter accounts (via browser tool)
2. Extract recent tweets from each profile
3. Analyze sentiment per coin using llm-task (Grok-4)
4. Save results: pipe JSON to save-sentiment.mjs
   → persists to memory/YYYY-MM-DD.md for Market Scan to read
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
