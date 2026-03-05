#!/usr/bin/env node
/**
 * market-precheck — Consolidated pre-trade data collection (NO LLM).
 *
 * Gathers ALL data needed for a trading decision in a single script:
 *   - Active strategies from ontology
 *   - Technical signals for all target coins
 *   - Whale positions and consensus
 *   - Account balance, positions (with open duration), margin
 *   - Last sentiment scan from daily memory
 *
 * Usage: node market-precheck.mjs [--json]
 *
 * Default output: formatted text (readable prompt for LLM)
 * --json: raw JSON (for programmatic use)
 *
 * Env:
 *   ONTOLOGY_URL, ONTOLOGY_API_TOKEN, ONTOLOGY_SPEAKER
 *   JARVIS_WALLET, HYPERLIQUID_ADDRESS
 *   MEMORY_DIR or WORKSPACE_DIR (for sentiment lookup)
 *   WHALE_MIN_POSITION (default: 10000)
 */

import { HLClient } from './lib/hl-client.mjs';
import { analyzeCandles, computeSignalScore } from './lib/indicators.mjs';
import { readFileSync, existsSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const ONTOLOGY_URL = process.env.ONTOLOGY_URL || 'http://127.0.0.1:8100';
const ONTOLOGY_SPEAKER = process.env.ONTOLOGY_SPEAKER || 'jarvis-agent';
const WHALE_MIN_POSITION = parseFloat(process.env.WHALE_MIN_POSITION || '10000');

const [,, ...args] = process.argv;
const hasFlag = (name) => args.includes(`--${name}`);

// --- Helpers ---

async function ontologyRequest(method, path, body = null) {
  const opts = {
    method,
    headers: {
      'Content-Type': 'application/json',
      'X-Speaker-Id': ONTOLOGY_SPEAKER,
      'Authorization': `Bearer ${process.env.ONTOLOGY_API_TOKEN || ''}`,
    },
  };
  if (body) opts.body = JSON.stringify(body);
  const resp = await fetch(`${ONTOLOGY_URL}${path}`, opts);
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`Ontology ${method} ${path}: ${resp.status} ${text}`);
  }
  return resp.json();
}

function parseStrategyConfig(description) {
  if (!description) return null;
  const marker = '---CONFIG---';
  const idx = description.indexOf(marker);
  if (idx === -1) return null;
  try {
    return JSON.parse(description.slice(idx + marker.length).trim());
  } catch {
    return null;
  }
}

function getWorkspaceDir() {
  return process.env.WORKSPACE_DIR || resolve(__dirname, '..', '..', '..');
}

function getMemoryDir() {
  if (process.env.MEMORY_DIR) return process.env.MEMORY_DIR;
  return resolve(getWorkspaceDir(), 'memory');
}

// --- Data collection functions ---

async function fetchStrategies() {
  try {
    const resp = await ontologyRequest('GET', '/entities?type=Strategy');
    const all = resp.entities || resp || [];
    return all
      .filter(s => s.properties?.status === 'active')
      .map(s => {
        const p = s.properties || {};
        const config = parseStrategyConfig(p.description);
        return {
          id: s.id,
          name: p.name,
          status: p.status,
          budget_pct: p.budget_limit,
          target_assets: p.target_assets || [],
          weights: config?.weights || null,
          hold_time: config?.hold_time || null,
          max_positions: config?.max_positions || null,
          leverage: config?.leverage || null,
          tp_pct: config?.tp_pct || null,
          sl_pct: config?.sl_pct || config?.sl_atr_multiplier || null,
        };
      });
  } catch (e) {
    return { error: `Failed to fetch strategies: ${e.message}` };
  }
}

