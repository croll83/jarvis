/**
 * Swarm Orchestrator v3 — Multi-model trading decision pipeline.
 *
 * Pipeline:
 *   Pre-filter (deterministic) → Scout (Haiku, parallel per coin) → Analyst (Sonnet, batch)
 *   → Challenger (Gemini, per trade) → Hard Gates → Execute/DryRun
 *
 * Changes from v2:
 *   - Risk Manager removed (hard gates are sufficient)
 *   - Challenger output affects confidence: veto → confidence -= 15
 *   - PositionManager as source of truth (not JSONL replay)
 *   - Scout/Analyst memory removed (noise, not signal)
 *   - Context gathering is global (not per-first-coin)
 *   - Failed executions never registered as positions
 */

import { TokenCounter } from './token-counter.mjs';
import { HLClient } from '../lib/hl-client.mjs';
import { LLMDirectClient } from '../lib/llm-direct-client.mjs';
import { StrategyManager } from '../lib/strategy-manager.mjs';
import { PositionManager } from '../lib/position-manager.mjs';
import { sendTelegram } from './daily-report.mjs';
import { readFileSync, writeFileSync, existsSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';
import { fetchTelegramNews } from '../lib/news-fetcher.mjs';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const STATE_DIR = resolve(__dirname, '..', 'state');

function loadSentiment() {
  try {
    const file = resolve(STATE_DIR, 'sentiment.json');
    if (!existsSync(file)) return null;
    const data = JSON.parse(readFileSync(file, 'utf8'));
    if (Date.now() - new Date(data.timestamp).getTime() > 6 * 3600_000) return null;
    return data;
  } catch {}
  return null;
}

function loadNewsletter() {
  try {
    const file = resolve(STATE_DIR, 'newsletter.json');
    if (!existsSync(file)) return null;
    const data = JSON.parse(readFileSync(file, 'utf8'));
    if (Date.now() - new Date(data.timestamp).getTime() > 14 * 3600_000) return null;
    return data;
  } catch {}
  return null;
}

function loadTradingMemory() {
  try {
    const dir = '/home/jarvis/.openclaw/workspace-burry/memory';
    const lessons = readFileSync(resolve(dir, 'lessons.md'), 'utf8').trim();
    if (lessons && !lessons.startsWith('# (intentionally')) return lessons;
  } catch {}
  return '';
}

// --- Model configuration ---
const MODELS = {
  scout: { id: 'anthropic/claude-haiku-4-5', tokenKey: 'haiku', role: 'Scout' },
  analyst: { id: 'anthropic/claude-sonnet-4-6', tokenKey: 'sonnet', role: 'Trading Analyst' },
  challenger: { id: 'google/gemini-3.1-flash-lite-preview', tokenKey: 'gemini_flash_lite', role: "Devil's Advocate" },
  news_analyst: { id: 'xai/grok-4-1-fast-non-reasoning', tokenKey: 'grok_fast', role: 'News Analyst' },
};


// --- Analyst tool definitions ---
const ANALYST_TOOLS = [{
  name: "query_trade_history",
  description: "Query closed trade history for a specific coin. Returns last N trades with PnL, strategy, timestamps. Use this when you need more context about past performance on a specific asset before making a decision.",
  input_schema: {
    type: "object",
    properties: {
      coin: { type: "string", description: "The coin to query, e.g. BTC, xyz:TSLA, ETH" },
      limit: { type: "number", description: "Max number of trades to return (default: 15, max: 30)" }
    },
    required: ["coin"]
  }
}];

const TRADE_LOG_FILE = resolve(STATE_DIR, "trade-log.jsonl");

function getAssetClass(coin) {
  if (coin.startsWith('xyz:')) {
    const name = coin.slice(4);
    if (['CL', 'BRENTOIL', 'GOLD', 'SILVER', 'COPPER', 'NATGAS', 'PLATINUM'].includes(name)) return 'commodity';
    if (['EUR', 'JPY'].includes(name)) return 'forex';
    if (['XYZ100'].includes(name)) return 'index';
    return 'equity';
  }
  return 'crypto';
}

// --- Signal detail formatter ---
function formatSignalDetail(signal) {
  let detail = signal.type;
  if (signal.type === 'whale_trade') {
    const side = signal.side === 'B' ? 'BUY' : signal.side === 'A' ? 'SELL' : signal.side || '?';
    const notional = signal.notional ? `$${(signal.notional / 1000).toFixed(0)}k` : '?';
    detail += ` — ${side} ${notional}`;
  } else if (signal.type === 'volume_spike') {
    detail += ` — ${signal.multiplier || '?'}x avg volume`;
  } else if (signal.type === 'rsi_extreme') {
    detail += ` — RSI ${signal.rsi?.toFixed(1) || '?'} (${signal.condition || '?'})`;
  } else {
    const dir = signal.direction || signal.condition || '';
    detail += dir ? ` — ${dir}` : '';
    if (signal.confidence != null) detail += ` (conf: ${signal.confidence})`;
  }
  return detail;
}

// ============================================================
// Pre-filter: deterministic noise rejection (zero tokens)
// ============================================================
const PRE_FILTER_RSI_MIN = 20;
const PRE_FILTER_RSI_MAX = 80;
const PRE_FILTER_VOLUME_MIN = 4.0;
const PRE_FILTER_WHALE_MIN = 750_000;

export function preFilterSignal(signal) {
  if (signal.type === 'rsi_extreme') {
    const rsi = signal.rsi || 50;
    if (rsi > PRE_FILTER_RSI_MIN && rsi < PRE_FILTER_RSI_MAX) {
      return { pass: false, reason: `RSI ${rsi.toFixed(1)} not extreme enough` };
    }
  }
  if (signal.type === 'volume_spike') {
    if ((signal.multiplier || 0) < PRE_FILTER_VOLUME_MIN) {
      return { pass: false, reason: `Volume ${signal.multiplier}x below ${PRE_FILTER_VOLUME_MIN}x` };
    }
  }
  if (signal.type === 'whale_trade') {
    if ((signal.notional || 0) < PRE_FILTER_WHALE_MIN) {
      return { pass: false, reason: `Whale $${signal.notional} below $${PRE_FILTER_WHALE_MIN}` };
    }
  }
  if (!signal.price && signal.type !== 'whale_trade') {
    return { pass: false, reason: 'No price data' };
  }
  return { pass: true };
}


export class SwarmOrchestrator {
  constructor(opts = {}) {
    this.tokenCounter = opts.tokenCounter || new TokenCounter();
    this.hl = opts.hlClient || new HLClient();
    this.llm = opts.llmClient || new LLMDirectClient();
    this.strategyManager = opts.strategyManager || new StrategyManager();
    this.positionManager = opts.positionManager || new PositionManager({ stateDir: STATE_DIR });
    this.dryRun = opts.dryRun || false;

    // Analyst buffer: scout-passed signals waiting for the batch timer
    this._analystBuffer = [];

    // Cache for account context (refreshed per analyst batch, not per scout)
    this._cachedContext = null;
    this._contextAge = 0;
  }

  // ============================================================
  // Scout: evaluates signals for ONE coin (accumulated over flush window)
  // ============================================================

  async runScout(coin, signals, context = {}) {
    const prompt = this._buildScoutPrompt(coin, signals, context);
    const response = await this._llmCall('scout', prompt);
    const result = this._parseScoutResponse(response);

    if (result.verdict === 'SKIP') {
      return { verdict: 'SKIP', brief: result.brief, confidence: result.confidence };
    }

    // PASS → push to analyst buffer
    this._analystBuffer.push({
      coin,
      signals,
      scout: result,
      newsContext: context.geopolitics || null,
      timestamp: new Date().toISOString(),
    });

    return { verdict: 'PASS', brief: result.brief, confidence: result.confidence };
  }

  // ============================================================
  // Analyst: batch processing of all scout-passed signals
  // ============================================================

  async runAnalyst() {
    const buffer = this._analystBuffer.splice(0);
    if (buffer.length === 0) return [];

    // Fresh global context (account, positions, orders)
    const marketContext = await this._gatherContext();
    const strategies = this.strategyManager.getActive();
    if (strategies.length === 0) {
      console.log('[Swarm] Analyst: no active strategies, skipping');
      return [];
    }

    const prompt = this._buildAnalystPrompt(buffer, marketContext, strategies);
    console.log(`[Swarm] ANALYST: ${buffer.length} signals × ${strategies.length} strategies, prompt ~${prompt.length} chars`);

    const response = await this._analystCallWithTools(prompt);
    const decisions = this._parseAnalystResponse(response, buffer);

    // Normalize coin names
    for (const d of decisions) {
      if (d.coin) d.coin = this._normalizeCoin(d.coin);
    }

    // Process each decision: Challenger → Hard Gates → Execute
    const results = [];
    for (const decision of decisions) {
      // Normalize invalid actions to HOLD
      if (!decision || !decision.action || decision.action === 'SKIP' || decision.action === 'RISK_BLOCKED') {
        if (decision?.side && (decision.side === 'long' || decision.side === 'short')) {
          decision.action = decision.side === 'long' ? 'BUY' : 'SELL';
        } else {
          results.push({ action: 'HOLD', coin: decision?.coin || '?', reason: decision?.reason || 'No opportunity' });
          continue;
        }
      }

      if (decision.action === 'HOLD') {
        results.push({ action: 'HOLD', coin: decision.coin, reason: decision.reason || 'Analyst hold' });
        continue;
      }

      // Find strategy
      const strategy = strategies.find(s => s.name === decision.strategy || s.id === decision.strategy);
      const strategyTag = strategy ? { strategyId: strategy.id, strategyName: strategy.name } : {};
      const isDryRun = this.dryRun || (strategy?.status === 'dry-run');

      // --- Challenger (advisory, affects confidence) ---
      let adjustedConfidence = decision.confidence || 50;
      let challengerNote = '';

      const challenge = await this._callChallenger(decision, marketContext);
      challengerNote = challenge.note || '';
      if (challenge.veto) {
        adjustedConfidence -= 15;
        console.log(`[Swarm] Challenger veto on ${decision.coin}: ${challenge.reason} → confidence ${decision.confidence} → ${adjustedConfidence}`);
      }

      // --- Set reference price + compute deterministic TP/SL ---
      const signalEntry = buffer.find(b => this._normalizeCoin(b.coin) === decision.coin || b.coin === decision.coin);
      let refPrice = signalEntry?.signals?.[0]?.price || 0;
      if (!refPrice) {
        try { refPrice = await this.hl.getPrice(decision.coin); } catch {}
      }
      decision.refPrice = refPrice || 0;

      // Compute TP/SL deterministically from strategy config — NEVER trust analyst LLM
      const tpPct = strategy?.config?.tp_pct || 2;
      const slPct = strategy?.config?.sl_pct || 2;
      if (decision.refPrice > 0) {
        const oldTp = decision.tp;
        const oldSl = decision.sl;
        if (decision.side === 'long') {
          decision.tp = parseFloat((decision.refPrice * (1 + tpPct / 100)).toPrecision(6));
          decision.sl = parseFloat((decision.refPrice * (1 - slPct / 100)).toPrecision(6));
        } else {
          decision.tp = parseFloat((decision.refPrice * (1 - tpPct / 100)).toPrecision(6));
          decision.sl = parseFloat((decision.refPrice * (1 + slPct / 100)).toPrecision(6));
        }
        console.log(`[Swarm] TP/SL: ${decision.coin} ${decision.side} ref=${decision.refPrice} → tp=${decision.tp} sl=${decision.sl} (${tpPct}%/${slPct}%) [analyst had tp=${oldTp}/sl=${oldSl}]`);
      }

      // --- Hard gates (deterministic) ---
      const gateResult = this._applyHardGates(decision, adjustedConfidence, marketContext, strategy);
      if (gateResult.blocked) {
        results.push({ action: 'RISK_BLOCKED', coin: decision.coin, reason: gateResult.reason, ...strategyTag });
        continue;
      }

      const tradeSize = gateResult.clampedSize;
      const clampedLeverage = gateResult.clampedLeverage;

      const finalTrade = {
        action: decision.action,
        coin: decision.coin,
        side: decision.side,
        size: tradeSize,
        tp: decision.tp,
        sl: decision.sl,
        leverage: clampedLeverage,
        reason: decision.reason,
        confidence: adjustedConfidence,
        signalType: decision.signalType || 'unknown',
        challengerNote,
        ...strategyTag,
      };

      if (isDryRun) {
        // Dry-run: get current price, register position, no HL execution
        let entryPrice = null;
        try { entryPrice = await this.hl.getPrice(decision.coin); } catch {}

        // Recompute TP/SL from actual price (signal price may be stale)
        if (entryPrice > 0) {
          if (finalTrade.side === 'long') {
            finalTrade.tp = parseFloat((entryPrice * (1 + tpPct / 100)).toPrecision(6));
            finalTrade.sl = parseFloat((entryPrice * (1 - slPct / 100)).toPrecision(6));
          } else {
            finalTrade.tp = parseFloat((entryPrice * (1 - tpPct / 100)).toPrecision(6));
            finalTrade.sl = parseFloat((entryPrice * (1 + slPct / 100)).toPrecision(6));
          }
        }

        const { opened } = this.positionManager.open({
          ...finalTrade,
          price: entryPrice,
          dryRun: true,
          timestamp: new Date().toISOString(),
        });

        if (opened) {
          this._notifyOpen(finalTrade, entryPrice, true);
        }
        results.push({ ...finalTrade, executed: false, dryRun: true, price: entryPrice, opened });
        continue;
      }

      // Live execution
      const execResult = await this._executeTrade(finalTrade);
      if (execResult.executed) {
        // Recompute TP/SL from ACTUAL fill price (signal price may be stale)
        const fillPrice = execResult.price;
        if (fillPrice > 0) {
          if (finalTrade.side === 'long') {
            finalTrade.tp = parseFloat((fillPrice * (1 + tpPct / 100)).toPrecision(6));
            finalTrade.sl = parseFloat((fillPrice * (1 - slPct / 100)).toPrecision(6));
          } else {
            finalTrade.tp = parseFloat((fillPrice * (1 - tpPct / 100)).toPrecision(6));
            finalTrade.sl = parseFloat((fillPrice * (1 + slPct / 100)).toPrecision(6));
          }
          console.log(`[Swarm] TP/SL recomputed from fill: ${finalTrade.coin} ${finalTrade.side} fill=${fillPrice} → tp=${finalTrade.tp} sl=${finalTrade.sl}`);
        }

        // Register with PositionManager only on successful execution
        this.positionManager.open({
          ...finalTrade,
          price: execResult.price,
          sizeUnits: execResult.sizeUnits,
          dryRun: false,
          execResult: execResult.result,
          timestamp: new Date().toISOString(),
        });

        this._notifyOpen(finalTrade, execResult.price, false);

        // Strategy attribution
        if (strategy) {
          const oid = execResult.result?.response?.data?.statuses?.[0]?.resting?.oid
            || execResult.result?.response?.data?.statuses?.[0]?.filled?.oid || null;
          // attributeTrade disabled — trade-log.jsonl is the single source of truth
        }
      } else {
        console.log(`[Swarm] Execution FAILED for ${decision.coin}: ${execResult.error} — NOT registered as position`);
      }

      results.push({ ...finalTrade, ...execResult });
    }

    return results;
  }

  get analystBufferSize() {
    return this._analystBuffer.length;
  }

  // ============================================================
  // Hard gates (deterministic, no LLM)
  // ============================================================

  _applyHardGates(decision, confidence, marketContext, strategy) {
    const MIN_CONFIDENCE = 70;
    const MIN_NOTIONAL = 50;
    const MAX_MARGIN_PCT = 80;
    const MAX_SIZE_PCT = 20;
    const MIN_TP_DISTANCE_PCT = 0.3; // TP must be at least 0.3% from price

    // Confidence
    if (confidence < MIN_CONFIDENCE) {
      return { blocked: true, reason: `Confidence ${confidence}% < ${MIN_CONFIDENCE}%` };
    }

    // TP/SL sanity — final safety net (TP/SL are now computed deterministically before hard gates)
    const tp = parseFloat(decision.tp);
    const sl = parseFloat(decision.sl);
    const refPrice = parseFloat(decision.refPrice || 0);
    if (!refPrice || refPrice <= 0) {
      return { blocked: true, reason: 'No reference price — cannot validate TP/SL' };
    }
    if (!tp || !sl) {
      return { blocked: true, reason: 'Missing TP or SL' };
    }
    // Final safety: TP/SL must be on correct side of price
    const isLong = decision.side === 'long';
    if (isLong) {
      if (tp <= refPrice) return { blocked: true, reason: `TP ${tp} not above price ${refPrice} for long` };
      if (sl >= refPrice) return { blocked: true, reason: `SL ${sl} not below price ${refPrice} for long` };
    } else {
      if (tp >= refPrice) return { blocked: true, reason: `TP ${tp} not below price ${refPrice} for short` };
      if (sl <= refPrice) return { blocked: true, reason: `SL ${sl} not above price ${refPrice} for short` };
    }

    // Paused strategy
    if (strategy?.status === 'paused') {
      return { blocked: true, reason: `Strategy "${strategy.name}" is paused` };
    }

    // Position limit per strategy
    if (strategy) {
      const maxPos = strategy.config?.max_positions || 3;
      const currentPos = this.positionManager.countByStrategy(strategy.id);
      if (currentPos >= maxPos) {
        return { blocked: true, reason: `${strategy.name} full: ${currentPos}/${maxPos}` };
      }
    }

    // Duplicate position check (same strategy)
    if (this.positionManager.has(decision.coin, strategy?.id)) {
      return { blocked: true, reason: `Already have ${decision.coin} for ${strategy?.name || 'default'}` };
    }

    // Cross-strategy conflict: HL has ONE position per coin — prevent different strategies
    // from opening the same coin (live only; dry-run can coexist)
    const isDryRunTrade = this.dryRun || (strategy?.status === 'dry-run');
    if (!isDryRunTrade) {
      const existingLive = this.positionManager.getByCoin(decision.coin).filter(p => !p.dryRun);
      if (existingLive.length > 0) {
        const existingStrat = existingLive[0].strategyName || '?';
        return { blocked: true, reason: `${decision.coin} already live under ${existingStrat} — HL single-position conflict` };
      }
    }

    // Size clamping
    const equity = parseFloat(marketContext.account?.equity) || 0;
    const maxSizeUsd = equity > 0 ? equity * (MAX_SIZE_PCT / 100) : 1000;
    let tradeSize = decision.size || 0;
    if (tradeSize > maxSizeUsd) tradeSize = parseFloat(maxSizeUsd.toFixed(2));
    if (tradeSize < MIN_NOTIONAL) {
      return { blocked: true, reason: `Size $${tradeSize} < $${MIN_NOTIONAL} minimum` };
    }

    // Leverage clamping
    const maxLev = strategy?.config?.leverage?.max || 5;
    const clampedLeverage = Math.min(decision.leverage || maxLev, maxLev);

    // Margin check
    const marginUsed = parseFloat(marketContext.account?.marginUsed) || 0;
    const tradeMargin = tradeSize / clampedLeverage;
    if (equity > 0 && (marginUsed + tradeMargin) > equity * (MAX_MARGIN_PCT / 100)) {
      return { blocked: true, reason: `Margin ${((marginUsed + tradeMargin) / equity * 100).toFixed(0)}% > ${MAX_MARGIN_PCT}%` };
    }

    return { blocked: false, clampedSize: tradeSize, clampedLeverage };
  }

  // ============================================================
  // Challenger
  // ============================================================

  async _callChallenger(analysis, context) {
    const prompt = this._buildChallengerPrompt(analysis, context);
    const response = await this._llmCall('challenger', prompt);
    return this._parseChallengerResponse(response);
  }

  // ============================================================
  // Geopolitics polling
  // ============================================================

  async pollGeopolitics(allCoins) {
    const coinList = allCoins.map(c => `${c.replace('xyz:', '')} (${getAssetClass(c)})`).join(', ');

    // Fetch real news headlines from public Telegram channels
    let newsBlock = '';
    try {
      const news = await fetchTelegramNews({ maxPerChannel: 10, maxAgeHours: 6 });
      if (news.headlines.length > 0) {
        const numbered = news.headlines.map((h, i) => `${i + 1}. ${h.substring(0, 200)}`).join('\n');
        newsBlock = `RECENT NEWS (last 6h from ${news.sources.join(', ')}):
${numbered}

Based on THESE specific news items, rate these assets: ${coinList}.
Only rate based on the news above. If no relevant news for an asset, confidence should be low (<30).`;
        console.log(`[Swarm] pollGeopolitics: fetched ${news.headlines.length} headlines from ${news.sources.join(', ')}`);
      }
    } catch (e) {
      console.log(`[Swarm] pollGeopolitics: news fetch failed (${e.message}), using fallback`);
    }

    const prompt = newsBlock
      ? `${newsBlock}

Per asset: BULLISH/BEARISH/NEUTRAL + confidence 0-100 + max 5-word factor referencing specific news.

COMPACT JSON only, no whitespace, factors max 5 words:
{"summary":"<10 words>","analysis":{"BTC":{"r":"B","c":70,"f":"SEC crypto scope reduction"},...}}
r=rating(B/E/N for BULLISH/BEARISH/NEUTRAL), c=confidence, f=factor`
      : `Rate these assets on current news/geopolitics: ${coinList}.

Per asset: BULLISH/BEARISH/NEUTRAL + confidence 0-100 + max 5-word factor.
Crypto: whale alerts, regulatory, protocol news. Commodities/equities: geopolitics, supply, macro.

COMPACT JSON only, no whitespace, factors max 5 words:
{"summary":"<10 words>","analysis":{"BTC":{"r":"B","c":70,"f":"ETF inflows strong"},...}}
r=rating(B/E/N for BULLISH/BEARISH/NEUTRAL), c=confidence, f=factor`;

    const response = await this._llmCall('news_analyst', prompt);
    if (!response) return null;

    try {
      const parsed = this._extractJSON(response);
      if (parsed?.analysis) {
        const RATING_MAP = { B: 'BULLISH', E: 'BEARISH', N: 'NEUTRAL' };
        for (const [, val] of Object.entries(parsed.analysis)) {
          if (val.r && !val.rating) {
            val.rating = RATING_MAP[val.r] || val.r;
            val.confidence = val.c || 50;
            val.factor = val.f || '';
            delete val.r; delete val.c; delete val.f;
          }
        }
        const result = { ...parsed, timestamp: new Date().toISOString() };
        writeFileSync(resolve(STATE_DIR, 'geopolitics.json'), JSON.stringify(result, null, 2));
        return result;
      }
    } catch (e) {
      console.error(`[Swarm] pollGeopolitics error: ${e.message}`);
    }
    return null;
  }

  // ============================================================
  // Prompt builders
  // ============================================================

  _buildScoutPrompt(coin, signals, context) {
    const signalLines = signals.map(s => `- ${formatSignalDetail(s)} @ ${s.price || 'N/A'}`).join('\n');

    let prompt = `Scout ${coin}. ${signals.length} signal(s).
${signalLines}`;

    if (context.existingPosition) prompt += `\nPOS: ${context.existingPosition.side} $${context.existingPosition.size}`;
    if (context.geopolitics) {
      const geo = typeof context.geopolitics === 'object' ? (context.geopolitics.rating || context.geopolitics.r || '') : context.geopolitics;
      if (geo) prompt += `\nNEWS: ${geo}`;
    }

    const sentiment = loadSentiment();
    if (sentiment?.scores) {
      const cs = sentiment.scores.find(s => s.coin === coin.replace('xyz:', '').replace(/-PERP$/, ''));
      if (cs) prompt += `\nSENT: ${cs.score}/100 ${cs.trend}`;
    }

    prompt += `\nRate signal quality. Single weak signal=SKIP. Contradictory=SKIP. Default=SKIP.
JSON:{"verdict":"PASS"|"SKIP","brief":"<8 words>","confidence":<0-100>,"direction":"BULLISH"|"BEARISH"|"MIXED"}`;
    return prompt;
  }

  _buildAnalystPrompt(buffer, marketContext, strategies) {
    const equity = parseFloat(marketContext.account?.equity) || 0;
    const maxSize = Math.round(equity * 0.20);

    // All open positions (live + dry-run) from PositionManager
    const allPositions = this.positionManager.getAll().map(p => ({
      coin: p.coin, side: p.side, size: p.size, strategy: p.strategyName,
      dryRun: p.dryRun, entryPrice: p.entryPrice,
    }));

    let prompt = `Senior trading analyst. ${buffer.length} pre-screened signal group(s) from Scouts.
For EACH signal, evaluate against ALL ${strategies.length} strategies. Multiple trades from same signal allowed (different strategies).

ACCOUNT:
- Equity: $${equity.toFixed(2)} | Available: $${marketContext.account?.available} | Margin: ${marketContext.account?.marginRatio}
- MAX trade size: $${maxSize} (20% equity, system-enforced)

OPEN POSITIONS: ${JSON.stringify(allPositions)}

STRATEGIES:`;

    for (const strat of strategies) {
      const c = strat.config;
      const posCount = this.positionManager.countByStrategy(strat.id);
      prompt += `\n\n--- ${strat.name} (${strat.status}) ---`;
      prompt += `\nWEIGHTS: tech=${c.weights?.technical || 0}% sent=${c.weights?.sentiment || 0}% whale=${c.weights?.whales || 0}%`;
      prompt += `\nHOLD: ${c.hold_time?.min || '?'}-${c.hold_time?.max || '?'} ${c.hold_time?.unit || 'min'} | LEV: ${c.leverage?.base || 3}-${c.leverage?.max || 5}x`;
      prompt += `\nTP=${c.tp_pct || 2}% SL=${c.sl_pct || 2}% | Positions: ${posCount}/${c.max_positions || 3}`;
      if (strat.performance) prompt += ` | PnL=$${strat.performance.pnl || 0} WR=${strat.performance.winRate || 0}%`;
    }

    
    // Recent closes (rolling window)
    const recentCloses = this._getRecentCloses(4);
    if (recentCloses.length > 0) {
      const shown = recentCloses.slice(-15);
      const closeLines = shown.map(c => {
        const ago = Math.round((Date.now() - new Date(c.timestamp).getTime()) / 60_000);
        const agoStr = ago >= 60 ? (ago / 60).toFixed(1) + "h ago" : ago + "m ago";
        const pnlStr = c.pnl >= 0 ? "+" + c.pnl.toFixed(2) + "%" : c.pnl.toFixed(2) + "%";
        return c.coin + ": " + c.side.toUpperCase() + " closed " + pnlStr + " " + agoStr + " (" + c.reason + ") [" + c.strategy + "]";
      }).join("\n");
      prompt += "\n\nRECENT CLOSES (last 4h):\n" + closeLines;
    } else {
      prompt += "\n\nRECENT CLOSES: none";
    }

prompt += `\n\nSIGNALS:`;
    for (let i = 0; i < buffer.length; i++) {
      const entry = buffer[i];
      const details = entry.signals.map(s => formatSignalDetail(s)).join('; ');
      prompt += `\n[${i + 1}] ${entry.coin} Scout:${entry.scout.confidence}% ${entry.scout.direction || '?'} — ${entry.scout.brief} | ${details} | Price:${entry.signals[0].price || '?'}`;
    }

    const sentiment = loadSentiment();
    if (sentiment?.macro?.length > 0) prompt += `\nMACRO: ${sentiment.macro.join('; ')}`;

    prompt += `

DEFAULT: HOLD. Only BUY/SELL when confidence ≥70 AND ≥2 confirming signals align with strategy weights.
Do NOT return RISK_BLOCKED/SKIP — use HOLD to reject. No duplicates (same coin+strategy open).

TP/SL: System auto-computes from strategy config. Set tp=0 and sl=0.
"size" = USD amount. System enforces: max $${maxSize}, leverage cap, confidence ≥70%, margin ≤80%.

JSON array:
[{"coin":"<coin>","strategy":"<name>","action":"BUY|SELL|HOLD","side":"long|short","size":<usd>,"leverage":<int>,"tp":0,"sl":0,"confidence":<0-100>,"reason":"<8 words>"},...]`;
    return prompt;
  }

  _buildChallengerPrompt(analysis, context) {
    const positions = this.positionManager.getAll().map(p => ({
      coin: p.coin, side: p.side, strategy: p.strategyName,
    }));

    const margin = analysis.size / (analysis.leverage || 1);
    const equityVal = parseFloat(context.account?.equity) || 1;
    const marginPct = (margin / equityVal * 100).toFixed(1);
    return `Advisory risk review for proposed trade. Your concerns are noted and MAY reduce the trade's confidence score, potentially blocking it.

PROPOSED: ${analysis.coin} ${analysis.action} ${analysis.side} $${analysis.size} notional @${analysis.leverage}x leverage — ${analysis.reason}
MARGIN REQUIRED: $${margin.toFixed(0)} (${marginPct}% of equity) — this is the actual capital at risk, NOT the notional size
ACCOUNT: equity=$${context.account?.equity} available=$${context.account?.available} | positions: ${positions.length} open
POSITIONS: ${JSON.stringify(positions)}

IMPORTANT: $${analysis.size} is the NOTIONAL position size. With ${analysis.leverage}x leverage, only $${margin.toFixed(0)} margin is locked.
Do NOT veto based on notional size exceeding equity — that is normal and expected with leverage.

Flag concerns ONLY for:
- Overtrading (too many correlated positions)
- Bad risk/reward ratio
- Signal quality issues
- Contradicting market conditions

JSON only:
{"veto":true|false,"reason":"<max 10 words if concern>","note":"<max 10 words>"}
veto:true ONLY for critical risks.`;
  }

  // ============================================================
  // Response parsers
  // ============================================================

  _parseScoutResponse(response) {
    try {
      const json = this._extractJSON(response);
      if (!json) return { verdict: 'SKIP', brief: 'Parse failed', confidence: 0 };
      const verdict = (json.verdict || 'SKIP').toUpperCase();
      if (verdict !== 'PASS' && verdict !== 'LONG' && verdict !== 'SHORT') {
        return { verdict: 'SKIP', brief: json.brief || 'No opportunity', confidence: json.confidence || 0 };
      }
      return {
        verdict: 'PASS',
        brief: json.brief || '',
        confidence: json.confidence || 50,
        direction: json.direction || (verdict === 'LONG' ? 'BULLISH' : verdict === 'SHORT' ? 'BEARISH' : 'MIXED'),
      };
    } catch {
      return { verdict: 'SKIP', brief: 'Parse failed', confidence: 0 };
    }
  }

  _parseAnalystResponse(response, buffer) {
    if (!response) return buffer.map(b => ({ action: 'HOLD', coin: b.coin, reason: 'No analyst response' }));
    try {
      const json = this._extractJSON(response);
      if (Array.isArray(json)) return json;
      if (json?.action) return [json];
      if (Array.isArray(json?.decisions)) return json.decisions;
      if (Array.isArray(json?.trades)) return json.trades;
      console.error(`[Swarm] Analyst: unexpected JSON shape`);
    } catch (e) {
      console.error(`[Swarm] Analyst parse error: ${e.message}`);
    }
    return buffer.map(b => ({ action: 'HOLD', coin: b.coin, reason: 'Parse failed' }));
  }

  _parseChallengerResponse(response) {
    try {
      const json = this._extractJSON(response);
      return { veto: json?.veto || false, reason: json?.reason || '', note: json?.note || '' };
    } catch {
      return { veto: false, reason: '', note: 'Parse failed' };
    }
  }

  _normalizeCoin(coin) {
    if (!coin) return coin;
    if (coin.startsWith('xyz:')) return coin;
    const bare = coin.replace(/-PERP$/, '').replace(/-SPOT$/, '');
    if (this.hl._xyzAssets?.some(a => a.name === bare)) return `xyz:${bare}`;
    return coin;
  }

  _extractJSON(text) {
    if (!text) return null;
    try { return JSON.parse(text); } catch {}
    const match = text.match(/```(?:json)?\s*([\s\S]*?)(?:```|$)/);
    if (match) try { return JSON.parse(match[1].trim()); } catch {}
    const arrayMatch = text.match(/\[[\s\S]*\]/);
    if (arrayMatch) try { return JSON.parse(arrayMatch[0]); } catch {}
    const braceMatch = text.match(/\{[\s\S]*\}/);
    if (braceMatch) try { return JSON.parse(braceMatch[0]); } catch {}
    // Salvage truncated array
    const partialArray = text.match(/\[\s*\{[\s\S]*/);
    if (partialArray) {
      let s = partialArray[0];
      const lastBrace = s.lastIndexOf('}');
      if (lastBrace > 0) {
        s = s.substring(0, lastBrace + 1) + ']';
        try { return JSON.parse(s); } catch {}
      }
    }
    return null;
  }

  // ============================================================
  // LLM call abstraction
  // ============================================================


  /**
   * Analyst LLM call with tool support (query_trade_history).
   */
  async _analystCallWithTools(prompt) {
    const model = MODELS.analyst;
    const maxTokens = 2048;
    const timeoutMs = 120_000;

    try {
      const result = await this.llm.callWithTools(
        model.id,
        prompt,
        ANALYST_TOOLS,
        (name, input) => this._handleAnalystTool(name, input),
        { maxTokens, timeoutMs, maxTurns: 3 }
      );
      this.tokenCounter.record(
        model.tokenKey,
        result.inputTokens,
        result.outputTokens,
        result.cacheWriteTokens,
        result.cacheReadTokens
      );
      return result.content;
    } catch (e) {
      console.error("[Swarm] " + model.role + " (with tools) failed: " + e.message);
      return null;
    }
  }

  async _llmCall(agentKey, prompt) {
    const model = MODELS[agentKey];
    if (!model) throw new Error(`Unknown agent: ${agentKey}`);

    const TOKEN_LIMITS = { scout: 120, challenger: 150, analyst: 2048, news_analyst: 4096 };
    const maxTokens = TOKEN_LIMITS[agentKey] || 512;

    try {
      const timeoutMs = (agentKey === 'news_analyst' || agentKey === 'analyst') ? 120_000 : undefined;
      const result = await this.llm.call(model.id, prompt, { maxTokens, timeoutMs });
      this.tokenCounter.record(model.tokenKey, result.inputTokens, result.outputTokens, result.cacheWriteTokens, result.cacheReadTokens);
      return result.content;
    } catch (e) {
      console.error(`[Swarm] ${model.role} (${model.id}) failed: ${e.message}`);
      return null;
    }
  }

  // ============================================================
  // Trade execution (live mode)
  // ============================================================

  async _executeTrade(trade) {
    try {
      const price = await this.hl.getPrice(trade.coin);
      if (!price || isNaN(price)) throw new Error(`Cannot get price for ${trade.coin}`);

      const szDecimals = this.hl.getSzDecimals(trade.coin);
      const sizeUnits = parseFloat((trade.size / price).toFixed(szDecimals));
      //       console.log(`[Swarm] DEBUG _executeTrade: coin=${trade.coin} tradeSize=$${trade.size} price=${price} szDec=${szDecimals} sizeUnits=${sizeUnits} notional=$${(sizeUnits*price).toFixed(2)} lev=${trade.leverage}`);

      if (trade.leverage) {
        await this.hl.setLeverage(trade.coin, trade.leverage, 'isolated').catch(e => {
          console.error(`[Swarm] setLeverage failed: ${e.message}`);
        });
      }

      const result = trade.side === 'long'
        ? await this.hl.marketBuy(trade.coin, sizeUnits, 0.5)
        : await this.hl.marketSell(trade.coin, sizeUnits, 0.5);

      // Verify the order was actually filled
      const status = result?.response?.data?.statuses?.[0];
      const filled = status?.filled;
      const resting = status?.resting;

      if (!filled && !resting) {
        const errMsg = status?.error || 'Order not filled or resting';
        console.error(`[Swarm] Order rejected for ${trade.coin}: ${errMsg}`);
        return { executed: false, error: errMsg };
      }

      const fillPrice = filled?.avgPx ? parseFloat(filled.avgPx) : price;

      return { executed: true, price: fillPrice, sizeUnits, result };
    } catch (e) {
      console.error(`[Swarm] Execution failed for ${trade.coin}: ${e.message}`);
      return { executed: false, error: e.message };
    }
  }

  // ============================================================
  // Telegram notifications
  // ============================================================

  _notifyOpen(trade, price, isDryRun) {
    const tag = isDryRun ? '🧪 DRY-RUN' : '🟢 LIVE';
    const side = trade.side === 'long' ? 'LONG' : 'SHORT';
    const strat = trade.strategyName ? ` [${trade.strategyName}]` : '';
    const priceStr = price ? `$${parseFloat(price).toFixed(2)}` : '?';
    const note = trade.challengerNote ? `\n⚠️ ${trade.challengerNote}` : '';
    sendTelegram(`${tag} <b>OPEN ${trade.coin}</b> ${side}${strat}\nSize: $${trade.size} @ ${priceStr}\nLev: ${trade.leverage || '?'}x | TP/SL: ${trade.tp || '?'}/${trade.sl || '?'}${note}`).catch(() => {});
  }

  // ============================================================
  // Context gathering (global, not per-coin)
  // ============================================================


  /**
   * Read recent CLOSE entries from trade log (last N hours, tail-read for efficiency).
   */
  _getRecentCloses(hours = 4) {
    try {
      if (!existsSync(TRADE_LOG_FILE)) return [];
      const raw = readFileSync(TRADE_LOG_FILE, "utf8");
      const lines = raw.trim().split("\n");
      const tail = lines.slice(-200);
      const cutoff = Date.now() - hours * 3600_000;
      const closes = [];
      for (let i = tail.length - 1; i >= 0; i--) {
        try {
          const entry = JSON.parse(tail[i]);
          if (entry.action !== "CLOSE") continue;
          const ts = new Date(entry.timestamp).getTime();
          if (ts < cutoff) break;
          closes.push({
            coin: entry.coin,
            side: entry.side,
            pnl: entry.pnl,
            strategy: entry.strategyName || "?",
            timestamp: entry.timestamp,
            reason: entry.closeReason || "?",
          });
        } catch {}
      }
      return closes.reverse();
    } catch {
      return [];
    }
  }

  /**
   * Handle analyst tool calls.
   */
  _handleAnalystTool(name, input) {
    if (name === "query_trade_history") {
      const queryCoin = input.coin || "";
      const limit = Math.min(Math.max(input.limit || 15, 1), 30);
      try {
        if (!existsSync(TRADE_LOG_FILE)) return "No trade history available.";
        const raw = readFileSync(TRADE_LOG_FILE, "utf8");
        const lines = raw.trim().split("\n");
        const tail = lines.slice(-500);
        const matches = [];
        for (let i = tail.length - 1; i >= 0 && matches.length < limit; i--) {
          try {
            const entry = JSON.parse(tail[i]);
            if (entry.action !== "CLOSE") continue;
            const normEntry = this._normalizeCoin(entry.coin?.replace(/-PERP$/, ""));
            const normQuery = this._normalizeCoin(queryCoin.replace(/-PERP$/, ""));
            const bareEntry = (entry.coin || "").replace(/-PERP$/, "").replace(/^xyz:/, "");
            const bareQuery = queryCoin.replace(/-PERP$/, "").replace(/^xyz:/, "");
            if (normEntry !== normQuery && bareEntry.toUpperCase() !== bareQuery.toUpperCase()) continue;
            const holdMs = new Date(entry.timestamp).getTime() - new Date(entry.openedAt || entry.timestamp).getTime();
            const holdMin = Math.round(holdMs / 60_000);
            const holdStr = holdMin >= 60 ? (holdMin / 60).toFixed(1) + "h" : holdMin + "m";
            const pnlSign = entry.pnl >= 0 ? "+" : "";
            matches.push(entry.timestamp?.slice(0, 16) + " " + (entry.side || "?").toUpperCase() + " " + entry.entryPrice + "\u2192" + entry.closePrice + " PnL:" + pnlSign + "$" + (entry.pnl?.toFixed(2) || "0") + " [" + (entry.strategyName || "?") + "] hold:" + holdStr + " (" + (entry.closeReason || "?") + ")");
          } catch {}
        }
        if (matches.length === 0) return "No closed trades found for " + queryCoin + ".";
        return queryCoin + " \u2014 last " + matches.length + " closes:\n" + matches.join("\n");
      } catch (e) {
        return "Error reading trade history: " + e.message;
      }
    }
    return "Unknown tool: " + name;
  }

  async _gatherContext() {
    // Cache for 30s to avoid hammering HL API
    if (this._cachedContext && Date.now() - this._contextAge < 30_000) {
      return this._cachedContext;
    }

    try {
      const [balance, positions, orders, fills] = await Promise.all([
        this.hl.getBalance(),
        this.hl.getPositions(),
        this.hl.getOrders().catch(() => []),
        this.hl.getFills(5).catch(() => []),
      ]);

      this._cachedContext = {
        account: {
          equity: balance.overview,
          available: balance.availableForTrading,
          marginUsed: balance.perps.marginUsed,
          marginRatio: balance.perps.marginRatio,
        },
        positions: positions.map(p => ({
          coin: p.coin, side: p.side, size: p.size, pnl: p.pnl, leverage: p.leverage,
        })),
        openOrders: orders.map(o => ({ coin: o.coin, side: o.side, size: o.size, price: o.price })),
        recentFills: fills.map(f => ({ coin: f.coin, side: f.side, price: f.price, pnl: f.pnl })),
      };
      this._contextAge = Date.now();
      return this._cachedContext;
    } catch (e) {
      return this._cachedContext || { account: {}, positions: [], openOrders: [], recentFills: [] };
    }
  }
}
