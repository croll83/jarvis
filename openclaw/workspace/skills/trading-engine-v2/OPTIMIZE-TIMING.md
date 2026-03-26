# Trading Engine V2 — Timing Optimization (Deferred)

## Context

As of 2026-03-16, v2 runs at ~$2.40/day with Part 4+5 patches applied (sessionKey + promptMode="none"). This is sustainable on Claude.ai 5x subscription. The optimizations below are deferred because the latency impact on scalping strategies outweighs the cost savings.

## When to apply

Apply these optimizations when:
- Scalping strategies are disabled or removed
- Costs need further reduction (e.g. subscription pressure)
- Trading shifts to longer hold-time strategies only (copytrading, sentiment)

## Changes to implement

### 1. Scout flush interval: 30s → 120s

**File**: `daemon/market-daemon.mjs`

```js
// Line ~154: change from
const SCOUT_FLUSH_INTERVAL = 30_000;
// To:
const SCOUT_FLUSH_INTERVAL = 120_000;
```

**Effect**: Signals accumulate for 120s per coin before being batched to scout. Scout sees more signals per call → better confluence detection. ~50% fewer scout calls.

**Impact**: +90s max latency on signal detection. BAD for scalping, neutral for longer strategies.

### 2. Remove cooldown (signals must NOT be dropped)

**File**: `daemon/market-daemon.mjs`

```js
// Line ~154-155: remove SCOUT_COOLDOWN_MS entirely
// DELETE: const SCOUT_COOLDOWN_MS = 60_000;

// Line ~164-166 in bufferSignal(): remove the cooldown check
// DELETE:
//   const lastCall = scoutLastCall.get(coin) || 0;
//   if (now - lastCall < SCOUT_COOLDOWN_MS) return;
```

**CRITICAL**: Never drop signals. A strong signal followed by 20 weaker reinforcing signals must ALL be captured. The 120s flush interval provides natural batching without dropping.

### 3. Analyst interval: 60s → 180s

**File**: `daemon/market-daemon.mjs`

```js
// Line ~156: change from
const ANALYST_INTERVAL_MS = 60_000;
// To:
const ANALYST_INTERVAL_MS = 180_000;
```

**Effect**: Analyst runs 3x less often, processes larger batches of scout-passed signals. ~66% fewer Sonnet calls.

**Impact**: +120s max latency on trade execution. BAD for scalping, neutral for longer strategies.

### 4. Aggressive pre-filter: whale $1M+

**File**: `daemon/swarm.mjs`

```js
// Line ~110: change from
const PRE_FILTER_WHALE_MIN = 20_000;
// To:
const PRE_FILTER_WHALE_MIN = 1_000_000;
```

**Effect**: Filters out smaller whale trades before they reach scout LLM. Reduces Haiku calls by ~20-30%.

**Impact**: Misses smaller whale signals that could be meaningful in aggregate. Consider making this configurable per-strategy.

## Projected savings

| Metric | Current | Optimized |
|--------|---------|-----------|
| Haiku calls/day | ~1,678 | ~800 |
| Sonnet calls/day | ~96 | ~32 |
| Daily cost | ~$2.40 | ~$1.21 |
| Max signal-to-trade latency | ~105s | ~315s |

## Latency impact by strategy type

- **Scalping (5-60 min hold)**: NEGATIVE — 315s worst case eats 5+ min of hold window, misses fast breakouts
- **Copytrading (hours/days)**: NEUTRAL — 315s is irrelevant for multi-hour holds
- **Sentiment (hours/days)**: SLIGHTLY POSITIVE — better aggregated context for longer-term decisions

## Recommendation

Only apply when scalping strategies are disabled. The $1.19/day savings does not justify the risk of degraded scalping P&L. If needed, consider applying ONLY the whale pre-filter ($1M+) as it reduces costs without adding latency.