async function fetchSignals(hl, coins) {
  const signals = {};
  // Process coins in batches of 5 to avoid hammering the API
  const batches = [];
  for (let i = 0; i < coins.length; i += 5) {
    batches.push(coins.slice(i, i + 5));
  }

  for (const batch of batches) {
    const results = await Promise.allSettled(
      batch.map(async (coin) => {
        const candles = await hl.getCandles(coin, '5m', 100);
        if (candles.length < 30) {
          return { coin, error: `Insufficient data (${candles.length} candles)` };
        }
        const analysis = analyzeCandles(candles, {
          rsiPeriod: 9, emaFast: 9, emaSlow: 21,
          macdFast: 5, macdSlow: 13, macdSignal: 6,
          bbPeriod: 20, bbStdDev: 2, atrPeriod: 14,
        });
        const signal = computeSignalScore(analysis);
        return {
          coin,
          price: analysis.price,
          score: signal.score,
          direction: signal.direction,
          confidence: signal.confidence,
          key_signals: signal.signals,
          rsi: analysis.rsi,
          ema_trend: analysis.ema.trend,
          ema_cross: analysis.ema.cross,
          bb_position: analysis.bb.position,
          bb_squeeze: analysis.bb.squeeze,
          vwap_delta: analysis.vwapDelta,
        };
      })
    );

    for (const r of results) {
      if (r.status === 'fulfilled') {
        signals[r.value.coin] = r.value;
      } else {
        // Extract coin name from error if possible
        signals[`unknown`] = { error: r.reason.message };
      }
    }
  }
  return signals;
}

async function fetchWhalePositions(hl) {
  try {
    const whales = await ontologyRequest('POST', '/entities/query?type=Account', {
      service: 'hl_whales',
    });
    if (!whales || whales.length === 0) {
      return { error: 'No tracked whales. Run whale-monitor.mjs refresh first.' };
    }

    const byCoin = {};
    let scannedCount = 0;

    for (const whale of whales) {
      const addr = whale.properties?.username;
      if (!addr) continue;

      try {
        const positions = await hl.getPositions(addr);
        scannedCount++;
        for (const pos of positions) {
          const notional = pos.size * pos.entryPrice;
          if (notional < WHALE_MIN_POSITION) continue;

          if (!byCoin[pos.coin]) byCoin[pos.coin] = { long: 0, short: 0, whale_count: 0 };
          const side = pos.side.toLowerCase();
          byCoin[pos.coin][side] += notional;
          byCoin[pos.coin].whale_count++;
        }
      } catch {
        // Skip individual whale errors
      }
    }

    // Add bias
    for (const [coin, data] of Object.entries(byCoin)) {
      data.long = Math.round(data.long);
      data.short = Math.round(data.short);
      data.bias = data.long > data.short ? 'LONG' : data.short > data.long ? 'SHORT' : 'MIXED';
    }

    return { whales_scanned: scannedCount, positions: byCoin };
  } catch (e) {
    return { error: `Failed to fetch whale positions: ${e.message}` };
  }
}

async function fetchAccountState(hl) {
  try {
    const [balance, positions, fills] = await Promise.all([
      hl.getBalance(),
      hl.getPositions(),
      hl.getFills(100),
    ]);

    // Compute position durations from fills
    const positionsWithDuration = positions.map(pos => {
      // Find the earliest matching fill for this position (same coin, same side)
      const matchingFills = fills.filter(f =>
        f.coin === pos.coin &&
        f.side.toUpperCase() === (pos.side === 'LONG' ? 'B' : 'A') // B=buy, A=sell (ask)
      );

      // The most recent matching fill is likely the position entry
      // (or the most recent add-to-position)
      let openedAt = null;
      let durationMinutes = null;

      if (matchingFills.length > 0) {
        // Sort by time ascending and find the first fill that opened this position
        // For simplicity, use the most recent fill as the entry time
        const sorted = matchingFills.sort((a, b) => new Date(a.time) - new Date(b.time));
        // The first fill is likely when the position was opened
        openedAt = sorted[0].time;
        durationMinutes = Math.round((Date.now() - new Date(openedAt).getTime()) / 60000);
      }

      return {
        ...pos,
        opened_at: openedAt,
        duration_minutes: durationMinutes,
      };
    });

    return {
      account: {
        equity: balance.perps.equity,
        available: balance.perps.available,
        marginUsed: balance.perps.marginUsed,
        marginRatio: balance.perps.marginRatio,
        unrealizedPnl: balance.perps.unrealizedPnl,
        overview: balance.overview,
        spot: balance.spot,
      },
      positions: positionsWithDuration,
    };
  } catch (e) {
    return { error: `Failed to fetch account state: ${e.message}` };
  }
}

function parseSentimentTable(body) {
  const scores = {};
  const tableRegex = /\|\s*(\w+)\s*\|\s*([+-]?\d+)\s*\|\s*(\d+)%?\s*\|\s*(\w+)\s*\|/g;
  let row;
  while ((row = tableRegex.exec(body)) !== null) {
    if (row[1] === 'Coin') continue; // skip header
    scores[row[1]] = {
      score: parseInt(row[2]),
      confidence: parseInt(row[3]),
      trend: row[4],
    };
  }
  return scores;
}

