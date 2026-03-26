/**
 * Swarm Orchestrator v2 — Multi-model trading decision pipeline.
 *
 * Architecture:
 *   Pre-filter (Node.js) → Scout (Haiku x26, per-coin) → Analyst (Sonnet x1, timer) → Challenger → Risk Manager → Execute
 *
 * Scout: accumulates signals per coin over 30s, evaluates batch strength, PASS/SKIP.
 *        Has memory of last 10 decisions. Does NOT assign strategies.
 * Analyst: runs every 60s on a timer, reads ALL scout-passed signals from buffer,
 *          loads ALL active strategies from ontology, decides per-signal × per-strategy.
 *          Can produce multiple trades from the same signal for different strategies.
 *          Has memory of last 30 runs.
 *
 * Token usage is tracked via TokenCounter.
 */

import { TokenCounter } from './token-counter.mjs';
import { HLClient } from '../lib/hl-client.mjs';
import { LLMTaskClient } from '../lib/llm-task-client.mjs';
import { StrategyManager } from '../lib/strategy-manager.mjs';
import { sendTelegram } from './daily-report.mjs';
import { readFileSync, writeFileSync, appendFileSync, existsSync, mkdirSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const STATE_DIR = resolve(__dirname, '..', 'state');
const SCOUT_MEMORY_DIR = resolve(STATE_DIR, 'scout-memory');
const ANALYST_MEMORY_FILE = resolve(STATE_DIR, 'analyst-memory.json');
const MEMORY_DIR = '/home/jarvis/.openclaw/workspace-burry/memory';
const SCOUT_MEMORY_MAX = 10;
const ANALYST_MEMORY_MAX = 30;

function loadTradingMemory() {
  try {
    const lessons = readFileSync(resolve(MEMORY_DIR, 'lessons.md'), 'utf8').trim();
    if (lessons && !lessons.startsWith('# (intentionally')) return lessons;
  } catch {}
  return '';
}

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

// --- Model configuration ---
const MODELS = {
  scout: { id: 'anthropic/claude-haiku-4-5', tokenKey: 'haiku', role: 'Scout' },
  analyst: { id: 'anthropic/claude-sonnet-4-6', tokenKey: 'sonnet', role: 'Trading Analyst' },
  challenger: { id: 'google/gemini-3.1-flash-lite-preview', tokenKey: 'gemini_flash_lite', role: "Devil's Advocate" },
  risk_manager: { id: 'anthropic/claude-haiku-4-5', tokenKey: 'haiku', role: 'Risk Manager' },
  news_analyst: { id: 'xai/grok-4-1-fast-non-reasoning', tokenKey: 'grok_fast', role: 'News Analyst' },
};

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

// --- Signal detail formatter (shared by Scout prompt) ---
function formatSignalDetail(signal) {
  let detail = signal.type;
  if (signal.type === 'whale_trade') {
    const side = signal.side === 'B' ? 'BUY' : signal.side === 'A' ? 'SELL' : signal.side || '?';
    const notional = signal.notional ? `$${(signal.notional / 1000).toFixed(0)}k` : '?';
    detail += ` — ${side} ${notional}`;
    if (signal.sz) detail += ` (size: ${signal.sz})`;
  } else if (signal.type === 'volume_spike') {
    detail += ` — ${signal.multiplier || '?'}x avg volume`;
  } else if (signal.type === 'rsi_extreme') {
    detail += ` — RSI ${signal.rsi?.toFixed(1) || '?'} (${signal.condition || '?'})`;
  } else {
    const dir = signal.direction || signal.condition || '';
    detail += dir ? ` — ${dir}` : '';
    if (signal.confidence != null) detail += ` (confidence: ${signal.confidence})`;
  }
  return detail;
}

// ============================================================
// Pre-filter: deterministic noise rejection (no LLM, no tokens)
// ============================================================
const PRE_FILTER_RSI_MIN = 15;
const PRE_FILTER_RSI_MAX = 85;
const PRE_FILTER_VOLUME_MIN = 3.0;
const PRE_FILTER_WHALE_MIN = 500_000; // $500k minimum for whale signals

export function preFilterSignal(signal) {
  // RSI not extreme enough
  if (signal.type === 'rsi_extreme') {
    const rsi = signal.rsi || 50;
    if (rsi > PRE_FILTER_RSI_MIN && rsi < PRE_FILTER_RSI_MAX) {
      return { pass: false, reason: `RSI ${rsi.toFixed(1)} not extreme enough` };
    }
  }
  // Volume spike too weak
  if (signal.type === 'volume_spike') {
    if ((signal.multiplier || 0) < PRE_FILTER_VOLUME_MIN) {
      return { pass: false, reason: `Volume ${signal.multiplier}x below ${PRE_FILTER_VOLUME_MIN}x threshold` };
    }
  }
  // Whale trade too small
  if (signal.type === 'whale_trade') {
    if ((signal.notional || 0) < PRE_FILTER_WHALE_MIN) {
      return { pass: false, reason: `Whale $${signal.notional} below $${PRE_FILTER_WHALE_MIN} threshold` };
    }
  }
  // No price data
  if (!signal.price && signal.type !== 'whale_trade') {
    return { pass: false, reason: 'No price data' };
  }
  return { pass: true };
}


export class SwarmOrchestrator {
  constructor(opts = {}) {
    this.tokenCounter = opts.tokenCounter || new TokenCounter();
    this.hl = opts.hlClient || new HLClient();
    this.llm = opts.llmClient || new LLMTaskClient();
    this.strategyManager = opts.strategyManager || new StrategyManager();
    this.dryRun = opts.dryRun || false;
    this._tradeLog = resolve(STATE_DIR, 'trade-log.jsonl');

    // Analyst buffer: scout-passed signals waiting for the analyst timer
    this._analystBuffer = [];
  }

  // ============================================================
  // Scout: evaluates a batch of signals for ONE coin (accumulated over 30s)
  // Returns { verdict: 'PASS'|'SKIP', summary, confidence, signals }
  // Does NOT assign strategies — that's the Analyst's job.
  // ============================================================

  async runScout(coin, signals, context = {}) {
    const marketContext = await this._gatherContext(coin, signals[0], context);
    const newsContext = this._getNewsContext(coin);

    const prompt = this._buildScoutPrompt(coin, signals, marketContext, newsContext);
    const response = await this._llmCall('scout', prompt, coin);
    const result = this._parseScoutResponse(response);

    // Save to scout memory
    this._saveScoutMemory(coin, {
      signalCount: signals.length,
      signalTypes: [...new Set(signals.map(s => s.type))].join(','),
      verdict: result.verdict,
      brief: result.brief,
      confidence: result.confidence,
      price: signals[0].price,
    });

    if (result.verdict === 'SKIP') {
      return { verdict: 'SKIP', brief: result.brief, confidence: result.confidence };
    }

    // PASS — push to analyst buffer
    const entry = {
      coin,
      signals,
      scout: result,
      newsContext,
      timestamp: new Date().toISOString(),
    };
    this._analystBuffer.push(entry);

    return { verdict: 'PASS', brief: result.brief, confidence: result.confidence };
  }

  // ============================================================
  // Analyst: runs on a 60s timer, reads ALL scout-passed signals,
  // loads ALL active strategies, decides per-signal × per-strategy.
  // ============================================================

  async runAnalyst() {
    // Drain the buffer
    const buffer = this._analystBuffer.splice(0);
    if (buffer.length === 0) return [];

    // Gather fresh context once
    const marketContext = await this._gatherContext(buffer[0].coin, buffer[0].signals[0], {});

    // Load ALL active strategies dynamically
    const strategies = this.strategyManager.getActive();
    if (strategies.length === 0) {
      console.log('[Swarm] Analyst: no active strategies, skipping');
      return [];
    }

    // Build prompt
    const prompt = this._buildAnalystPrompt(buffer, marketContext, strategies);
    const promptChars = prompt.length;
    console.log(`[Swarm] ANALYST: ${buffer.length} signals × ${strategies.length} strategies, prompt ~${promptChars} chars`);

    const response = await this._llmCall('analyst', prompt);
    const decisions = this._parseAnalystResponse(response, buffer);

    // Normalize coin names: analyst may return "CL-PERP" instead of "xyz:CL"
    for (const d of decisions) {
      if (d.coin) d.coin = this._normalizeCoin(d.coin);
    }

    // Save analyst memory
    this._saveAnalystMemory({
      signalCount: buffer.length,
      strategyCount: strategies.length,
      decisionsCount: decisions.length,
      trades: decisions.filter(d => d.action !== 'HOLD').length,
      coins: [...new Set(buffer.map(b => b.coin))],
    });

    // Process each decision through Challenger → Risk Manager → Execute
    const results = [];
    for (const decision of decisions) {
      // Analyst sometimes returns RISK_BLOCKED instead of HOLD — normalize.
      // If it has a valid side, convert to BUY/SELL and let hard gates decide.
      if (decision && (decision.action === 'RISK_BLOCKED' || decision.action === 'SKIP')) {
        if (decision.side && (decision.side === 'long' || decision.side === 'short')) {
          console.log(`[Swarm] Analyst returned ${decision.action} for ${decision.coin} — converting to ${decision.side === 'long' ? 'BUY' : 'SELL'} (hard gates will validate)`);
          decision.action = decision.side === 'long' ? 'BUY' : 'SELL';
        } else {
          decision.action = 'HOLD';
        }
      }

      if (!decision || decision.action === 'HOLD') {
        const coin = decision?.coin || '?';
        if (coin !== '?') this._updateScoutOutcome(coin, 'HOLD', decision?.reason || 'Analyst rejected');
        results.push({ action: 'HOLD', reason: decision?.reason || 'No opportunity', coin });
        continue;
      }

      // Find the strategy referenced by the analyst
      const strategy = strategies.find(s => s.name === decision.strategy || s.id === decision.strategy);
      const strategyTag = strategy ? { strategyId: strategy.id, strategyName: strategy.name } : {};
      const isDryRun = this.dryRun || (strategy && strategy.status === 'dry-run');

      // Hard gate: paused strategies cannot execute
      if (strategy && strategy.status === 'paused') {
        results.push({ action: 'RISK_BLOCKED', reason: `Strategy "${strategy.name}" is paused`, coin: decision.coin, ...strategyTag });
        continue;
      }

      // --- Hard risk gates (deterministic, run FIRST) ---
      const MIN_CONFIDENCE = 65;
      if ((decision.confidence || 0) < MIN_CONFIDENCE) {
        this._updateScoutOutcome(decision.coin, 'RISK_BLOCKED', `Confidence ${decision.confidence}% < ${MIN_CONFIDENCE}%`);
        results.push({ action: 'RISK_BLOCKED', reason: `Confidence too low: ${decision.confidence}%`, coin: decision.coin, ...strategyTag });
        continue;
      }

      const maxLev = strategy?.config?.leverage?.max || 5;
      const clampedLeverage = Math.min(decision.leverage || maxLev, maxLev);

      const equity = parseFloat(marketContext.account?.equity) || 0;
      const maxSizeUsd = equity * 0.20;
      const minNotional = 50;
      let tradeSize = decision.size || 0;

      // Clamp size to 20% equity (hard cap)
      if (equity > 0 && tradeSize > maxSizeUsd) {
        console.log(`[Swarm] Size cap: ${decision.coin} $${tradeSize} → $${maxSizeUsd.toFixed(0)} (20% of $${equity.toFixed(0)} equity)`);
        tradeSize = parseFloat(maxSizeUsd.toFixed(2));
      }

      // Minimum notional check
      if (tradeSize < minNotional) {
        this._updateScoutOutcome(decision.coin, 'RISK_BLOCKED', `Size $${tradeSize} < $${minNotional} minimum`);
        results.push({ action: 'RISK_BLOCKED', reason: `Size too small: $${tradeSize}`, coin: decision.coin, ...strategyTag });
        continue;
      }

      // Total margin check (max 80%)
      const marginUsed = parseFloat(marketContext.account?.marginUsed) || 0;
      const tradeMargin = tradeSize / clampedLeverage;
      if (equity > 0 && (marginUsed + tradeMargin) > equity * 0.80) {
        this._updateScoutOutcome(decision.coin, 'RISK_BLOCKED', `Margin would exceed 80%`);
        results.push({ action: 'RISK_BLOCKED', reason: `Margin limit: ${((marginUsed + tradeMargin) / equity * 100).toFixed(0)}% > 80%`, coin: decision.coin, ...strategyTag });
        continue;
      }

      // Strategy position limit
      if (strategy) {
        const maxPos = strategy.config?.max_positions || 3;
        const currentPos = this._countStrategyPositions(strategy.id);
        if (currentPos >= maxPos) {
          this._updateScoutOutcome(decision.coin, 'RISK_BLOCKED', `Strategy ${strategy.name} at ${currentPos}/${maxPos} positions`);
          results.push({ action: 'RISK_BLOCKED', reason: `${strategy.name} full: ${currentPos}/${maxPos}`, coin: decision.coin, ...strategyTag });
          continue;
        }
      }

      // Challenger — advisory only
      const challenge = await this._callChallenger(decision.coin, decision, marketContext);
      if (challenge.veto) {
        console.log(`[Swarm] Challenger concern on ${decision.coin}: ${challenge.reason} (advisory only)`);
        decision.challengerWarning = challenge.reason;
      }

      // Risk Manager — advisory only (logs note, does NOT block)
      const riskCheck = await this._callRiskManager(decision.coin, decision, marketContext, strategy);
      if (!riskCheck.approved) {
        console.log(`[Swarm] Risk manager concern on ${decision.coin}: ${riskCheck.reason} (advisory, not blocking)`);
      }
      // Use risk manager's adjusted size only if it's reasonable (within our deterministic bounds)
      if (riskCheck.adjustedSize && riskCheck.adjustedSize >= minNotional && riskCheck.adjustedSize <= maxSizeUsd) {
        tradeSize = riskCheck.adjustedSize;
      }

      const finalTrade = {
        action: decision.action,
        coin: decision.coin,
        side: decision.side,
        size: tradeSize,
        tp: decision.tp,
        sl: decision.sl,
        leverage: clampedLeverage,
        reason: decision.reason,
        confidence: decision.confidence,
        signalType: decision.signalType || 'unknown',
        challengerNote: challenge.note,
        riskNote: riskCheck.note,
        ...strategyTag,
      };

      if (isDryRun) {
        let entryPrice = null;
        try { entryPrice = await this.hl.getPrice(decision.coin); } catch {}
        this._logTrade({ ...finalTrade, dryRun: true, price: entryPrice, timestamp: new Date().toISOString() });
        this._updateScoutOutcome(decision.coin, 'TRADED', `${decision.side} $${decision.size} @${entryPrice} (dry-run)`);
        results.push({ ...finalTrade, executed: false, dryRun: true, price: entryPrice });
        continue;
      }

      const execResult = await this._executeTrade(finalTrade);
      if (strategy && execResult.executed && execResult.result) {
        const oid = execResult.result?.response?.data?.statuses?.[0]?.resting?.oid
          || execResult.result?.response?.data?.statuses?.[0]?.filled?.oid || null;
        this.strategyManager.attributeTrade(decision.coin, oid, strategy.id, finalTrade.action).catch(() => {});
      }
      this._updateScoutOutcome(decision.coin, execResult.executed ? 'TRADED' : 'EXEC_FAILED',
        execResult.executed ? `${decision.side} $${decision.size}` : execResult.error);
      results.push({ ...finalTrade, ...execResult });
    }

    return results;
  }

  /** How many signals are waiting for the analyst */
  get analystBufferSize() {
    return this._analystBuffer.length;
  }

  // ============================================================
  // Agent implementations
  // ============================================================

  async _callChallenger(coin, analysis, context) {
    const prompt = this._buildChallengerPrompt(coin, analysis, context);
    const response = await this._llmCall('challenger', prompt);
    return this._parseChallengerResponse(response);
  }

  async _callRiskManager(coin, analysis, context, strategy = null) {
    const prompt = this._buildRiskManagerPrompt(coin, analysis, context, strategy);
    const response = await this._llmCall('risk_manager', prompt);
    return this._parseRiskManagerResponse(response);
  }

  _getNewsContext(coin) {
    const cached = this._loadGeopolitics();
    if (!cached || Date.now() - new Date(cached.timestamp).getTime() > 3 * 3600_000) return null;
    return cached.analysis?.[coin] || null;
  }

  async pollGeopolitics(allCoins) {
    const coinList = allCoins.map(c => {
      const cls = getAssetClass(c);
      const name = c.replace('xyz:', '');
      return `${name} (${cls})`;
    }).join(', ');

    const prompt = `Rate these assets on current news/geopolitics: ${coinList}.

Per asset: BULLISH/BEARISH/NEUTRAL + confidence 0-100 + max 5-word factor.
Crypto: whale alerts, regulatory, protocol news. Commodities/equities: geopolitics, supply, macro.

COMPACT JSON only, no whitespace, factors max 5 words:
{"summary":"<10 words>","analysis":{"BTC":{"r":"B","c":70,"f":"ETF inflows strong"},"xyz:CL":{"r":"N","c":50,"f":"stable supply"},...}}
r=rating(B/E/N for BULLISH/BEARISH/NEUTRAL), c=confidence, f=factor`;

    const response = await this._llmCall('news_analyst', prompt);
    if (!response) {
      console.error('[Swarm] pollGeopolitics: news_analyst returned null/empty');
      return null;
    }

    try {
      const parsed = this._extractJSON(response);
      if (parsed) {
        if (parsed.analysis) {
          const RATING_MAP = { B: 'BULLISH', E: 'BEARISH', N: 'NEUTRAL' };
          for (const [key, val] of Object.entries(parsed.analysis)) {
            if (val.r && !val.rating) {
              val.rating = RATING_MAP[val.r] || val.r;
              val.confidence = val.c || 50;
              val.factor = val.f || '';
              delete val.r; delete val.c; delete val.f;
            }
          }
        }
        const result = { ...parsed, timestamp: new Date().toISOString() };
        writeFileSync(resolve(STATE_DIR, 'geopolitics.json'), JSON.stringify(result, null, 2));
        return result;
      }
      console.error(`[Swarm] pollGeopolitics: JSON parse returned null. Response (${response.length} chars): ${response.substring(0, 500)}`);
    } catch (e) {
      console.error(`[Swarm] pollGeopolitics: parse error: ${e.message}`);
    }
    return null;
  }

  // ============================================================
  // Prompt builders
  // ============================================================

  _buildScoutPrompt(coin, signals, context, newsContext) {
    const signalLines = signals.map(s => `- ${formatSignalDetail(s)} @ ${s.price || 'N/A'}`).join('\n');

    let prompt = `You are a signal quality scout for ${coin}. You received ${signals.length} signal(s) in the last 30 seconds.
Evaluate whether these signals together represent a real trading opportunity worth passing to the senior analyst.

SIGNALS:
${signalLines}`;

    if (context.existingPosition) {
      prompt += `\nEXISTING POSITION: ${JSON.stringify(context.existingPosition)}`;
    }
    prompt += `\nACCOUNT: equity=$${context.account?.equity} margin=${context.account?.marginRatio}`;

    if (newsContext) {
      prompt += `\nNEWS: ${typeof newsContext === 'object' ? JSON.stringify(newsContext) : newsContext}`;
    }

    const sentiment = loadSentiment();
    if (sentiment?.scores) {
      const cs = sentiment.scores.find(s => s.coin === coin.replace('xyz:', '').replace(/-PERP$/, ''));
      if (cs) prompt += `\nSENTIMENT: score=${cs.score}/100 trend=${cs.trend}`;
    }

    // Scout memory (last 10 decisions)
    const scoutMemory = this._loadScoutMemory(coin);
    if (scoutMemory.length > 0) {
      const recent = scoutMemory.slice(-5);
      const memLines = recent.map(m => {
        const age = Math.round((Date.now() - new Date(m.timestamp).getTime()) / 60000);
        let line = `${age}m ago: ${m.signalTypes || m.signalType} → ${m.verdict}`;
        if (m.verdict === 'PASS' && m.outcome) {
          line += ` → ${m.outcome}`;
          if (m.outcomeDetail) line += ` (${m.outcomeDetail})`;
        }
        return line;
      });
      prompt += `\nYOUR HISTORY (${scoutMemory.length} total): ${memLines.join(' | ')}`;
    }

    prompt += `

EVALUATE:
1. Are signals aligned/coherent or contradictory?
2. Is signal strength sufficient? (e.g. RSI extreme, large whale, strong breakout)
3. Are there concrete contradictions (news vs signal, existing position conflict)?

Do NOT evaluate strategies — that is the analyst's job.
Do NOT skip for margin or past skips — the analyst handles that.

Respond ONLY with JSON:
{"verdict":"PASS"|"SKIP","brief":"<max 15 words summary>","confidence":<0-100>,"direction":"BULLISH"|"BEARISH"|"MIXED"}`;
    return prompt;
  }

  _buildAnalystPrompt(buffer, marketContext, strategies) {
    const equity = parseFloat(marketContext.account?.equity) || 0;
    const maxSize = Math.round(equity * 0.20);
    let prompt = `You are a senior trading analyst. You have ${buffer.length} pre-screened signal group(s) from your Scout agents.
For EACH signal, evaluate it against ALL ${strategies.length} active strategies. You may open MULTIPLE trades from the same signal for different strategies.

ACCOUNT:
- Total equity: $${equity.toFixed(2)}
- Available for trading: $${marketContext.account?.available}
- Margin used: $${marketContext.account?.marginUsed || 'N/A'}
- Margin ratio: ${marketContext.account?.marginRatio}
- MAX trade size: $${maxSize} (20% of equity — system will cap anything higher)

OPEN POSITIONS (live): ${JSON.stringify(marketContext.positions || [])}
OPEN ORDERS (pending): ${JSON.stringify(marketContext.openOrders || [])}
DRY-RUN POSITIONS: ${JSON.stringify(this._getDryRunOpenPositions().map(p => ({ coin: p.coin, side: p.side, strategy: p.strategyName })))}
RECENT FILLS: ${JSON.stringify((marketContext.recentFills || []).slice(0, 5))}

STRATEGIES:`;

    for (const strat of strategies) {
      const c = strat.config;
      prompt += `\n\n--- ${strat.name} (${strat.status}) ---`;
      prompt += `\nWEIGHTS: technical=${c.weights?.technical || 0}%, sentiment=${c.weights?.sentiment || 0}%, whales=${c.weights?.whales || 0}%`;
      prompt += `\nHOLD TIME: ${c.hold_time?.min || '?'}-${c.hold_time?.max || '?'} ${c.hold_time?.unit || 'minutes'}`;
      prompt += `\nLEVERAGE: ${c.leverage?.base || 3}-${c.leverage?.max || 5}x`;
      prompt += `\nRISK: TP=${c.tp_pct || 2}% SL=${c.sl_pct || 2}% max_positions=${c.max_positions || 3}`;
      const stratPositions = this._countStrategyPositions(strat.id);
      prompt += `\nCurrent positions: ${stratPositions}/${c.max_positions || 3}`;
      if (strat.performance) {
        prompt += `\nPERFORMANCE: PnL=$${strat.performance.pnl || 0} WR=${strat.performance.winRate || 0}%`;
      }
    }

    prompt += `\n\nSIGNALS FROM SCOUTS:`;

    for (let i = 0; i < buffer.length; i++) {
      const entry = buffer[i];
      const signalDetails = entry.signals.map(s => formatSignalDetail(s)).join('; ');
      prompt += `\n\n--- Signal Group ${i + 1}: ${entry.coin} ---`;
      prompt += `\nSCOUT: ${entry.scout.verdict} (confidence: ${entry.scout.confidence}%) direction: ${entry.scout.direction || '?'} — ${entry.scout.brief}`;
      prompt += `\nSIGNALS: ${signalDetails}`;
      prompt += `\nPRICE: ${entry.signals[0].price || 'N/A'}`;
      if (entry.newsContext) prompt += `\nNEWS: ${JSON.stringify(entry.newsContext)}`;
    }

    // Shared context
    const sentiment = loadSentiment();
    if (sentiment?.macro?.length > 0) {
      prompt += `\n\nMACRO EVENTS: ${sentiment.macro.join('; ')}`;
    }
    const newsletter = loadNewsletter();
    if (newsletter?.digest) {
      prompt += `\nNEWSLETTER: ${typeof newsletter.digest === 'string' ? newsletter.digest.substring(0, 300) : ''}`;
    }
    const memory = loadTradingMemory();
    if (memory) {
      prompt += `\nTRADING MEMORY (informational, small samples NOT significant):\n${memory}`;
    }

    // Analyst memory (last 5 runs summary)
    const analystMemory = this._loadAnalystMemory();
    if (analystMemory.length > 0) {
      const recent = analystMemory.slice(-5);
      const memLines = recent.map(m => {
        const age = Math.round((Date.now() - new Date(m.timestamp).getTime()) / 60000);
        return `${age}m ago: ${m.signalCount} signals → ${m.trades} trades (${m.coins?.join(',')})`;
      });
      prompt += `\nYOUR RECENT RUNS: ${memLines.join(' | ')}`;
    }

    prompt += `

For EACH signal, decide for EACH strategy: BUY, SELL, or HOLD.
Same signal can produce multiple trades for different strategies (e.g. Scalping SHORT + Sentiment LONG).
Consider correlations within each strategy — avoid duplicate positions on same coin for same strategy.

HARD CONSTRAINTS (system-enforced, do NOT self-enforce — the system handles these automatically):
- Max trade size: $${Math.round((parseFloat(marketContext.account?.equity) || 0) * 0.20)} (20% of equity) — system will auto-clamp
- Leverage: auto-capped to strategy max
- Minimum confidence: 65% — auto-rejected below this
- Do NOT open a position if you already hold one on the same coin for the same strategy
- Do NOT open if strategy is at max_positions

CRITICAL — "size" field:
- "size" = USD DOLLAR AMOUNT, NOT coin quantity
- size=70 means SEVENTY US DOLLARS ($70), NOT 70 coins
- Example: size=70 for BTC means a $70 position (~0.0008 BTC), NOT 70 BTC
- Example: size=50 for ETH means a $50 position (~0.02 ETH), NOT 50 ETH
- The system enforces all risk limits — just output BUY/SELL/HOLD and the correct USD size

ONLY valid actions: BUY, SELL, HOLD. Do NOT return "RISK_BLOCKED" or any other action — if you want to reject, use HOLD.

Respond in JSON array:
[{"coin":"<coin>","strategy":"<strategy name>","action":"BUY|SELL|HOLD","side":"long|short","size":<usd_dollar_amount>,"leverage":<int>,"tp":<price>,"sl":<price>,"confidence":<0-100>,"reason":"<1 sentence>"},...]
Return one object per signal×strategy combination. Use HOLD for combinations you reject.`;
    return prompt;
  }

  _buildChallengerPrompt(coin, analysis, context) {
    return `You are an advisory risk reviewer for a proposed trade. Your observations will be noted but you do NOT have veto power — the senior analyst has already approved this trade.

PROPOSED TRADE: ${JSON.stringify(analysis)}
ACCOUNT: equity=$${context.account?.equity} available=$${context.account?.available}
POSITIONS: ${JSON.stringify(context.positions || [])}

Review this trade and flag any concerns:
- Overtrading risk (too many positions, correlated assets)
- Bad risk/reward ratio
- Signal quality issues
- Market conditions that contradict the trade

Respond ONLY with a single JSON object, no other text:
{"veto":true|false,"reason":"<max 10 words if concern>","note":"<max 10 words>"}
veto:true only for CRITICAL risks. Minor concerns → veto:false.`;
  }

  _buildRiskManagerPrompt(coin, analysis, context, strategy = null) {
    let prompt = `You are a risk manager. Validate this approved trade.

TRADE: ${JSON.stringify(analysis)}
ACCOUNT: equity=$${context.account?.equity} available=$${context.account?.available} marginUsed=$${context.account?.marginUsed}
POSITIONS: ${JSON.stringify(context.positions || [])}`;

    if (strategy) {
      prompt += `\nSTRATEGY: "${strategy.name}" status=${strategy.status} max_positions=${strategy.config?.max_positions || 3}`;
      const stratPositions = this._countStrategyPositions(strategy.id);
      prompt += `\nCurrent positions for this strategy: ${stratPositions}`;
      if (strategy.status === 'dry-run') {
        prompt += `\nNOTE: This strategy is in DRY-RUN mode. Validate normally.`;
      } else if (strategy.status === 'paused') {
        prompt += `\nCRITICAL: This strategy is PAUSED. Reject this trade.`;
      }
    }

    prompt += `

Check:
1. Position size vs available margin (max 20% of equity per position — hard-enforced by system)
2. Correlation with existing positions (flag but don't block cross-strategy)
3. Total margin usage (max 80%)
4. Minimum notional $50
5. Strategy position limit (if strategy provided)
6. Leverage must not exceed strategy max

If size is too large, set adjustedSize to a safer value (system will also clamp to 20% equity).

Respond ONLY with a single JSON object, no other text:
{"approved":true|false,"adjustedSize":<number|null>,"reason":"<max 10 words if rejected>","note":"<max 10 words>"}`;
    return prompt;
  }

  // ============================================================
  // Response parsers
  // ============================================================

  _parseScoutResponse(response) {
    try {
      const json = this._extractJSON(response);
      if (!json) return { verdict: 'SKIP', brief: 'Failed to parse scout response', confidence: 0 };
      const verdict = (json.verdict || 'SKIP').toUpperCase();
      if (verdict !== 'PASS' && verdict !== 'LONG' && verdict !== 'SHORT') {
        return { verdict: 'SKIP', brief: json.brief || 'No opportunity', confidence: json.confidence || 0 };
      }
      // Normalize LONG/SHORT → PASS (scout just says "worth looking at")
      return {
        verdict: 'PASS',
        brief: json.brief || '',
        confidence: json.confidence || 50,
        direction: json.direction || (verdict === 'LONG' ? 'BULLISH' : verdict === 'SHORT' ? 'BEARISH' : 'MIXED'),
      };
    } catch {
      return { verdict: 'SKIP', brief: 'Failed to parse scout response', confidence: 0 };
    }
  }

  _normalizeCoin(coin) {
    if (!coin) return coin;
    if (coin.startsWith('xyz:')) return coin;
    const bare = coin.replace(/-PERP$/, '').replace(/-SPOT$/, '');
    if (this.hl._xyzAssets?.some(a => a.name === bare)) return `xyz:${bare}`;
    return coin;
  }

  _parseAnalystResponse(response, buffer) {
    if (!response) return buffer.flatMap(b => [{ action: 'HOLD', coin: b.coin, reason: 'No analyst response' }]);
    try {
      const json = this._extractJSON(response);
      if (Array.isArray(json)) return json;
      if (json && json.action) return [json];
      if (json && Array.isArray(json.decisions)) return json.decisions;
      if (json && Array.isArray(json.trades)) return json.trades;
      if (json) {
        console.error(`[Swarm] Analyst parse: unexpected JSON shape:`, JSON.stringify(json)?.substring(0, 300));
      } else {
        console.error(`[Swarm] Analyst parse: no JSON found (${response.length} chars):`, response.substring(0, 400));
      }
    } catch (e) {
      console.error(`[Swarm] Analyst parse error:`, e.message);
    }
    return buffer.flatMap(b => [{ action: 'HOLD', coin: b.coin, reason: 'Failed to parse analyst response' }]);
  }

  _parseChallengerResponse(response) {
    try {
      const json = this._extractJSON(response);
      return { veto: json.veto || false, reason: json.reason || '', note: json.note || '' };
    } catch {
      return { veto: false, reason: '', note: 'Failed to parse challenger response' };
    }
  }

  _parseRiskManagerResponse(response) {
    try {
      const json = this._extractJSON(response);
      return { approved: json.approved !== false, adjustedSize: json.adjustedSize, reason: json.reason || '', note: json.note || '' };
    } catch {
      return { approved: false, reason: 'Failed to parse risk manager response' };
    }
  }

  _extractJSON(text) {
    if (!text) return null;
    try { return JSON.parse(text); } catch {}
    // Try code-fenced JSON (may be missing closing ```)
    const match = text.match(/```(?:json)?\s*([\s\S]*?)(?:```|$)/);
    if (match) try { return JSON.parse(match[1].trim()); } catch {}
    const arrayMatch = text.match(/\[[\s\S]*\]/);
    if (arrayMatch) try { return JSON.parse(arrayMatch[0]); } catch {}
    const braceMatch = text.match(/\{[\s\S]*\}/);
    if (braceMatch) try { return JSON.parse(braceMatch[0]); } catch {}
    // Truncated array: try salvaging complete objects from a partial array
    const partialArray = text.match(/\[\s*\{[\s\S]*/);
    if (partialArray) {
      let s = partialArray[0];
      // Find last complete object boundary
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

  async _llmCall(agentKey, prompt, coin = null) {
    const model = MODELS[agentKey];
    if (!model) throw new Error(`Unknown agent: ${agentKey}`);

    const TOKEN_LIMITS = { scout: 200, risk_manager: 150, challenger: 200, analyst: 4096, news_analyst: 4096 };
    const maxTokens = TOKEN_LIMITS[agentKey] || 512;

    try {
      const timeoutMs = (agentKey === 'news_analyst' || agentKey === 'analyst') ? 120_000 : undefined;
      const result = await this.llm.call(model.id, prompt, { maxTokens, timeoutMs });
      this.tokenCounter.record(model.tokenKey, result.inputTokens, result.outputTokens, result.cacheWriteTokens, result.cacheReadTokens);
      return result.content;
    } catch (e) {
      console.error(`[Swarm] ${model.role} (${model.id}) call failed:`, e.message);
      return null;
    }
  }

  // ============================================================
  // Trade execution
  // ============================================================

  async _executeTrade(trade) {
    const coin = trade.coin;
    const isBuy = trade.side === 'long';
    const notional = trade.size || 0;

    try {
      const price = await this.hl.getPrice(coin);
      if (!price || isNaN(price)) throw new Error(`Cannot get price for ${coin}`);
      const szDecimals = this.hl.getSzDecimals(coin);
      const sizeUnits = parseFloat((notional / price).toFixed(szDecimals));

      if (trade.leverage) {
        await this.hl.setLeverage(coin, trade.leverage, 'isolated').catch(e => {
          console.error(`[Swarm] setLeverage failed for ${coin} at ${trade.leverage}x:`, e.message);
        });
      }

      const result = isBuy
        ? await this.hl.marketBuy(coin, sizeUnits, 0.5)
        : await this.hl.marketSell(coin, sizeUnits, 0.5);

      this._logTrade({ ...trade, price, sizeUnits, result, timestamp: new Date().toISOString() });
      return { executed: true, price, sizeUnits, result };
    } catch (e) {
      console.error(`[Swarm] Execution failed for ${coin}:`, e.message);
      this._logTrade({ ...trade, error: e.message, timestamp: new Date().toISOString() });
      return { executed: false, error: e.message };
    }
  }

  _logTrade(entry) {
    try {
      if (!existsSync(STATE_DIR)) mkdirSync(STATE_DIR, { recursive: true });
      appendFileSync(this._tradeLog, JSON.stringify(entry) + '\n');
    } catch {}

    // Telegram notification for OPEN trades
    if (entry.action === 'BUY' || entry.action === 'SELL') {
      const tag = entry.dryRun ? '🧪 DRY-RUN' : '🟢 LIVE';
      const side = entry.side === 'long' ? 'LONG' : 'SHORT';
      const strat = entry.strategyName ? ` [${entry.strategyName}]` : '';
      const price = entry.price ? `$${parseFloat(entry.price).toFixed(2)}` : '?';
      const size = entry.size ? `$${entry.size}` : '?';
      const err = entry.error ? `\n⚠️ ${entry.error}` : '';
      sendTelegram(`${tag} <b>OPEN ${entry.coin}</b> ${side}${strat}\nSize: ${size} @ ${price}\nLev: ${entry.leverage || '?'}x | TP/SL: ${entry.tp || '?'}/${entry.sl || '?'}${err}`).catch(() => {});
    }
  }

  // ============================================================
  // Context gathering
  // ============================================================

  async _gatherContext(coin, signal, extra = {}) {
    try {
      const [balance, positions, orders, fills] = await Promise.all([
        this.hl.getBalance(),
        this.hl.getPositions(),
        this.hl.getOrders().catch(() => []),
        this.hl.getFills(5).catch(() => []),
      ]);
      return {
        signal,
        account: {
          equity: balance.overview,
          available: balance.availableForTrading,
          marginUsed: balance.perps.marginUsed,
          marginRatio: balance.perps.marginRatio,
        },
        positions: positions.map(p => ({
          coin: p.coin, side: p.side, size: p.size,
          pnl: p.pnl, leverage: p.leverage,
        })),
        openOrders: orders.map(o => ({
          coin: o.coin, side: o.side, size: o.size, price: o.price,
        })),
        recentFills: fills.map(f => ({
          coin: f.coin, side: f.side, size: f.size, price: f.price, pnl: f.pnl,
        })),
        existingPosition: positions.find(p => p.coin === coin) || null,
        ...extra,
      };
    } catch (e) {
      return { signal, error: e.message, ...extra };
    }
  }

  // ============================================================
  // Position counting helpers
  // ============================================================

  _countDryRunOpen(coin, strategyId = null) {
    return this._getDryRunOpenPositions().filter(p => {
      if (p.coin !== coin) return false;
      if (strategyId && p.strategyId !== strategyId) return false;
      return true;
    }).length;
  }

  _countDryRunOpenAll() {
    return this._getDryRunOpenPositions().length;
  }

  _countStrategyPositions(strategyId) {
    return this._getDryRunOpenPositions()
      .filter(p => p.strategyId === strategyId).length;
  }

  _getDryRunOpenPositions() {
    try {
      if (!existsSync(this._tradeLog)) return [];
      const lines = readFileSync(this._tradeLog, 'utf8').trim().split('\n').filter(Boolean);
      const openMap = new Map();
      const closedAfter = new Map();
      const MAX_TTL_MS = 4 * 3600_000;
      const now = Date.now();

      for (const line of lines) {
        try {
          const t = JSON.parse(line);
          if (!t.dryRun) continue;
          if (t.action === 'CLOSE') {
            closedAfter.set(t.coin, new Date(t.timestamp).getTime());
            continue;
          }
          if (t.action !== 'BUY' && t.action !== 'SELL') continue;
          openMap.set(`${t.coin}:${t.strategyId || 'default'}`, t);
        } catch {}
      }

      return [...openMap.values()].filter(t => {
        const age = now - new Date(t.timestamp).getTime();
        if (age >= MAX_TTL_MS) return false;
        const closeTs = closedAfter.get(t.coin);
        if (closeTs && closeTs > new Date(t.timestamp).getTime()) return false;
        return true;
      });
    } catch {
      return [];
    }
  }

  _loadGeopolitics() {
    const file = resolve(STATE_DIR, 'geopolitics.json');
    try {
      if (existsSync(file)) return JSON.parse(readFileSync(file, 'utf8'));
    } catch {}
    return null;
  }

  // ============================================================
  // Scout Memory (per-coin, last 10)
  // ============================================================

  _loadScoutMemory(coin) {
    const file = resolve(SCOUT_MEMORY_DIR, `${coin.replace(/[:/]/g, '_')}.json`);
    try {
      if (existsSync(file)) return JSON.parse(readFileSync(file, 'utf8'));
    } catch {}
    return [];
  }

  _saveScoutMemory(coin, entry) {
    try {
      if (!existsSync(SCOUT_MEMORY_DIR)) mkdirSync(SCOUT_MEMORY_DIR, { recursive: true });
      const file = resolve(SCOUT_MEMORY_DIR, `${coin.replace(/[:/]/g, '_')}.json`);
      const buf = this._loadScoutMemory(coin);
      buf.push({ timestamp: new Date().toISOString(), ...entry });
      writeFileSync(file, JSON.stringify(buf.slice(-SCOUT_MEMORY_MAX), null, 2));
    } catch (e) {
      console.error(`[Swarm] Scout memory save failed for ${coin}:`, e.message);
    }
  }

  _updateScoutOutcome(coin, outcome, detail) {
    try {
      const buf = this._loadScoutMemory(coin);
      for (let i = buf.length - 1; i >= 0; i--) {
        if (buf[i].verdict === 'PASS' && !buf[i].outcome) {
          buf[i].outcome = outcome;
          buf[i].outcomeDetail = detail;
          break;
        }
      }
      const file = resolve(SCOUT_MEMORY_DIR, `${coin.replace(/[:/]/g, '_')}.json`);
      writeFileSync(file, JSON.stringify(buf.slice(-SCOUT_MEMORY_MAX), null, 2));
    } catch {}
  }

  // ============================================================
  // Analyst Memory (global, last 30 runs)
  // ============================================================

  _loadAnalystMemory() {
    try {
      if (existsSync(ANALYST_MEMORY_FILE)) return JSON.parse(readFileSync(ANALYST_MEMORY_FILE, 'utf8'));
    } catch {}
    return [];
  }

  _saveAnalystMemory(entry) {
    try {
      const buf = this._loadAnalystMemory();
      buf.push({ timestamp: new Date().toISOString(), ...entry });
      writeFileSync(ANALYST_MEMORY_FILE, JSON.stringify(buf.slice(-ANALYST_MEMORY_MAX), null, 2));
    } catch (e) {
      console.error(`[Swarm] Analyst memory save failed:`, e.message);
    }
  }
}
