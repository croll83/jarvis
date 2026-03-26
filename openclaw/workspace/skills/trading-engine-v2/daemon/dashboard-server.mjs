/**
 * Dashboard Server — Lightweight HTTP API + static file server for v2 monitoring.
 *
 * Uses PositionManager as source of truth for open positions.
 * Trade log (JSONL) used only for historical CLOSE entries.
 *
 * Endpoints:
 *   GET /                     → serves dashboard HTML
 *   GET /api/overview         → daemon status + KPIs
 *   GET /api/account          → HL account: balance, positions, fills (async)
 *   GET /api/signals          → recent signals from dry-run-signals.jsonl
 *   GET /api/trades           → open positions + closed trades
 *   GET /api/swarm            → agent config + batch stats from daemon log
 *   GET /api/tokens           → token usage per model/day
 *   GET /api/strategies       → strategy configs + performance + trades
 *   GET /api/learner          → strategy-metrics.json (learner output)
 *   GET /api/geopolitics      → cached geopolitics/news analysis
 *   GET /api/sentiment        → cached sentiment data
 *   GET /api/assets           → asset universe (crypto + xyz)
 *   GET /api/log              → last N lines of daemon.log
 */

import http from 'http';
import { readFileSync, existsSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const STATE_DIR = resolve(__dirname, '..', 'state');
const DASHBOARD_DIR = resolve(__dirname, '..', 'dashboard');

// --- Helpers ---

function readJSON(filename) {
  const file = resolve(STATE_DIR, filename);
  try {
    if (existsSync(file)) return JSON.parse(readFileSync(file, 'utf8'));
  } catch {}
  return null;
}

function readJSONL(filename, maxLines = 200) {
  const file = resolve(STATE_DIR, filename);
  try {
    if (!existsSync(file)) return [];
    const lines = readFileSync(file, 'utf8').trim().split('\n').filter(Boolean);
    return lines.slice(-maxLines).reverse().map(line => {
      try { return JSON.parse(line); } catch { return null; }
    }).filter(Boolean);
  } catch { return []; }
}

function readLogTail(maxLines = 100) {
  const file = resolve(STATE_DIR, 'daemon.log');
  try {
    if (!existsSync(file)) return [];
    const lines = readFileSync(file, 'utf8').trim().split('\n').filter(Boolean);
    return lines.slice(-maxLines).reverse();
  } catch { return []; }
}

function parseDaemonStats(logLines, todayOnly = false) {
  const stats = {
    totalSignals: 0,
    scoutPassed: 0,
    scoutSkipped: 0,
    batchesFlushed: 0,
    trades: 0,
    vetoed: 0,
    holds: 0,
    lastBatchSize: 0,
  };

  const today = new Date().toISOString().split('T')[0];

  for (const line of logLines) {
    if (todayOnly && !line.includes(`[${today}`)) continue;

    if (line.includes('SIGNAL:')) stats.totalSignals++;
    if (line.includes('SCOUT: PASS')) stats.scoutPassed++;
    if (line.includes('SCOUT: SKIP')) stats.scoutSkipped++;
    if (line.includes('ANALYST: processing')) {
      stats.batchesFlushed++;
      const m = line.match(/processing (\d+)/);
      if (m) stats.lastBatchSize = parseInt(m[1]);
    }
    if (line.includes('DECISION:') && !line.includes('HOLD') && !line.includes('SKIP')) stats.trades++;
    if (line.includes('DECISION:') && line.includes('HOLD')) stats.holds++;
  }

  return stats;
}

// Normalize prices: add short coin names alongside BTC-PERP, xyz:GOLD etc.
function normalizePrices(raw) {
  const out = { ...raw };
  for (const [key, val] of Object.entries(raw)) {
    if (key.endsWith('-PERP')) out[key.replace('-PERP', '')] = val;
    if (key.startsWith('xyz:')) {
      const bare = key.slice(4);
      out[bare] = val;
      out[`${bare}-PERP`] = val;
    }
  }
  return out;
}

function getPrice(prices, coin) {
  return parseFloat(prices[coin] || prices[coin + '-PERP'] || prices['xyz:' + coin] || 0);
}

// Compute unrealized PnL for a list of positions given current prices
function computeUnrealizedPnl(positions, prices) {
  let total = 0;
  for (const pos of positions) {
    const cp = getPrice(prices, pos.coin);
    if (!cp || !pos.entryPrice) continue;
    const isLong = pos.side === 'long';
    total += isLong
      ? ((cp - pos.entryPrice) / pos.entryPrice) * pos.size
      : ((pos.entryPrice - cp) / pos.entryPrice) * pos.size;
  }
  return parseFloat(total.toFixed(2));
}

// --- Route builder ---

function buildRoutes(hlClient, allCoins, positionManager) {
  return {
    '/api/overview': () => {
      const tokenUsage = readJSON('token-usage.json');
      const today = new Date().toISOString().split('T')[0];
      const todayTokens = tokenUsage?.daily?.[today] || {};

      // Daily realized PnL from CLOSE entries in trade log (live only)
      const trades = readJSONL('trade-log.jsonl', 500).filter(t => !t.error && !t.dryRun);
      const todayTrades = trades.filter(t => t.timestamp && t.timestamp.startsWith(today));
      const todayCloses = todayTrades.filter(t => t.action === 'CLOSE');
      const dailyRealizedPnl = todayCloses.reduce((s, t) => s + (parseFloat(t.pnl) || 0), 0);

      // Open positions from PositionManager (source of truth)
      const liveOpen = positionManager ? positionManager.getLive().length : 0;
      const dryRunOpen = positionManager ? positionManager.getDryRun().length : 0;

      return {
        daemon: {
          mode: process.env.DRY_RUN === 'true' ? 'DRY-RUN' : 'LIVE',
          uptime: process.uptime(),
          startedAt: new Date(Date.now() - process.uptime() * 1000).toISOString(),
        },
        openPositions: { live: liveOpen, dryRun: dryRunOpen, total: liveOpen + dryRunOpen },
        tokenCost: todayTokens.total_cost_usd || 0,
        tokenBudget: tokenUsage?.alerts?.daily_budget_usd || 10,
        dailyPnl: parseFloat(dailyRealizedPnl.toFixed(2)),
        dailyTrades: todayTrades.filter(t => t.action === 'BUY' || t.action === 'SELL').length,
        dailyCloses: todayCloses.length,
      };
    },

    '/api/account': async () => {
      if (!hlClient) return { error: 'No HLClient available' };
      try {
        const [balance, hlPositions] = await Promise.all([
          hlClient.getBalance(),
          hlClient.getPositions(),
        ]);

        // Enrich HL positions with PositionManager data (strategy, TP/SL, open time)
        for (const pos of hlPositions) {
          if (!positionManager) continue;
          const managed = positionManager.getByCoin(pos.coin).filter(p => !p.dryRun);
          if (managed.length > 0) {
            // Use first matching managed position for enrichment
            const m = managed[0];
            pos.strategyName = m.strategyName || null;
            pos.strategyId = m.strategyId || null;
            pos.tp = m.tp || null;
            pos.sl = m.sl || null;
            pos.openTime = m.openedAt || null;
            pos.managedCount = managed.length; // how many strategies have this coin
          }
        }

        const totalUnrealizedPnl = hlPositions.reduce((s, p) => s + parseFloat(p.pnl || 0), 0);
        const totalNotional = hlPositions.reduce((s, p) => {
          const notional = p.size * (p.markPrice || p.entryPrice || 0);
          return s + (p.side === 'LONG' ? notional : -notional);
        }, 0);

        return {
          balance: {
            total: balance.overview,
            perpEquity: balance.perps.equity,
            spotFree: balance.spot.total,
            spotTotal: balance.spot.full || balance.spot.total,
            available: balance.availableForTrading,
            marginUsed: balance.perps.marginUsed,
            marginRatio: balance.perps.marginRatio,
            unrealizedPnl: balance.perps.unrealizedPnl,
            vault: balance.vault,
            staked: balance.staked,
            xyzEquity: balance.xyz?.equity || '0.00',
            xyzMarginUsed: balance.xyz?.marginUsed || '0.00',
          },
          positions: hlPositions,
          netPosition: totalNotional.toFixed(2),
          totalUnrealizedPnl: totalUnrealizedPnl.toFixed(2),
        };
      } catch (e) {
        return { error: e.message };
      }
    },

    '/api/signals': (query) => {
      const limit = parseInt(query.limit) || 100;
      const signals = readJSONL('dry-run-signals.jsonl', limit);

      const byType = {};
      const byHour = {};
      for (const s of signals) {
        byType[s.type] = (byType[s.type] || 0) + 1;
        if (s.timestamp) {
          const h = new Date(s.timestamp).toISOString().slice(0, 13);
          byHour[h] = (byHour[h] || 0) + 1;
        }
      }

      return { signals, stats: { byType, byHour, total: signals.length } };
    },

    '/api/trades': async (query) => {
      const limit = parseInt(query.limit) || 200;

      // Open positions from PositionManager
      const openPositions = positionManager ? positionManager.getAll() : [];

      // Closed trades from JSONL (CLOSE entries have complete pnl)
      const logEntries = readJSONL('trade-log.jsonl', limit);
      const closedTrades = logEntries.filter(t => t.action === 'CLOSE' && !t.error);

      // Current prices for unrealized PnL
      let prices = {};
      if (hlClient) {
        try {
          const p = await hlClient.getAllPrices(true);
          prices = normalizePrices(p || {});
        } catch {}
      }

      const unrealizedPnl = computeUnrealizedPnl(openPositions, prices);
      const realizedPnl = closedTrades.reduce((s, t) => s + (parseFloat(t.pnl) || 0), 0);
      const wins = closedTrades.filter(t => (parseFloat(t.pnl) || 0) > 0).length;
      const losses = closedTrades.filter(t => (parseFloat(t.pnl) || 0) < 0).length;
      const closedCount = wins + losses;

      return {
        openPositions,
        closedTrades,
        prices,
        pnl: {
          realizedPnl: parseFloat(realizedPnl.toFixed(2)),
          unrealizedPnl,
          totalPnl: parseFloat((realizedPnl + unrealizedPnl).toFixed(2)),
          openCount: openPositions.length,
          closedCount,
          wins,
          losses,
          winRate: closedCount > 0 ? parseFloat(((wins / closedCount) * 100).toFixed(1)) : null,
        },
      };
    },

    '/api/swarm': () => {
      const logLines = readLogTail(5000);
      const stats = parseDaemonStats(logLines, true);

      const agents = [
        { key: 'scout', name: 'Scout', model: 'claude-haiku-4-5', role: 'Pre-filter signals (parallel per signal)', tokenKey: 'haiku' },
        { key: 'analyst', name: 'Analyst', model: 'claude-sonnet-4-6', role: 'Batch trading decisions (1 call per batch)', tokenKey: 'sonnet' },
        { key: 'challenger', name: 'Challenger', model: 'gemini-3.1-flash-lite', role: "Devil's advocate — veto reduces confidence by 15%", tokenKey: 'gemini_flash_lite' },
        { key: 'news_analyst', name: 'News Analyst', model: 'grok-4-1-fast', role: 'Geopolitics & news (every 2h)', tokenKey: 'grok_fast' },
      ];

      const tokenUsage = readJSON('token-usage.json');
      const today = new Date().toISOString().split('T')[0];
      const todayTokens = tokenUsage?.daily?.[today] || {};

      for (const agent of agents) {
        const usage = todayTokens[agent.tokenKey];
        agent.calls = usage?.calls || 0;
        agent.inputTokens = usage?.input || 0;
        agent.outputTokens = usage?.output || 0;
        agent.costUsd = usage?.cost_usd || 0;
      }

      return { agents, stats, batchWindow: 60, scoutAssetCount: allCoins.length, totalAssets: allCoins.length };
    },

    '/api/tokens': () => {
      const tokenUsage = readJSON('token-usage.json');
      if (!tokenUsage) return { daily: {}, alerts: {} };
      const days = Object.keys(tokenUsage.daily || {}).sort().slice(-7);
      const daily = {};
      for (const d of days) daily[d] = tokenUsage.daily[d];
      return { daily, alerts: tokenUsage.alerts || {} };
    },

    '/api/strategies': async () => {
      const strategies = readJSON('strategies.json');
      const metrics = readJSON('strategy-metrics.json');

      // Closed trades from log (CLOSE entries)
      const logEntries = readJSONL('trade-log.jsonl', 500);
      const closedTrades = logEntries.filter(t => t.action === 'CLOSE' && !t.error);

      // Current prices for unrealized PnL
      let prices = {};
      if (hlClient) {
        try {
          const p = await hlClient.getAllPrices(true);
          prices = normalizePrices(p || {});
        } catch {}
      }

      // Build tradesByStrategy from raw log entries (format frontend expects)
      const tradesByStrategy = {};
      for (const entry of logEntries) {
        if (!entry.strategyId || entry.error) continue;
        if (!tradesByStrategy[entry.strategyId]) tradesByStrategy[entry.strategyId] = [];
        tradesByStrategy[entry.strategyId].push(entry);
      }

      // Per-strategy PnL from PositionManager + trade log
      const pnlByStrategy = {};
      for (const strat of (strategies?.strategies || [])) {
        const openPositions = positionManager ? positionManager.getByStrategy(strat.id) : [];
        const stratCloses = closedTrades.filter(t => t.strategyId === strat.id);

        const unrealizedPnl = computeUnrealizedPnl(openPositions, prices);
        const realizedPnl = stratCloses.reduce((s, t) => s + (parseFloat(t.pnl) || 0), 0);
        const wins = stratCloses.filter(t => (parseFloat(t.pnl) || 0) > 0).length;
        const losses = stratCloses.filter(t => (parseFloat(t.pnl) || 0) < 0).length;
        const closedCount = wins + losses;

        pnlByStrategy[strat.id] = {
          realizedPnl: parseFloat(realizedPnl.toFixed(2)),
          unrealizedPnl,
          totalPnl: parseFloat((realizedPnl + unrealizedPnl).toFixed(2)),
          openCount: openPositions.length,
          closedCount,
          wins,
          losses,
          winRate: closedCount > 0 ? parseFloat(((wins / closedCount) * 100).toFixed(1)) : null,
        };
      }

      // Live position coins (bare names) for open/closed cross-reference
      let livePositionCoins = [];
      if (hlClient) {
        try {
          const hlPos = await hlClient.getPositions();
          livePositionCoins = hlPos.map(p => p.coin.replace(/-PERP$/, ''));
        } catch {}
      }

      return {
        strategies: strategies?.strategies || [],
        tradesByStrategy,
        pnlByStrategy,
        livePositionCoins,
        prices,
        metricsHistory: metrics?.strategies || {},
        overallHistory: metrics?.overall || [],
      };
    },

    '/api/learner': () => {
      const metrics = readJSON('strategy-metrics.json');
      return metrics || { overall: [], strategies: {} };
    },

    '/api/geopolitics': () => {
      const geo = readJSON('geopolitics.json');
      return geo || { summary: 'No data', analysis: {}, timestamp: null };
    },

    '/api/sentiment': () => {
      const sentiment = readJSON('sentiment.json');
      return sentiment || { timestamp: null, scores: [], macro: [], sources: [] };
    },

    '/api/assets': () => {
      return {
        crypto: allCoins.filter(c => !c.startsWith('xyz:')),
        xyz: allCoins.filter(c => c.startsWith('xyz:')),
        total: allCoins.length,
      };
    },

    '/api/log': (query) => {
      const limit = parseInt(query.limit) || 50;
      return { lines: readLogTail(limit) };
    },
  };
}

// --- Server ---

export function startDashboardServer(opts = {}) {
  const port = opts.port || 18800;
  const hlClient = opts.hlClient || null;
  const allCoins = opts.allCoins || [];
  const positionManager = opts.positionManager || null;
  const routes = buildRoutes(hlClient, allCoins, positionManager);

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, `http://localhost:${port}`);
    const path = url.pathname;
    const query = Object.fromEntries(url.searchParams);

    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Methods', 'GET');

    if (routes[path]) {
      try {
        const data = await routes[path](query);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(data));
      } catch (e) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: e.message }));
      }
      return;
    }

    if (path === '/' || path === '/index.html') {
      const htmlFile = resolve(DASHBOARD_DIR, 'index.html');
      if (existsSync(htmlFile)) {
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        res.end(readFileSync(htmlFile, 'utf8'));
      } else {
        res.writeHead(404, { 'Content-Type': 'text/plain' });
        res.end('Dashboard HTML not found');
      }
      return;
    }

    res.writeHead(404, { 'Content-Type': 'text/plain' });
    res.end('Not found');
  });

  server.listen(port, '0.0.0.0', () => {
    console.log(`[Dashboard] Server running on http://0.0.0.0:${port}`);
  });

  server.on('error', (e) => {
    if (e.code === 'EADDRINUSE') {
      console.error(`[Dashboard] Port ${port} already in use — dashboard disabled`);
    } else {
      console.error(`[Dashboard] Server error: ${e.message}`);
    }
  });

  return server;
}