function fetchLastSentiments(count = 3) {
  try {
    const memoryDir = getMemoryDir();
    const today = new Date().toISOString().split('T')[0];
    const yesterday = new Date(Date.now() - 86400000).toISOString().split('T')[0];

    const allScans = [];

    for (const date of [today, yesterday]) {
      const filePath = resolve(memoryDir, `${date}.md`);
      if (!existsSync(filePath)) continue;

      const content = readFileSync(filePath, 'utf8');

      // Match all sentiment blocks (both heading formats)
      const regex = /## Sentiment Scan[— ]+(\d{2}:\d{2}) UTC\n([\s\S]*?)(?=\n## |\n---|\Z)/g;
      let match;
      while ((match = regex.exec(content)) !== null) {
        const scores = parseSentimentTable(match[2]);
        if (Object.keys(scores).length === 0) continue; // skip empty scans
        const scanTime = new Date(`${date}T${match[1]}:00Z`);
        allScans.push({
          scan_time: scanTime.toISOString(),
          age_hours: parseFloat(((Date.now() - scanTime.getTime()) / 3600000).toFixed(1)),
          scores,
        });
      }
    }

    if (allScans.length === 0) {
      return { available: false, reason: 'No sentiment scans found in last 2 days' };
    }

    // Sort newest first, take last N
    allScans.sort((a, b) => new Date(b.scan_time) - new Date(a.scan_time));
    return {
      available: true,
      count: Math.min(count, allScans.length),
      scans: allScans.slice(0, count),
    };
  } catch (e) {
    return { available: false, reason: `Error reading sentiment: ${e.message}` };
  }
}

function fetchMemoryLessons() {
  try {
    const workspaceDir = getWorkspaceDir();
    const memoryFile = resolve(workspaceDir, 'MEMORY.md');
    if (!existsSync(memoryFile)) return null;
    const content = readFileSync(memoryFile, 'utf8');
    // Extract key sections: lessons, patterns, known issues
    // Return raw content (it's ~4K, the LLM needs all of it for decision-making)
    return content;
  } catch {
    return null;
  }
}

// --- Output formatting ---

