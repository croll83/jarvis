#!/usr/bin/env node
/**
 * Market Daemon — Always-on WebSocket listener with deterministic signal detection.
 * Calls LLM swarm only when a trading signal is detected.
 *
 * Usage:
 *   node daemon/market-daemon.mjs              # live mode
 *   DRY_RUN=true node daemon/market-daemon.mjs # dry-run (log signals, no trades)
 *
 * Env:
 *   HYPERLIQUID_ADDRESS — wallet address for user event subscriptions
 *   JARVIS_WALLET — private key (only needed for live trading)
 *   DRY_RUN — if "true", signals logged but swarm not called
 */

import { WSManager } from '../lib/ws-manager.mjs';
import { SignalRouter, SIGNAL_TYPES } from '../lib/signal-router.mjs';
import { SwarmOrchestrator, preFilterSignal } from './swarm.mjs';
import { TokenCounter } from './token-counter.mjs';
import { StrategyLearner } from './strategy-learner.mjs';
import { DailyReporter, sendTelegram } from './daily-report.mjs';
import { StrategyManager } from '../lib/strategy-manager.mjs';
import { HLClient } from '../lib/hl-client.mjs';
import { LLMTaskClient } from '../lib/llm-task-client.mjs';
import { startDashboardServer } from './dashboard-server.mjs';
import { readFileSync, writeFileSync, existsSync, mkdirSync, appendFileSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const STATE_DIR = resolve(__dirname, '..', 'state');
const LOG_FILE = resolve(STATE_DIR, 'daemon.log');

const DRY_RUN = process.env.DRY_RUN === 'true';
const ADDRESS = process.env.HYPERLIQUID_ADDRESS;

// --- Asset Universe — loaded from ontology (TradingWatchlist), hardcoded fallback ---
const FALLBACK_CRYPTO = ['BTC','ETH','HYPE','SOL','XRP','SUI','DOGE','BNB','PAXG','AVAX','LINK','AAVE','ENA','kPEPE','SEI'];
const FALLBACK_XYZ = ['xyz:CL','xyz:SILVER','xyz:BRENTOIL','xyz:GOLD','xyz:NATGAS','xyz:COPPER','xyz:TSLA','xyz:HOOD','xyz:NVDA','xyz:COIN','xyz:ORCL'];

async function loadWatchlistFromOntology() {
  const ONTOLOGY_URL = process.env.ONTOLOGY_URL || 'http://127.0.0.1:8100';
  const ONTOLOGY_TOKEN = process.env.ONTOLOGY_API_TOKEN || '';
  try {
    const resp = await fetch(`${ONTOLOGY_URL}/entities?type=TradingWatchlist`, {
      headers: {
        'Content-Type': 'application/json',
        'X-Speaker-Id': 'jarvis-agent',
        'Authorization': `Bearer ${ONTOLOGY_TOKEN}`,
      },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const entities = await resp.json();
    const all = Array.isArray(entities) ? entities : entities.entities || [];
    const wl = all.find(e => e.properties?.name === 'v2-monitored-assets');
    if (wl?.properties) {
      const crypto = wl.properties.crypto || FALLBACK_CRYPTO;
      const xyz = wl.properties.xyz || FALLBACK_XYZ;
      return { crypto, xyz };
    }
  } catch (e) {
    console.error(`[Daemon] Watchlist load from ontology failed: ${e.message}, using fallback`);
  }
  return { crypto: FALLBACK_CRYPTO, xyz: FALLBACK_XYZ };
}

// These are set in main() after ontology load
let CRYPTO_COINS = FALLBACK_CRYPTO;
let XYZ_COINS = FALLBACK_XYZ;
let WS_TRADE_COINS = [...FALLBACK_CRYPTO];
let ALL_COINS = [...FALLBACK_CRYPTO, ...FALLBACK_XYZ];

// Funding/OI poll interval (WebSocket doesn't stream these)
const FUNDING_POLL_INTERVAL = 60_000;   // 1 min
const XYZ_PRICE_POLL_INTERVAL = 5_000;  // 5 sec (XYZ not on WS allMids)

// Log rotation: keep last 5000 lines when log exceeds 10000 lines
const LOG_MAX_LINES = 10_000;
const LOG_KEEP_LINES = 5_000;
let _logLineCount = 0;
let _logRotating = false;

function log(msg) {
  const line = `[${new Date().toISOString()}] ${msg}`;
  try {
    if (!existsSync(STATE_DIR)) mkdirSync(STATE_DIR, { recursive: true });
    appendFileSync(LOG_FILE, line + '\n');
    _logLineCount++;

    // Rotate when exceeding max lines (async, non-blocking)
    if (_logLineCount >= LOG_MAX_LINES && !_logRotating) {
      _logRotating = true;
      setImmediate(() => {
        try {
          const content = readFileSync(LOG_FILE, 'utf8');
          const lines = content.split('\n');
          if (lines.length > LOG_MAX_LINES) {
            const kept = lines.slice(-LOG_KEEP_LINES).join('\n');
            writeFileSync(LOG_FILE, kept);
            _logLineCount = LOG_KEEP_LINES;
          }
        } catch {} finally { _logRotating = false; }
      });
    }
  } catch {}
}

async function main() {
  log(`Market Daemon starting (${DRY_RUN ? 'DRY-RUN' : 'LIVE'} mode)`);

  // Load asset universe from ontology (single source of truth)
  const watchlist = await loadWatchlistFromOntology();
  CRYPTO_COINS = watchlist.crypto;
  XYZ_COINS = watchlist.xyz;
  WS_TRADE_COINS = [...CRYPTO_COINS];
  ALL_COINS = [...CRYPTO_COINS, ...XYZ_COINS];
  log(`Monitoring ${ALL_COINS.length} assets: ${ALL_COINS.join(', ')}`);

  const ws = new WSManager();
  const router = new SignalRouter();
  const hl = new HLClient();

  // Start dashboard server early (needs hl for /api/account)
  startDashboardServer({ port: 18800, hlClient: hl, allCoins: ALL_COINS });

  const tokenCounter = new TokenCounter({
    onAlert: (alert) => {
      log(`TOKEN ALERT [${alert.level}]: ${alert.message}`);
      sendTelegram(`⚠️ <b>Token Alert [${alert.level}]</b>\n${alert.message}`).catch(() => {});
    },
  });

  // --- Strategy manager (load from ontology at boot) ---
  const strategyManager = new StrategyManager({ stateDir: STATE_DIR });
  try {
    const strategies = await strategyManager.load();
    log(`Loaded ${strategies.length} strategies: ${strategies.map(s => `${s.name} (${s.status})`).join(', ')}`);
  } catch (e) {
    log(`WARNING: Strategy load failed: ${e.message} — running without strategies`);
  }

  // --- LLM Task Client (stateless, direct embedded runner — no gateway WS needed) ---
  const llm = new LLMTaskClient({
    workspaceDir: resolve(__dirname, '..'),
    log: (msg) => log(msg),
  });
  try {
    await llm.init();
    log('LLM Task Client initialized (stateless mode)');
  } catch (e) {
    log(`FATAL: LLM Task Client init failed: ${e.message}`);
    process.exit(1);
  }
  const swarm = new SwarmOrchestrator({
    tokenCounter,
    hlClient: hl,
    llmClient: llm,
    strategyManager,
    dryRun: DRY_RUN,
  });

  // --- Strategy learner (self-learning loop) ---
  const learner = new StrategyLearner({
    hlClient: hl,
    strategyManager,
    intervalMs: 3600_000,    // run at most every 1h
    tradeThreshold: 5,       // or after 5 new trades
  });
  let tradeCount = 0;

  // ============================================================
  // Signal pipeline v2: Pre-filter → Scout (30s accumulate) → Analyst (60s timer)
  // ============================================================

  // --- Signal accumulation buffer (per-coin, 30s window) ---
  const SCOUT_FLUSH_INTERVAL = 30_000; // flush scouts every 30s
  const SCOUT_COOLDOWN_MS = 120_000;   // min 2min between scout calls per coin (was 60s — too many calls)
  const ANALYST_INTERVAL_MS = 90_000;  // analyst runs every 90s (was 60s — reduces lane pressure)
  const scoutLastCall = new Map();     // coin → timestamp
  const signalBuffer = new Map();      // coin → Signal[]

  function bufferSignal(signal) {
    const coin = signal.coin;
    const now = Date.now();

    // Hard cooldown: skip if recently scouted
    const lastCall = scoutLastCall.get(coin) || 0;
    if (now - lastCall < SCOUT_COOLDOWN_MS) return;

    // Pre-filter: reject obvious noise before accumulating
    const filter = preFilterSignal(signal);
    if (!filter.pass) return; // silently drop noise

    // Accumulate ALL signals for this coin (not just the "best" one)
    if (!signalBuffer.has(coin)) {
      signalBuffer.set(coin, []);
    }
    signalBuffer.get(coin).push(signal);
  }

  // --- Scout flush: every 30s, send accumulated signals per coin to Scout ---
  let scoutFlushing = false;
  setInterval(async () => {
    if (signalBuffer.size === 0 || scoutFlushing) return;
    scoutFlushing = true;
    const entries = [...signalBuffer.entries()];
    signalBuffer.clear();

    for (const [coin, signals] of entries) {
      scoutLastCall.set(coin, Date.now());
      try {
        const result = await swarm.runScout(coin, signals, { geopolitics: loadGeoContext(coin) });
        if (result.verdict === 'PASS') {
          log(`SCOUT: PASS on ${coin} (${signals.length} signals, ${result.confidence}%) — ${result.brief} (analyst buffer: ${swarm.analystBufferSize})`);
        } else {
          log(`SCOUT: SKIP on ${coin} (${signals.length} signals) — ${result.brief}`);
        }
      } catch (e) {
        log(`SCOUT ERROR on ${coin}: ${e.message}`);
      }
    }
    scoutFlushing = false;
  }, SCOUT_FLUSH_INTERVAL);

  // --- Analyst timer: every 60s, process all scout-passed signals ---
  let analystRunning = false;
  setInterval(async () => {
    if (analystRunning || swarm.analystBufferSize === 0) return;
    analystRunning = true;

    log(`ANALYST: processing ${swarm.analystBufferSize} scout-passed signal groups`);
    try {
      const results = await swarm.runAnalyst();
      for (const result of results) {
        const tag = result.strategyName ? `[${result.strategyName}] ` : '';
        log(`DECISION: ${tag}${result.action} on ${result.coin} — ${result.reason || ''} ${result.executed ? '(EXECUTED)' : ''}`);
        if (result.action !== 'HOLD' && result.action !== 'SKIP' && result.action !== 'RISK_BLOCKED') {
          tradeCount++;
        }
      }
      // Trigger learner
      if (learner.shouldRun(tradeCount)) {
        learner.run().then(r => {
          log(`LEARNER: ${r.overall?.status || r.status} | ${r.lessonsGenerated || 0} lessons generated`);
        }).catch(e => log(`LEARNER ERROR: ${e.message}`));
      }
    } catch (e) {
      log(`ANALYST ERROR: ${e.message}`);
    } finally {
      analystRunning = false;
    }
  }, ANALYST_INTERVAL_MS);

  // Load geopolitics context (cached file)
  function loadGeoContext(coin) {
    try {
      const geoFile = resolve(STATE_DIR, 'geopolitics.json');
      if (existsSync(geoFile)) {
        const geo = JSON.parse(readFileSync(geoFile, 'utf8'));
        return geo.analysis?.[coin] || null;
      }
    } catch {}
    return null;
  }

  router.onSignal((signal) => {
    const brief = signal.type === 'whale_trade'
      ? `$${(signal.notional / 1000).toFixed(0)}k ${signal.side === 'B' ? 'BUY' : 'SELL'}`
      : signal.type === 'volume_spike'
      ? `${signal.multiplier}x avg`
      : signal.type === 'rsi_extreme'
      ? `RSI ${signal.rsi?.toFixed(0)} ${signal.condition}`
      : signal.direction || '';
    log(`SIGNAL: ${signal.type} ${signal.coin} ${brief}`);
    logDryRunSignal(signal);
    bufferSignal(signal);
  });

  // --- Connect WebSocket ---
  ws.addEventListener('connected', () => log('WebSocket connected'));
  ws.addEventListener('disconnected', () => log('WebSocket disconnected'));
  ws.addEventListener('error', (e) => log(`WebSocket error: ${e.detail?.message}`));
  ws.addEventListener('fatal', () => {
    log('FATAL: WebSocket reconnection failed. Exiting.');
    process.exit(1);
  });

  await ws.connect();

  // --- Subscribe to channels ---

  // 1. allMids — all crypto perp prices in one subscription
  ws.onAllMids((data) => {
    if (data.mids) {
      router.updatePrices(data.mids);
      // Check dry-run TP/SL on every price update (throttled below)
      dryRunPriceCheck(data.mids);
    }
  });

  // Throttle TP/SL checks to avoid excessive file I/O and API calls (every 5s)
  let lastTPSLCheck = 0;
  let liveTPSLRunning = false;
  const TPSL_CHECK_INTERVAL = 5_000;
  const latestPrices = {};
  function dryRunPriceCheck(mids) {
    Object.assign(latestPrices, mids);
    const now = Date.now();
    if (now - lastTPSLCheck < TPSL_CHECK_INTERVAL) return;
    lastTPSLCheck = now;

    // Dry-run TP/SL (sync, fast)
    try { checkDryRunTPSL(latestPrices, strategyManager, null); } catch (e) { log(`TP/SL check error: ${e.message}`); }

    // Live TP/SL (async, needs HL API calls)
    if (!DRY_RUN && !liveTPSLRunning) {
      liveTPSLRunning = true;
      checkLiveTPSL(latestPrices, hl, strategyManager)
        .catch(e => log(`Live TP/SL error: ${e.message}`))
        .finally(() => { liveTPSLRunning = false; });
    }
  }

  // 2. Trades per crypto coin (for whale detection)
  for (const coin of WS_TRADE_COINS) {
    ws.onTrades(coin, (trades) => {
      if (Array.isArray(trades)) router.updateTrades(coin, trades);
    });
  }

  // 3. Candles per crypto coin (5m for indicator computation)
  for (const coin of CRYPTO_COINS) {
    ws.onCandle(coin, '5m', (candle) => {
      if (candle) router.updateCandle(coin, candle);
    });
  }

  // 4. User events (position changes, fills)
  if (ADDRESS) {
    ws.onUserFills(ADDRESS, (fills) => {
      if (Array.isArray(fills)) {
        for (const f of fills) {
          log(`FILL: ${f.dir || f.side} ${f.coin} ${f.sz}@${f.px} PnL=${f.closedPnl || 0}`);
        }
      } else if (fills?.fills) {
        for (const f of fills.fills) {
          log(`FILL: ${f.dir || f.side} ${f.coin} ${f.sz}@${f.px} PnL=${f.closedPnl || 0}`);
        }
      }
    });
    ws.onOrderUpdates(ADDRESS, (updates) => {
      if (Array.isArray(updates)) {
        for (const u of updates) {
          log(`ORDER: ${u.order?.side || '?'} ${u.order?.coin || '?'} status=${u.status || '?'}`);
        }
      }
    });
  }

  log(`Subscribed: ${ws.subscriptionCount} WebSocket channels`);

  // --- Poll XYZ prices (not available on WebSocket) ---
  async function pollXyzPrices() {
    try {
      const mids = await xyzApiCall({ type: 'allMids', dex: 'xyz' });
      router.updatePrices(mids);
      dryRunPriceCheck(mids);
    } catch (e) {
      log(`XYZ price poll error: ${e.message}`);
    }
  }

  // --- Poll funding rates and OI ---
  async function pollFundingAndOI() {
    try {
      // Perp
      const [perpMeta, perpCtxs] = await hl.getMetaAndCtxs('perp');
      for (const coin of CRYPTO_COINS) {
        const idx = perpMeta.universe.findIndex(u => u.name === coin);
        if (idx >= 0) {
          router.updateFunding(coin, parseFloat(perpCtxs[idx].funding));
          router.updateOI(coin, parseFloat(perpCtxs[idx].openInterest));
        }
      }

      // XYZ
      const [xyzMeta, xyzCtxs] = await hl.getMetaAndCtxs('xyz');
      for (const coin of XYZ_COINS) {
        const idx = xyzMeta.universe.findIndex(u => u.name === coin);
        if (idx >= 0) {
          router.updateFunding(coin, parseFloat(xyzCtxs[idx].funding));
          router.updateOI(coin, parseFloat(xyzCtxs[idx].openInterest));
        }
      }
    } catch (e) {
      log(`Funding/OI poll error: ${e.message}`);
    }
  }

  // --- Poll XYZ candles (for indicator computation) ---
  // 1m candles polled every 60s — fast enough for commodities/forex intraday signals
  async function pollXyzCandles(bulk = false) {
    for (const coin of XYZ_COINS) {
      try {
        const candles = await hl.getCandles(coin, '1m', 60);
        if (bulk) {
          router.bulkLoadCandles(coin, candles.map(c => ({ t: c.time, o: c.open, h: c.high, l: c.low, c: c.close, v: c.volume })));
        } else {
          for (const c of candles) {
            router.updateCandle(coin, { t: c.time, o: c.open, h: c.high, l: c.low, c: c.close, v: c.volume });
          }
        }
      } catch (e) {
        // Some XYZ coins may not have enough data
      }
    }
  }

  // --- Poll crypto candles (supplements WS 5m feed) ---
  // WS only sends live candle updates — without historical bulk load, indicators
  // need 30×5min = 2.5h to warm up. This polls 60 historical 5m candles at boot
  // and refreshes every 5min to keep indicators current.
  async function pollCryptoCandles(bulk = false) {
    for (const coin of CRYPTO_COINS) {
      try {
        const candles = await hl.getCandles(coin, '5m', 60);
        if (bulk) {
          router.bulkLoadCandles(coin, candles.map(c => ({ t: c.time, o: c.open, h: c.high, l: c.low, c: c.close, v: c.volume })));
        } else {
          for (const c of candles) {
            router.updateCandle(coin, { t: c.time, o: c.open, h: c.high, l: c.low, c: c.close, v: c.volume });
          }
        }
      } catch (e) {
        // Some coins may not have enough candle history
      }
    }
  }

  // Initial data load (bulk = true suppresses signals)
  await Promise.all([pollXyzPrices(), pollFundingAndOI(), pollXyzCandles(true), pollCryptoCandles(true)]);
  log('Initial data loaded (crypto + xyz candles bulk-loaded)');

  // Start polling timers
  setInterval(pollXyzPrices, XYZ_PRICE_POLL_INTERVAL);
  setInterval(pollFundingAndOI, FUNDING_POLL_INTERVAL);
  setInterval(pollXyzCandles, 60_000); // every 1 min for XYZ 1m candles
  setInterval(pollCryptoCandles, 5 * 60_000); // every 5 min for crypto 5m candles

  // Status report every 5 minutes
  setInterval(() => {
    const summary = tokenCounter.getTodaySummary();
    log(`STATUS: ${ALL_COINS.length} coins | WS: ${ws.connected ? 'OK' : 'DISCONNECTED'} | LLM: stateless | Subs: ${ws.subscriptionCount} | Tokens: $${summary.total_cost_usd.toFixed(2)} (${summary.budget_used_pct}% budget)`);
  }, 5 * 60_000);

  // Strategy learner periodic run (every 1h)
  setInterval(() => {
    if (learner.shouldRun(tradeCount)) {
      learner.run().then(r => {
        log(`LEARNER (periodic): ${r.overall?.status || r.status} | ${r.lessonsGenerated || 0} lessons`);
      }).catch(e => log(`LEARNER ERROR: ${e.message}`));
    }
  }, 3600_000);

  // Geopolitics poll (every 2h) — single Grok call for ALL monitored assets
  async function pollGeopolitics() {
    try {
      const result = await swarm.pollGeopolitics(ALL_COINS);
      if (result) {
        log(`GEOPOLITICS: updated ${Object.keys(result.analysis || {}).length} assets`);
      }
    } catch (e) {
      log(`GEOPOLITICS ERROR: ${e.message}`);
    }
  }
  // Non-blocking initial load — don't let a slow/failed LLM call block daemon startup
  pollGeopolitics().catch(e => log(`GEOPOLITICS BOOT ERROR: ${e.message}`));
  setInterval(pollGeopolitics, 2 * 3600_000); // Every 2 hours

  // Reload strategies from ontology (every 30 min)
  setInterval(() => {
    strategyManager.load().then(s => {
      log(`Strategies reloaded: ${s.length} (${s.filter(x => x.status === 'active').length} active, ${s.filter(x => x.status === 'dry-run').length} dry-run)`);
    }).catch(e => log(`Strategy reload failed: ${e.message}`));
  }, 30 * 60_000);

  // Daily report at 09:00 and 21:00 CET
  const dailyReporter = new DailyReporter({ hlClient: hl });
  let lastReportHour = -1;
  setInterval(() => {
    const now = new Date();
    // CET = UTC+1, CEST = UTC+2 — use Europe/Rome
    const cetHour = parseInt(now.toLocaleString('en-US', { hour: 'numeric', hour12: false, timeZone: 'Europe/Rome' }));
    const cetMinute = parseInt(now.toLocaleString('en-US', { minute: 'numeric', timeZone: 'Europe/Rome' }));

    // Trigger at :00-:04 of target hours (9 and 21), avoid double-send
    if ((cetHour === 9 || cetHour === 21) && cetMinute < 5 && lastReportHour !== cetHour) {
      lastReportHour = cetHour;
      dailyReporter.sendReport(1).then(r => {
        log(`DAILY REPORT: ${r.sent ? 'sent' : 'FAILED'} — ${r.trades} trades`);
      }).catch(e => log(`DAILY REPORT ERROR: ${e.message}`));
    }
    // Reset guard after the window passes
    if (cetMinute >= 5 && lastReportHour === cetHour) {
      lastReportHour = -1;
    }
  }, 60_000); // Check every minute

  // Graceful shutdown
  for (const sig of ['SIGINT', 'SIGTERM']) {
    process.on(sig, () => {
      log(`Received ${sig}, shutting down...`);
      ws.close();
      tokenCounter.destroy();
      process.exit(0);
    });
  }

  log('Daemon running. Press Ctrl+C to stop.');
}

function logDryRunSignal(signal) {
  const file = resolve(STATE_DIR, 'dry-run-signals.jsonl');
  if (!existsSync(STATE_DIR)) mkdirSync(STATE_DIR, { recursive: true });
  appendFileSync(file, JSON.stringify(signal) + '\n');
}

// --- Dry-run position monitor ---
// Mirrors live mode behavior:
//   1. Close on TP/SL hit
//   2. Close at market when hold_time.max exceeded AND PnL > fees (no point closing at a loss to fees)
//   3. Force-close oldest position when margin needed for a stronger signal
// Positions NEVER "expire" silently — every position gets a CLOSE entry with real PnL.
const TRADE_LOG = resolve(STATE_DIR, 'trade-log.jsonl');

// Hyperliquid fee schedule (taker): 0.035% for most tiers
const HL_TAKER_FEE_PCT = 0.035;

function getDryRunOpenPositions() {
  if (!existsSync(TRADE_LOG)) return [];
  let lines;
  try {
    lines = readFileSync(TRADE_LOG, 'utf8').trim().split('\n').filter(Boolean);
  } catch { return []; }

  const openMap = new Map();
  const closeTimestamps = new Map(); // coin → latest close timestamp

  for (const line of lines) {
    try {
      const t = JSON.parse(line);
      if (!t.dryRun) continue;
      if (t.action === 'CLOSE') {
        const prev = closeTimestamps.get(t.coin);
        if (!prev || new Date(t.timestamp) > new Date(prev)) {
          closeTimestamps.set(t.coin, t.timestamp);
        }
        continue;
      }
      if (t.action !== 'BUY' && t.action !== 'SELL') continue;
      openMap.set(t.coin, t);
    } catch {}
  }

  // Filter: only positions opened AFTER the latest close for that coin
  const openTrades = [];
  for (const [coin, trade] of openMap) {
    const lastClose = closeTimestamps.get(coin);
    if (lastClose && new Date(lastClose) > new Date(trade.timestamp)) continue;
    openTrades.push(trade);
  }
  return openTrades;
}

function closeDryRunPosition(trade, currentPrice, closeReason) {
  const entryPrice = parseFloat(trade.price);
  const isLong = trade.side === 'long';
  const sizeUsd = trade.size || 0;

  // PnL at current market price (not TP/SL price — market close)
  let closePrice = currentPrice;
  let pnl;

  if (closeReason === 'tp_hit') {
    closePrice = trade.tp;
    pnl = isLong
      ? ((trade.tp - entryPrice) / entryPrice) * sizeUsd
      : ((entryPrice - trade.tp) / entryPrice) * sizeUsd;
  } else if (closeReason === 'sl_hit') {
    closePrice = trade.sl;
    pnl = isLong
      ? ((trade.sl - entryPrice) / entryPrice) * sizeUsd
      : ((entryPrice - trade.sl) / entryPrice) * sizeUsd;
  } else {
    // Market close (hold_time exceeded, margin needed, etc.)
    pnl = isLong
      ? ((currentPrice - entryPrice) / entryPrice) * sizeUsd
      : ((entryPrice - currentPrice) / entryPrice) * sizeUsd;
  }

  // Subtract round-trip fees (open + close)
  const fees = sizeUsd * (HL_TAKER_FEE_PCT / 100) * 2;
  pnl -= fees;

  const closeEntry = {
    action: 'CLOSE',
    coin: trade.coin,
    side: trade.side,
    size: sizeUsd,
    entryPrice,
    closePrice: parseFloat(closePrice),
    pnl: parseFloat(pnl.toFixed(2)),
    fees: parseFloat(fees.toFixed(2)),
    closeReason,
    leverage: trade.leverage,
    strategyId: trade.strategyId,
    strategyName: trade.strategyName,
    openTimestamp: trade.timestamp,
    dryRun: true,
    timestamp: new Date().toISOString(),
  };
  appendFileSync(TRADE_LOG, JSON.stringify(closeEntry) + '\n');
  log(`DRY-RUN CLOSE: ${trade.coin} ${trade.side} ${closeReason} — PnL ${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)} (entry $${entryPrice.toFixed(2)} → $${closePrice.toFixed(2)}, fees $${fees.toFixed(2)})`);
  return closeEntry;
}

function checkDryRunTPSL(prices, strategyManager, scoutBuffer) {
  const openTrades = getDryRunOpenPositions();
  if (openTrades.length === 0) return;

  const now = Date.now();

  for (const trade of openTrades) {
    const coin = trade.coin;
    const currentPrice = parseFloat(prices[coin] || prices[coin + '-PERP']);
    if (!currentPrice || !trade.price) continue;

    const entryPrice = parseFloat(trade.price);
    const isLong = trade.side === 'long';
    const ageMs = now - new Date(trade.timestamp).getTime();

    // 1. TP/SL check (always, regardless of hold time)
    if (trade.tp && trade.sl) {
      if (isLong) {
        if (currentPrice >= trade.tp) { closeDryRunPosition(trade, currentPrice, 'tp_hit'); continue; }
        if (currentPrice <= trade.sl) { closeDryRunPosition(trade, currentPrice, 'sl_hit'); continue; }
      } else {
        if (currentPrice <= trade.tp) { closeDryRunPosition(trade, currentPrice, 'tp_hit'); continue; }
        if (currentPrice >= trade.sl) { closeDryRunPosition(trade, currentPrice, 'sl_hit'); continue; }
      }
    }

    // 2. Hold time check — close at market if exceeded and profitable after fees
    const strategy = trade.strategyId ? strategyManager?.get(trade.strategyId) : null;
    const holdTimeMax = strategy?.config?.hold_time?.max || 240; // default 4h in minutes
    const holdTimeUnit = strategy?.config?.hold_time?.unit || 'minutes';
    const maxMs = holdTimeMax * (holdTimeUnit === 'hours' ? 3600_000 : 60_000);

    if (ageMs >= maxMs) {
      const sizeUsd = trade.size || 0;
      const rawPnl = isLong
        ? ((currentPrice - entryPrice) / entryPrice) * sizeUsd
        : ((entryPrice - currentPrice) / entryPrice) * sizeUsd;
      const fees = sizeUsd * (HL_TAKER_FEE_PCT / 100) * 2;

      if (rawPnl > fees) {
        // Profitable after fees — close and take profit
        closeDryRunPosition(trade, currentPrice, 'hold_time_profit');
      } else if (rawPnl < -fees * 3) {
        // Deep loss beyond 3x fees — cut loss, don't hold a loser forever
        closeDryRunPosition(trade, currentPrice, 'hold_time_stoploss');
      }
      // Otherwise: hold time exceeded but PnL near zero / small loss — keep holding
      // until TP/SL hit or margin is needed. Like a real trader would.
    }
  }

  // 3. Margin pressure: if buffer has pending signals and we're at max positions,
  //    close the weakest position to free margin (checked per-strategy)
  if (scoutBuffer && scoutBuffer.length > 0 && strategyManager) {
    const strategies = strategyManager.getActive();
    for (const strat of strategies) {
      const maxPos = strat.config?.max_positions || 3;
      const stratPositions = openTrades.filter(t => t.strategyId === strat.id);
      if (stratPositions.length < maxPos) continue;

      // At max positions — find weakest by current PnL
      let weakest = null;
      let worstPnl = Infinity;
      for (const trade of stratPositions) {
        const cp = parseFloat(prices[trade.coin] || prices[trade.coin + '-PERP']);
        if (!cp || !trade.price) continue;
        const ep = parseFloat(trade.price);
        const isL = trade.side === 'long';
        const pnl = isL ? ((cp - ep) / ep) * trade.size : ((ep - cp) / ep) * trade.size;
        if (pnl < worstPnl) { worstPnl = pnl; weakest = trade; }
      }

      // Only force-close if there's a buffered signal for this strategy
      const hasPendingForStrat = scoutBuffer.some(b =>
        b.scout?.strategy?.id === strat.id ||
        strategyManager.canTrade(strat, b.signal?.coin)
      );

      if (weakest && hasPendingForStrat) {
        const cp = parseFloat(prices[weakest.coin] || prices[weakest.coin + '-PERP']);
        if (cp) {
          closeDryRunPosition(weakest, cp, 'margin_realloc');
          log(`DRY-RUN MARGIN REALLOC: closed weakest ${weakest.coin} (PnL $${worstPnl.toFixed(2)}) to free margin for ${strat.name}`);
        }
      }
    }
  }
}

// ============================================================
// Live position monitor — TP/SL + hold time for REAL positions
// ============================================================
// Mirrors dry-run logic but executes real market closes via HL API.
// Uses trade-log.jsonl (dryRun=false) as source of truth for TP/SL levels,
// cross-referenced with actual HL positions to avoid phantom closes.

// Returns ALL live open entries grouped by coin (multiple strategies per coin possible).
// Key: coin → [trade1 (stratA), trade2 (stratB), ...]
function getLiveOpenPositionsByCoin() {
  if (!existsSync(TRADE_LOG)) return new Map();
  let lines;
  try {
    lines = readFileSync(TRADE_LOG, 'utf8').trim().split('\n').filter(Boolean);
  } catch { return new Map(); }

  // Track close timestamps per coin+strategy
  const closeTimestamps = new Map(); // "coin:stratId" → timestamp
  const allOpens = []; // all BUY/SELL entries (not deduped)

  for (const line of lines) {
    try {
      const t = JSON.parse(line);
      if (t.dryRun) continue;
      if (t.action === 'CLOSE') {
        const key = `${t.coin}:${t.strategyId || 'default'}`;
        const prev = closeTimestamps.get(key);
        if (!prev || new Date(t.timestamp) > new Date(prev)) {
          closeTimestamps.set(key, t.timestamp);
        }
        continue;
      }
      if (t.action !== 'BUY' && t.action !== 'SELL') continue;
      allOpens.push(t);
    } catch {}
  }

  // Filter to entries not yet closed (per coin+strategy), dedup to latest per key
  const byKey = new Map(); // "coin:stratId" → trade
  for (const t of allOpens) {
    const key = `${t.coin}:${t.strategyId || 'default'}`;
    const lastClose = closeTimestamps.get(key);
    if (lastClose && new Date(lastClose) > new Date(t.timestamp)) continue;
    byKey.set(key, t); // latest open per coin+strategy wins
  }

  // Group by coin
  const byCoin = new Map();
  for (const trade of byKey.values()) {
    if (!byCoin.has(trade.coin)) byCoin.set(trade.coin, []);
    byCoin.get(trade.coin).push(trade);
  }
  return byCoin;
}

// Log a CLOSE entry for a single trade (per-strategy). No HL execution — caller does that.
function logCloseEntry(trade, closePrice, closeReason, execResult = null) {
  const entryPrice = parseFloat(trade.price);
  const isLong = trade.side === 'long';
  const sizeUsd = trade.size || 0;

  let pnl = isLong
    ? ((closePrice - entryPrice) / entryPrice) * sizeUsd
    : ((entryPrice - closePrice) / entryPrice) * sizeUsd;
  const fees = sizeUsd * (HL_TAKER_FEE_PCT / 100) * 2;
  pnl -= fees;

  const closeEntry = {
    action: 'CLOSE',
    coin: trade.coin,
    side: trade.side,
    size: sizeUsd,
    entryPrice,
    closePrice: parseFloat(closePrice),
    pnl: parseFloat(pnl.toFixed(2)),
    fees: parseFloat(fees.toFixed(2)),
    closeReason,
    leverage: trade.leverage,
    strategyId: trade.strategyId,
    strategyName: trade.strategyName,
    openTimestamp: trade.timestamp,
    dryRun: false,
    timestamp: new Date().toISOString(),
    execResult: execResult || undefined,
  };
  appendFileSync(TRADE_LOG, JSON.stringify(closeEntry) + '\n');
  return { closeEntry, pnl };
}

// Close a coin on HL, then log CLOSE for ALL strategy entries referencing that coin.
async function closeLivePositionAll(coin, trades, currentPrice, closeReason, hlClient) {
  let execResult = null;
  let closePrice = currentPrice;

  // Execute real close on Hyperliquid (once per coin, not per strategy)
  try {
    execResult = await hlClient.closePosition(coin, 1.0);
    log(`LIVE CLOSE EXEC: ${coin} ${closeReason} — order sent`);
  } catch (e) {
    log(`LIVE CLOSE FAILED: ${coin} ${closeReason} — ${e.message}`);
    return null;
  }

  // Use actual fill price if available
  const fillPx = execResult?.response?.data?.statuses?.[0]?.filled?.avgPx;
  if (fillPx) closePrice = parseFloat(fillPx);
  else if (closeReason === 'tp_hit') closePrice = trades[0]?.tp || currentPrice;
  else if (closeReason === 'sl_hit') closePrice = trades[0]?.sl || currentPrice;

  // Log CLOSE for EACH strategy entry
  let totalPnl = 0;
  for (const trade of trades) {
    const { pnl } = logCloseEntry(trade, closePrice, closeReason, execResult);
    totalPnl += pnl;
    log(`LIVE CLOSE: ${pnl >= 0 ? '✅' : '🔴'} ${coin} ${trade.side} [${trade.strategyName || '?'}] ${closeReason} — PnL ${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}`);
  }

  // Single Telegram notification per coin
  const emoji = totalPnl >= 0 ? '✅' : '🔴';
  const strats = trades.map(t => t.strategyName || '?').join(', ');
  sendTelegram(`${emoji} <b>CLOSE ${coin}</b> ${trades[0]?.side} (${closeReason})\nPnL: ${totalPnl >= 0 ? '+' : ''}$${totalPnl.toFixed(2)}\nStrategies: ${strats}`).catch(() => {});

  return { coin, closeReason, totalPnl, strategies: trades.length };
}

async function checkLiveTPSL(prices, hlClient, strategyManager) {
  const byCoin = getLiveOpenPositionsByCoin();
  if (byCoin.size === 0) return;

  // Cross-reference with actual HL positions
  let hlPositions;
  try {
    hlPositions = await hlClient.getPositions();
  } catch (e) {
    log(`Live TP/SL: failed to get HL positions: ${e.message}`);
    return;
  }
  // Build a set that includes both raw HL names (e.g. "ENA-PERP") and bare names ("ENA")
  // so trades logged without the -PERP suffix still match their HL positions.
  const hlPositionCoins = new Set();
  for (const p of hlPositions) {
    hlPositionCoins.add(p.coin);
    hlPositionCoins.add(p.coin.replace(/-PERP$/, ''));
  }
  const now = Date.now();

  for (const [coin, trades] of byCoin) {
    // Position closed externally — log CLOSE for ALL strategy entries
    if (!hlPositionCoins.has(coin)) {
      const currentPrice = parseFloat(prices[coin] || prices[coin + '-PERP']);
      if (currentPrice) {
        for (const trade of trades) {
          logCloseEntry(trade, currentPrice, 'external_close');
        }
        log(`LIVE CLOSE (external): ${coin} — not on HL, closed ${trades.length} strategy entries`);
      }
      continue;
    }

    // Use the first trade's data for TP/SL check (all trades for same coin share the same HL position)
    const primaryTrade = trades[0];
    const currentPrice = parseFloat(prices[coin] || prices[coin + '-PERP']);
    if (!currentPrice || !primaryTrade.price) continue;

    const entryPrice = parseFloat(primaryTrade.price);
    const isLong = primaryTrade.side === 'long';

    // 1. TP/SL check (uses primary trade's TP/SL levels)
    if (primaryTrade.tp && primaryTrade.sl) {
      let tpslHit = null;
      if (isLong) {
        if (currentPrice >= primaryTrade.tp) tpslHit = 'tp_hit';
        else if (currentPrice <= primaryTrade.sl) tpslHit = 'sl_hit';
      } else {
        if (currentPrice <= primaryTrade.tp) tpslHit = 'tp_hit';
        else if (currentPrice >= primaryTrade.sl) tpslHit = 'sl_hit';
      }
      if (tpslHit) {
        await closeLivePositionAll(coin, trades, currentPrice, tpslHit, hlClient);
        continue;
      }
    }

    // 2. Hold time check (use oldest trade's age — if any strategy says close, close all)
    const oldestAge = Math.max(...trades.map(t => now - new Date(t.timestamp).getTime()));
    const strategy = primaryTrade.strategyId ? strategyManager?.get(primaryTrade.strategyId) : null;
    const holdTimeMax = strategy?.config?.hold_time?.max || 240;
    const holdTimeUnit = strategy?.config?.hold_time?.unit || 'minutes';
    const maxMs = holdTimeMax * (holdTimeUnit === 'hours' ? 3600_000 : 60_000);

    if (oldestAge >= maxMs) {
      const sizeUsd = primaryTrade.size || 0;
      const rawPnl = isLong
        ? ((currentPrice - entryPrice) / entryPrice) * sizeUsd
        : ((entryPrice - currentPrice) / entryPrice) * sizeUsd;
      const fees = sizeUsd * (HL_TAKER_FEE_PCT / 100) * 2;

      if (rawPnl > fees) {
        await closeLivePositionAll(coin, trades, currentPrice, 'hold_time_profit', hlClient);
      } else if (rawPnl < -fees * 3) {
        await closeLivePositionAll(coin, trades, currentPrice, 'hold_time_stoploss', hlClient);
      }
    }
  }
}

// XYZ API helper (duplicated from hl-client for daemon use without SDK init)
async function xyzApiCall(body) {
  const resp = await fetch('https://api.hyperliquid.xyz/info', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`XYZ API error: ${resp.status}`);
  return resp.json();
}

main().catch(e => {
  log(`FATAL: ${e.message}`);
  console.error(e);
  process.exit(1);
});
