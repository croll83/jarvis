#!/usr/bin/env node
/**
 * Market Daemon v3 — Always-on WebSocket listener with deterministic signal detection.
 *
 * Changes from v2:
 *   - PositionManager as source of truth (not JSONL replay)
 *   - TP/SL + hold time managed via PositionManager
 *   - HL reconciliation loop (30s) — detects externally closed positions
 *   - No scout cooldown — signals accumulate, never dropped
 *   - ~500 lines removed (duplicate TPSL code, position parsing)
 */

import { WSManager } from '../lib/ws-manager.mjs';
import { SignalRouter, SIGNAL_TYPES } from '../lib/signal-router.mjs';
import { SwarmOrchestrator, preFilterSignal } from './swarm.mjs';
import { TokenCounter } from './token-counter.mjs';
import { StrategyLearner } from './strategy-learner.mjs';
import { DailyReporter, sendTelegram } from './daily-report.mjs';
import { StrategyManager } from '../lib/strategy-manager.mjs';
import { PositionManager } from '../lib/position-manager.mjs';
import { HLClient } from '../lib/hl-client.mjs';
import { LLMDirectClient } from '../lib/llm-direct-client.mjs';
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

// --- Asset Universe ---
const FALLBACK_CRYPTO = ['BTC','ETH','HYPE','SOL','XRP','SUI','DOGE','BNB','PAXG','AVAX','LINK','AAVE','ENA','kPEPE','SEI'];
const FALLBACK_XYZ = ['xyz:CL','xyz:SILVER','xyz:BRENTOIL','xyz:GOLD','xyz:NATGAS','xyz:COPPER','xyz:TSLA','xyz:HOOD','xyz:NVDA','xyz:COIN','xyz:ORCL'];

let CRYPTO_COINS = FALLBACK_CRYPTO;
let XYZ_COINS = FALLBACK_XYZ;
let WS_TRADE_COINS = [...FALLBACK_CRYPTO];
let ALL_COINS = [...FALLBACK_CRYPTO, ...FALLBACK_XYZ];

// Timers
const FUNDING_POLL_INTERVAL = 60_000;
const XYZ_PRICE_POLL_INTERVAL = 5_000;
const SCOUT_FLUSH_INTERVAL = 60_000;
const ANALYST_INTERVAL_MS = 90_000;
const TPSL_CHECK_INTERVAL = 5_000;
const RECONCILE_INTERVAL = 30_000;

// Log rotation
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
    if (_logLineCount >= LOG_MAX_LINES && !_logRotating) {
      _logRotating = true;
      setImmediate(() => {
        try {
          const content = readFileSync(LOG_FILE, 'utf8');
          const lines = content.split('\n');
          if (lines.length > LOG_MAX_LINES) {
            writeFileSync(LOG_FILE, lines.slice(-LOG_KEEP_LINES).join('\n'));
            _logLineCount = LOG_KEEP_LINES;
          }
        } catch {} finally { _logRotating = false; }
      });
    }
  } catch {}
}

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
      return { crypto: wl.properties.crypto || FALLBACK_CRYPTO, xyz: wl.properties.xyz || FALLBACK_XYZ };
    }
  } catch (e) {
    console.error(`[Daemon] Watchlist load failed: ${e.message}, using fallback`);
  }
  return { crypto: FALLBACK_CRYPTO, xyz: FALLBACK_XYZ };
}

// XYZ API helper
async function xyzApiCall(body) {
  const resp = await fetch('https://api.hyperliquid.xyz/info', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`XYZ API error: ${resp.status}`);
  return resp.json();
}

function logDryRunSignal(signal) {
  const file = resolve(STATE_DIR, 'dry-run-signals.jsonl');
  if (!existsSync(STATE_DIR)) mkdirSync(STATE_DIR, { recursive: true });
  appendFileSync(file, JSON.stringify(signal) + '\n');
}

// ============================================================
// Main
// ============================================================