function formatTextOutput(data) {
  const lines = [];
  lines.push('=== MARKET PRECHECK ===');
  lines.push(`Timestamp: ${data.timestamp}`);
  lines.push('');

  // Account
  if (data.account_state.error) {
    lines.push(`[ACCOUNT ERROR] ${data.account_state.error}`);
  } else {
    const a = data.account_state.account;
    lines.push('--- Account ---');
    lines.push(`  Equity: $${a.equity} | Available: $${a.available} | Margin: $${a.marginUsed} (${a.marginRatio})`);
    lines.push(`  Unrealized PnL: $${a.unrealizedPnl}`);
    lines.push('');

    // Positions
    const positions = data.account_state.positions;
    if (positions.length > 0) {
      lines.push(`--- Open Positions (${positions.length}) ---`);
      for (const p of positions) {
        const pnlSign = parseFloat(p.pnl) >= 0 ? '+' : '';
        const duration = p.duration_minutes !== null ? `${p.duration_minutes}min` : 'unknown';
        lines.push(`  ${p.side} ${p.coin}: size ${p.size} @ $${p.entryPrice} | PnL: ${pnlSign}$${p.pnl} | ${p.leverage}x | open ${duration}`);
      }
    } else {
      lines.push('--- No Open Positions ---');
    }
  }
  lines.push('');

  // Strategies
  if (Array.isArray(data.strategies)) {
    lines.push(`--- Active Strategies (${data.strategies.length}) ---`);
    for (const s of data.strategies) {
      const w = s.weights ? `tech:${s.weights.technical} sent:${s.weights.sentiment} whale:${s.weights.whales}` : 'n/a';
      const hold = s.hold_time ? `${s.hold_time.min}-${s.hold_time.max} ${s.hold_time.unit}` : 'n/a';
      lines.push(`  [${s.name}] budget:${s.budget_pct}% | weights: ${w} | hold: ${hold} | max_pos: ${s.max_positions}`);
    }
  } else {
    lines.push(`[STRATEGIES ERROR] ${data.strategies?.error || 'unknown'}`);
  }
  lines.push('');

  // Signals
  lines.push('--- Technical Signals ---');
  const signalEntries = Object.entries(data.signals).sort((a, b) => Math.abs(b[1].score || 0) - Math.abs(a[1].score || 0));
  for (const [coin, s] of signalEntries) {
    if (s.error) {
      lines.push(`  ${coin}: ERROR - ${s.error}`);
      continue;
    }
    const scoreSign = s.score >= 0 ? '+' : '';
    lines.push(`  ${coin}: ${scoreSign}${s.score} ${s.direction} (conf: ${s.confidence}%) @ $${s.price}`);
    if (s.key_signals && s.key_signals.length > 0) {
      for (const sig of s.key_signals) {
        lines.push(`    - ${sig}`);
      }
    }
  }
  lines.push('');

  // Whales
  lines.push('--- Whale Consensus ---');
  if (data.whales.error) {
    lines.push(`  ${data.whales.error}`);
  } else {
    const whalePositions = data.whales.positions || {};
    const entries = Object.entries(whalePositions).sort((a, b) => (b[1].long + b[1].short) - (a[1].long + a[1].short));
    if (entries.length === 0) {
      lines.push('  No significant whale positions detected.');
    } else {
      lines.push(`  Scanned: ${data.whales.whales_scanned} whales`);
      for (const [coin, d] of entries) {
        lines.push(`  ${coin}: ${d.bias} (long: $${d.long.toLocaleString()}, short: $${d.short.toLocaleString()}, ${d.whale_count} whales)`);
      }
    }
  }
  lines.push('');

  // Sentiment (last 3 scans for trend)
  lines.push('--- Sentiment (last 3 scans) ---');
  if (!data.sentiment.available) {
    lines.push(`  Not available: ${data.sentiment.reason}`);
  } else {
    for (let i = 0; i < data.sentiment.scans.length; i++) {
      const scan = data.sentiment.scans[i];
      const label = i === 0 ? 'LATEST' : `${scan.age_hours}h ago`;
      lines.push(`  [${label}] ${scan.scan_time} (${scan.age_hours}h ago)`);
      const sentEntries = Object.entries(scan.scores);
      for (const [coin, s] of sentEntries) {
        const scoreSign = s.score >= 0 ? '+' : '';
        lines.push(`    ${coin}: ${scoreSign}${s.score} (conf: ${s.confidence}%, trend: ${s.trend})`);
      }
    }
  }

  // Memory lessons (from MEMORY.md — included when using --light-context)
  if (data.memory_lessons) {
    lines.push('');
    lines.push('--- Lessons Learned (MEMORY.md) ---');
    lines.push(data.memory_lessons);
  }

  return lines.join('\n');
}

// --- Main ---

async function main() {
  const jsonOutput = hasFlag('json');
  const hl = new HLClient();

  // Phase 1: Fetch strategies first (need target_assets for signals)
  const strategies = await fetchStrategies();

  // Collect unique coins from all active strategies
  let targetCoins = [];
  if (Array.isArray(strategies)) {
    const coinSet = new Set();
    for (const s of strategies) {
      if (s.target_assets) {
        for (const coin of s.target_assets) coinSet.add(coin);
      }
    }
    targetCoins = [...coinSet];
  }

  // Phase 2: Fetch everything else in parallel
  const [signals, whales, accountState, sentiment] = await Promise.all([
    targetCoins.length > 0 ? fetchSignals(hl, targetCoins) : {},
    fetchWhalePositions(hl),
    fetchAccountState(hl),
    Promise.resolve(fetchLastSentiments(3)), // sync — last 3 scans (~12h)
  ]);

  // Phase 3: Read MEMORY.md (lessons learned — used with --light-context)
  const memoryLessons = fetchMemoryLessons();

  const result = {
    timestamp: new Date().toISOString(),
    strategies,
    account_state: accountState,
    signals,
    whales,
    sentiment,
    memory_lessons: memoryLessons,
  };

  if (jsonOutput) {
    console.log(JSON.stringify(result, null, 2));
  } else {
    console.log(formatTextOutput(result));
  }
}

main().then(() => process.exit(0)).catch(e => {
  console.error('Error:', e.message);
  process.exit(1);
});