async function main() {
  log(`Market Daemon v3 starting (${DRY_RUN ? 'DRY-RUN' : 'LIVE'} mode)`);

  // Load asset universe
  const watchlist = await loadWatchlistFromOntology();
  CRYPTO_COINS = watchlist.crypto;
  XYZ_COINS = watchlist.xyz;
  WS_TRADE_COINS = [...CRYPTO_COINS];
  ALL_COINS = [...CRYPTO_COINS, ...XYZ_COINS];
  log(`Monitoring ${ALL_COINS.length} assets: ${ALL_COINS.join(', ')}`);

  const ws = new WSManager();
  const router = new SignalRouter();
  const hl = new HLClient();

  // --- Position Manager (source of truth) ---
  const positionManager = new PositionManager({
    stateDir: STATE_DIR,
    tradeLogPath: resolve(STATE_DIR, 'trade-log.jsonl'),
    log,
    onClose: (closeEntry, allEntries) => {
      // Telegram notification on close
      const entries = allEntries || [closeEntry];
      const totalPnl = entries.reduce((s, e) => s + (e.pnl || 0), 0);
      const emoji = totalPnl >= 0 ? '✅' : '🔴';
      const tag = closeEntry.dryRun ? '🧪' : '';
      const strats = entries.map(e => e.strategyName || '?').join(', ');
      sendTelegram(`${tag}${emoji} <b>CLOSE ${closeEntry.coin}</b> ${closeEntry.side} (${closeEntry.closeReason})\nPnL: ${totalPnl >= 0 ? '+' : ''}$${totalPnl.toFixed(2)}\nStrategies: ${strats}`).catch(() => {});
    },
  });

  // Restore positions from trade log (survives daemon restart)
  positionManager.restoreFromLog();
  log(`Positions restored: ${positionManager.count()} (${positionManager.getLive().length} live, ${positionManager.getDryRun().length} dry-run)`);

  // --- Dashboard ---
  startDashboardServer({ port: 18800, hlClient: hl, allCoins: ALL_COINS, positionManager });

  // --- Token counter ---
  const tokenCounter = new TokenCounter({
    onAlert: (alert) => {
      log(`TOKEN ALERT [${alert.level}]: ${alert.message}`);
      sendTelegram(`⚠️ <b>Token Alert [${alert.level}]</b>\n${alert.message}`).catch(() => {});
    },
  });

  // --- Strategy manager ---
  const strategyManager = new StrategyManager({ stateDir: STATE_DIR });
  try {
    const strategies = await strategyManager.load();
    log(`Loaded ${strategies.length} strategies: ${strategies.map(s => `${s.name} (${s.status})`).join(', ')}`);
  } catch (e) {
    log(`WARNING: Strategy load failed: ${e.message}`);
  }

  // --- LLM Direct Client (direct API calls, TPM keys) ---
  const llm = new LLMDirectClient({
    log: (msg) => log(msg),
  });
  try {
    await llm.init();
    log('LLM Direct Client initialized');
  } catch (e) {
    log(`FATAL: LLM init failed: ${e.message}`);
    process.exit(1);
  }

  // --- Swarm ---
  const swarm = new SwarmOrchestrator({
    tokenCounter,
    
    llmClient: llm,
    strategyManager,
    positionManager,
    dryRun: DRY_RUN,
  });

  // --- Strategy learner ---
  const learner = new StrategyLearner({
    strategyManager,
    intervalMs: 3600_000,
    tradeThreshold: 5,
  });
  let tradeCount = 0;

  // ============================================================
  // Signal pipeline: Pre-filter → Scout (30s batch) → Analyst (90s timer)
  // ============================================================

  const signalBuffer = new Map(); // coin → Signal[]

  function bufferSignal(signal) {
    // Pre-filter: reject obvious noise
    const filter = preFilterSignal(signal);
    if (!filter.pass) return;

    // Accumulate ALL signals for this coin (no cooldown, never drop)
    if (!signalBuffer.has(signal.coin)) {
      signalBuffer.set(signal.coin, []);
    }
    signalBuffer.get(signal.coin).push(signal);
  }

  // --- Scout flush: every 60s ---
  let scoutFlushing = false;
  setInterval(async () => {
    if (signalBuffer.size === 0 || scoutFlushing) return;
    scoutFlushing = true;
    const entries = [...signalBuffer.entries()];
    signalBuffer.clear();

    // Gather lightweight context for scouts (cached, not per-coin)
    let scoutContext = {};
    try {
      const ctx = await swarm._gatherContext();
      scoutContext = { account: ctx.account };
    } catch {}

    for (const [coin, signals] of entries) {
      // Signal confluence: require 2+ signals OR 1 signal with confidence ≥65 OR any whale trade
      const maxConf = Math.max(...signals.map(s => s.confidence || 0));
      const hasWhale = signals.some(s => s.type === 'whale_trade');
      if (signals.length < 2 && maxConf < 65 && !hasWhale) {
        log(`SCOUT: SKIP ${coin} (${signals.length} signal, conf ${maxConf}) — no confluence`);
        continue;
      }

      try {
        const geoContext = loadGeoContext(coin);
        const existingPos = positionManager.getByCoin(coin)[0] || null;

        const result = await swarm.runScout(coin, signals, {
          ...scoutContext,
          geopolitics: geoContext,
          existingPosition: existingPos ? { side: existingPos.side, size: existingPos.size, pnl: '?' } : null,
        });

        if (result.verdict === 'PASS') {
          log(`SCOUT: PASS ${coin} (${signals.length} signals, ${result.confidence}%) — ${result.brief} [analyst buf: ${swarm.analystBufferSize}]`);
        } else {
          log(`SCOUT: SKIP ${coin} (${signals.length} signals) — ${result.brief}`);
        }
      } catch (e) {
        log(`SCOUT ERROR ${coin}: ${e.message}`);
      }
    }
    scoutFlushing = false;
  }, SCOUT_FLUSH_INTERVAL);

  // --- Analyst timer: every 90s ---
  let analystRunning = false;
  setInterval(async () => {
    if (analystRunning || swarm.analystBufferSize === 0) return;
    analystRunning = true;

    log(`ANALYST: processing ${swarm.analystBufferSize} scout-passed signal groups`);
    try {
      const results = await swarm.runAnalyst();
      for (const result of results) {
        const tag = result.strategyName ? `[${result.strategyName}] ` : '';
        log(`DECISION: ${tag}${result.action} ${result.coin} — ${result.reason || ''} ${result.executed ? '(EXECUTED)' : result.dryRun ? '(DRY-RUN)' : ''}`);
        if (result.action !== 'HOLD' && result.action !== 'RISK_BLOCKED') {
          tradeCount++;
        }
      }
      // Trigger learner
      if (learner.shouldRun(tradeCount)) {
        learner.run().then(r => {
          log(`LEARNER: ${r.overall?.status || r.status} | ${r.lessonsGenerated || 0} lessons`);
        }).catch(e => log(`LEARNER ERROR: ${e.message}`));
      }
    } catch (e) {
      log(`ANALYST ERROR: ${e.message}`);
    } finally {
      analystRunning = false;
    }
  }, ANALYST_INTERVAL_MS);

  // ============================================================
  // TP/SL + Hold Time (via PositionManager, throttled)
  // ============================================================

  const latestPrices = {};
  let lastTPSLCheck = 0;
  let tpslRunning = false;

  async function runTPSLCheck() {
    // TP/SL triggers
    const triggers = positionManager.getTPSLTriggers(latestPrices);
    const closePromises = [];
    // Deduplicate: only close each coin once (HL has one position per coin)
    const closingCoins = new Set();

    for (const { position, reason, triggerPrice } of triggers) {
      if (position.dryRun) {
        const entry = positionManager.close(position.coin, position.strategyId, triggerPrice, reason);
        if (entry) log(`DRY-RUN CLOSE: ${position.coin} ${position.side} ${reason} — PnL ${entry.pnl >= 0 ? '+' : ''}$${entry.pnl}`);
      } else if (!closingCoins.has(position.coin)) {
        closingCoins.add(position.coin);
        closePromises.push(closeLivePosition(position.coin, triggerPrice, reason));
      }
    }

    // Hold time expired
    const expired = positionManager.getHoldTimeExpired(strategyManager, latestPrices);
    for (const { position, currentPrice, shouldClose, closeReason } of expired) {
      if (!shouldClose) continue;
      if (position.dryRun) {
        const entry = positionManager.close(position.coin, position.strategyId, currentPrice, closeReason);
        if (entry) log(`DRY-RUN CLOSE: ${position.coin} ${position.side} ${closeReason} — PnL ${entry.pnl >= 0 ? '+' : ''}$${entry.pnl}`);
      } else if (!closingCoins.has(position.coin)) {
        closingCoins.add(position.coin);
        closePromises.push(closeLivePosition(position.coin, currentPrice, closeReason));
      }
    }

    // Wait for ALL close operations to complete before allowing next check
    if (closePromises.length > 0) {
      await Promise.allSettled(closePromises);
    }
  }

  function onPriceUpdate(mids) {
    Object.assign(latestPrices, mids);
    const now = Date.now();
    if (now - lastTPSLCheck < TPSL_CHECK_INTERVAL || tpslRunning) return;
    lastTPSLCheck = now;
    tpslRunning = true;

    runTPSLCheck().catch(e => {
      log(`TPSL CHECK ERROR: ${e.message}`);
    }).finally(() => {
      tpslRunning = false;
    });
  }

  async function closeLivePosition(coin, triggerPrice, reason) {
    try {
      const result = await hl.closePosition(coin, 1.0);
      const fillPx = result?.response?.data?.statuses?.[0]?.filled?.avgPx;
      const closePrice = fillPx ? parseFloat(fillPx) : triggerPrice;

      const entries = positionManager.closeAllForCoin(coin, closePrice, reason);
      const totalPnl = entries.reduce((s, e) => s + e.pnl, 0);
      log(`LIVE CLOSE: ${coin} ${reason} — ${entries.length} positions, total PnL ${totalPnl >= 0 ? '+' : ''}$${totalPnl.toFixed(2)}`);
    } catch (e) {
      // If HL says "No open position", the position was already closed externally
      // (liquidated, manual close, etc.) — clean up PositionManager to avoid infinite retry
      if (e.message?.includes('No open position')) {
        const entries = positionManager.closeAllForCoin(coin, triggerPrice, reason + '_external');
        if (entries.length > 0) {
          const totalPnl = entries.reduce((s, e) => s + e.pnl, 0);
          log(`EXTERNAL CLOSE: ${coin} ${reason} — position gone from HL, cleaned ${entries.length} PM entries, PnL ${totalPnl >= 0 ? '+' : ''}$${totalPnl.toFixed(2)}`);
        }
      } else {
        log(`LIVE CLOSE FAILED: ${coin} ${reason} — ${e.message}`);
      }
    }
  }

  // ============================================================
  // HL Reconciliation (30s, live mode only)
  // ============================================================

  async function reconcileWithHL() {
    if (DRY_RUN) return;
    try {
      const hlPositions = await hl.getPositions();
      const { closed, unmanaged } = positionManager.reconcile(hlPositions, latestPrices);

      for (const entry of closed) {
        log(`RECONCILE: closed phantom ${entry.coin} ${entry.side} [${entry.strategyName || '?'}] — PnL $${entry.pnl}`);
      }
      for (const pos of unmanaged) {
        log(`RECONCILE: unmanaged HL position ${pos.coin} ${pos.side} ${pos.size}`);
      }
    } catch (e) {
      log(`RECONCILE ERROR: ${e.message}`);
    }
  }

  // ============================================================
  // Geopolitics context loader
  // ============================================================

  function loadGeoContext(coin) {
    try {
      const file = resolve(STATE_DIR, 'geopolitics.json');
      if (existsSync(file)) {
        const geo = JSON.parse(readFileSync(file, 'utf8'));
        return geo.analysis?.[coin] || null;
      }
    } catch {}
    return null;
  }

  // ============================================================
  // Signal router → buffer
  // ============================================================

  router.onSignal((signal) => {
    const brief = signal.type === 'whale_trade'
      ? `$${(signal.notional / 1000).toFixed(0)}k ${signal.side === 'B' ? 'BUY' : 'SELL'}`
      : signal.type === 'volume_spike' ? `${signal.multiplier}x avg`
      : signal.type === 'rsi_extreme' ? `RSI ${signal.rsi?.toFixed(0)} ${signal.condition}`
      : signal.direction || '';
    log(`SIGNAL: ${signal.type} ${signal.coin} ${brief}`);
    logDryRunSignal(signal);
    bufferSignal(signal);
  });

  // ============================================================
  // WebSocket setup
  // ============================================================

  ws.addEventListener('connected', () => log('WebSocket connected'));
  ws.addEventListener('disconnected', () => log('WebSocket disconnected'));
  ws.addEventListener('error', (e) => log(`WebSocket error: ${e.detail?.message}`));
  ws.addEventListener('fatal', () => {
    log('FATAL: WebSocket reconnection failed. Exiting.');
    process.exit(1);
  });

  await ws.connect();

  // allMids — all crypto prices
  ws.onAllMids((data) => {
    if (data.mids) {
      router.updatePrices(data.mids);
      onPriceUpdate(data.mids);
    }
  });

  // Trades per crypto coin
  for (const coin of WS_TRADE_COINS) {
    ws.onTrades(coin, (trades) => {
      if (Array.isArray(trades)) router.updateTrades(coin, trades);
    });
  }

  // Candles per crypto coin (5m)
  for (const coin of CRYPTO_COINS) {
    ws.onCandle(coin, '5m', (candle) => {
      if (candle) router.updateCandle(coin, candle);
    });
  }

  // User events
  if (ADDRESS) {
    ws.onUserFills(ADDRESS, (fills) => {
      const arr = Array.isArray(fills) ? fills : fills?.fills || [];
      for (const f of arr) {
        log(`FILL: ${f.dir || f.side} ${f.coin} ${f.sz}@${f.px} PnL=${f.closedPnl || 0}`);
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

  // ============================================================
  // Polling loops
  // ============================================================

  async function pollXyzPrices() {
    try {
      const mids = await xyzApiCall({ type: 'allMids', dex: 'xyz' });
      router.updatePrices(mids);
      onPriceUpdate(mids);
    } catch (e) {
      log(`XYZ price poll error: ${e.message}`);
    }
  }

  async function pollFundingAndOI() {
    try {
      const [perpMeta, perpCtxs] = await hl.getMetaAndCtxs('perp');
      for (const coin of CRYPTO_COINS) {
        const idx = perpMeta.universe.findIndex(u => u.name === coin);
        if (idx >= 0) {
          router.updateFunding(coin, parseFloat(perpCtxs[idx].funding));
          router.updateOI(coin, parseFloat(perpCtxs[idx].openInterest));
        }
      }
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
      } catch {}
    }
  }

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
      } catch {}
    }
  }

  // Initial data load
  await Promise.all([pollXyzPrices(), pollFundingAndOI(), pollXyzCandles(true), pollCryptoCandles(true)]);
  log('Initial data loaded');

  // Start polling timers
  setInterval(pollXyzPrices, XYZ_PRICE_POLL_INTERVAL);
  setInterval(pollFundingAndOI, FUNDING_POLL_INTERVAL);
  setInterval(pollXyzCandles, 60_000);
  setInterval(pollCryptoCandles, 5 * 60_000);

  // Reconciliation (live mode only)
  if (!DRY_RUN) {
    // Initial reconciliation
    await reconcileWithHL();
    setInterval(reconcileWithHL, RECONCILE_INTERVAL);
  }

  // Status report every 5 min
  setInterval(() => {
    const summary = tokenCounter.getTodaySummary();
    const posCount = positionManager.count();
    log(`STATUS: ${ALL_COINS.length} coins | WS: ${ws.connected ? 'OK' : 'DISCONNECTED'} | Positions: ${posCount} | Tokens: $${summary.total_cost_usd.toFixed(2)} (${summary.budget_used_pct}%)`);
  }, 5 * 60_000);

  // Strategy learner (hourly)
  setInterval(() => {
    if (learner.shouldRun(tradeCount)) {
      learner.run().then(r => {
        log(`LEARNER (periodic): ${r.overall?.status || r.status} | ${r.lessonsGenerated || 0} lessons`);
      }).catch(e => log(`LEARNER ERROR: ${e.message}`));
    }
  }, 3600_000);

  // Geopolitics poll (2h)
  async function pollGeopolitics() {
    try {
      const result = await swarm.pollGeopolitics(ALL_COINS);
      if (result) log(`GEOPOLITICS: updated ${Object.keys(result.analysis || {}).length} assets`);
    } catch (e) {
      log(`GEOPOLITICS ERROR: ${e.message}`);
    }
  }
  pollGeopolitics().catch(e => log(`GEOPOLITICS BOOT ERROR: ${e.message}`));
  setInterval(pollGeopolitics, 2 * 3600_000);

  // Reload strategies (30 min)
  setInterval(() => {
    strategyManager.load().then(s => {
      log(`Strategies reloaded: ${s.length} (${s.filter(x => x.status === 'active').length} active, ${s.filter(x => x.status === 'dry-run').length} dry-run)`);
    }).catch(e => log(`Strategy reload failed: ${e.message}`));
  }, 30 * 60_000);

  // Daily report (09:00 + 21:00 CET)
  const dailyReporter = new DailyReporter({ hlClient: hl, positionManager });
  let lastReportHour = -1;
  setInterval(() => {
    const now = new Date();
    const cetHour = parseInt(now.toLocaleString('en-US', { hour: 'numeric', hour12: false, timeZone: 'Europe/Rome' }));
    const cetMinute = parseInt(now.toLocaleString('en-US', { minute: 'numeric', timeZone: 'Europe/Rome' }));
    if ((cetHour === 9 || cetHour === 21) && cetMinute < 5 && lastReportHour !== cetHour) {
      lastReportHour = cetHour;
      dailyReporter.sendReport(1).then(r => {
        log(`DAILY REPORT: ${r.sent ? 'sent' : 'FAILED'} — ${r.trades} trades`);
      }).catch(e => log(`DAILY REPORT ERROR: ${e.message}`));
    }
    if (cetMinute >= 5 && lastReportHour === cetHour) lastReportHour = -1;
  }, 60_000);

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

main().catch(e => {
  log(`FATAL: ${e.message}`);
  console.error(e);
  process.exit(1);
});
