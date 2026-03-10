#!/usr/bin/env node
/**
 * whale-monitor — Whale tracking via HL leaderboard + position monitoring.
 *
 * Usage:
 *   node whale-monitor.mjs refresh          — Update top whale list in ontology
 *   node whale-monitor.mjs scan             — Check tracked whales for position changes
 *   node whale-monitor.mjs list             — Show current tracked whales
 *
 * Selection criteria (configurable via env):
 *   WHALE_TOP_N=10            — Number of top whales to track
 *   WHALE_MAX_PORTFOLIO=100000 — Max account value (skill > capital)
 *   WHALE_MIN_PORTFOLIO=5000   — Min account value (filter dust/test accounts)
 *   WHALE_MIN_POSITION=10000   — Min position size to generate signal ($)
 *
 * Requires: ONTOLOGY_URL, ONTOLOGY_SPEAKER (or defaults)
 */

import { HLClient } from './lib/hl-client.mjs';

const ONTOLOGY_URL = process.env.ONTOLOGY_URL || 'http://127.0.0.1:8100';
const ONTOLOGY_SPEAKER = process.env.ONTOLOGY_SPEAKER || 'jarvis-agent';
const WHALE_TOP_N = parseInt(process.env.WHALE_TOP_N || '10');
const WHALE_MAX_PORTFOLIO = parseFloat(process.env.WHALE_MAX_PORTFOLIO || '100000');
const WHALE_MIN_PORTFOLIO = parseFloat(process.env.WHALE_MIN_PORTFOLIO || '5000');
const WHALE_MIN_POSITION = parseFloat(process.env.WHALE_MIN_POSITION || '10000');

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

async function getTrackedWhales() {
  try {
    const resp = await ontologyRequest('POST', '/entities/query?type=Account', {
      service: 'hl_whales',
    });
    return resp || [];
  } catch {
    return [];
  }
}

async function getCopytradingStrategy() {
  try {
    const resp = await ontologyRequest('POST', '/entities/query?type=Strategy', {
      name: 'Copytrading',
    });
    const entities = resp || [];
    return entities[0] || null;
  } catch {
    return null;
  }
}

async function refreshWhaleList() {
  const hl = new HLClient();
  console.log('Fetching HL leaderboard...');

  const leaderboard = await hl.getLeaderboard();
  if (!leaderboard || !Array.isArray(leaderboard)) {
    console.error('Failed to fetch leaderboard');
    process.exit(1);
  }

  // Filter: positive PnL, account < MAX, account > MIN
  const candidates = [];
  for (const entry of leaderboard) {
    const pnl = parseFloat(entry.accountValue || entry.allTimePnl || '0');
    const accountValue = parseFloat(entry.accountValue || '0');
    const displayName = entry.displayName || entry.ethAddress || 'unknown';

    if (accountValue > WHALE_MAX_PORTFOLIO || accountValue < WHALE_MIN_PORTFOLIO) continue;
    if (pnl <= 0) continue;

    candidates.push({
      address: entry.ethAddress,
      displayName,
      accountValue,
      allTimePnl: parseFloat(entry.allTimePnl || '0'),
      windowPnl: parseFloat(entry.windowPnl || entry.allTimePnl || '0'),
    });
  }

  // Sort by all-time PnL descending, take top N
  candidates.sort((a, b) => b.allTimePnl - a.allTimePnl);
  const topWhales = candidates.slice(0, WHALE_TOP_N);

  console.log(`\n=== Top ${topWhales.length} Whales (portfolio $${WHALE_MIN_PORTFOLIO}-$${WHALE_MAX_PORTFOLIO}) ===`);
  for (const w of topWhales) {
    console.log(`  ${w.displayName || w.address.slice(0, 10)}: PnL $${w.allTimePnl.toLocaleString()}, Account $${w.accountValue.toLocaleString()}`);
  }

  // Store in ontology
  const existing = await getTrackedWhales();
  const existingAddrs = new Set(existing.map(e => e.properties?.username));
  const copytradingStrategy = await getCopytradingStrategy();

  for (const whale of topWhales) {
    const entity = {
      type: 'Account',
      properties: {
        service: 'hl_whales',
        username: whale.address,
        display_name: whale.displayName,
        url: `https://app.hyperliquid.xyz/explorer/${whale.address}`,
        visibility: 'private',
      },
    };

    if (existingAddrs.has(whale.address)) {
      // Find and update
      const ex = existing.find(e => e.properties?.username === whale.address);
      if (ex) {
        await ontologyRequest('PATCH', `/entities/${ex.id}`, { properties: entity.properties });
        console.log(`  Updated: ${whale.address.slice(0, 10)}`);
      }
    } else {
      const created = await ontologyRequest('POST', '/entities', entity);
      console.log(`  Created: ${whale.address.slice(0, 10)} (id: ${created.id})`);

      // Link to Copytrading strategy
      if (copytradingStrategy) {
        try {
          await ontologyRequest('POST', '/relations', {
            from_id: created.id,
            rel_type: 'monitored_by',
            to_id: copytradingStrategy.id,
          });
          console.log(`    Linked to Copytrading strategy`);
        } catch (e) {
          console.error(`    Failed to link to strategy: ${e.message}`);
        }
      }
    }
  }

  console.log(`\nWhale list refreshed: ${topWhales.length} whales tracked.`);
  return topWhales;
}

async function scanWhalePositions() {
  const hl = new HLClient();
  const whales = await getTrackedWhales();

  if (whales.length === 0) {
    console.log('No tracked whales. Run "refresh" first.');
    return [];
  }

  console.log(`Scanning ${whales.length} tracked whales...\n`);
  const signals = [];

  for (const whale of whales) {
    const addr = whale.properties?.username;
    if (!addr) continue;

    try {
      const positions = await hl.getPositions(addr);
      const significant = positions.filter(p =>
        p.size * p.entryPrice > WHALE_MIN_POSITION
      );

      if (significant.length > 0) {
        console.log(`Whale ${addr.slice(0, 10)}... (${significant.length} significant positions):`);
        for (const pos of significant) {
          const notional = (pos.size * pos.entryPrice).toFixed(0);
          console.log(`  ${pos.side} ${pos.coin}: $${notional} @ $${pos.entryPrice} (${pos.leverage}x)`);
          signals.push({
            type: 'whale_position',
            whale: addr.slice(0, 10),
            coin: pos.coin,
            side: pos.side,
            notional: parseFloat(notional),
            entryPrice: pos.entryPrice,
            leverage: pos.leverage,
            pnl: pos.pnl,
          });
        }
      }
    } catch (e) {
      console.error(`  Error scanning ${addr.slice(0, 10)}: ${e.message}`);
    }
  }

  if (signals.length === 0) {
    console.log('No significant whale positions detected.');
  } else {
    console.log(`\n=== ${signals.length} whale signals found ===`);
    // Aggregate by coin
    const byCoin = {};
    for (const s of signals) {
      if (!byCoin[s.coin]) byCoin[s.coin] = { long: 0, short: 0, total: 0 };
      byCoin[s.coin][s.side.toLowerCase()] += s.notional;
      byCoin[s.coin].total += s.notional;
    }
    console.log('\nWhale consensus by coin:');
    for (const [coin, data] of Object.entries(byCoin).sort((a, b) => b[1].total - a[1].total)) {
      const bias = data.long > data.short ? 'LONG' : data.short > data.long ? 'SHORT' : 'MIXED';
      console.log(`  ${coin}: ${bias} (long: $${data.long.toLocaleString()}, short: $${data.short.toLocaleString()})`);
    }
  }

  return signals;
}

async function listWhales() {
  const whales = await getTrackedWhales();
  if (whales.length === 0) {
    console.log('No tracked whales. Run "refresh" first.');
    return;
  }
  console.log(`=== ${whales.length} Tracked Whales ===`);
  for (const w of whales) {
    console.log(`  ${w.properties?.username?.slice(0, 16)}... (${w.properties?.service})`);
  }
}

const [,, cmd] = process.argv;

const run = (fn) => fn().then(() => process.exit(0)).catch(e => { console.error('Error:', e.message); process.exit(1); });

switch (cmd) {
  case 'refresh': run(refreshWhaleList); break;
  case 'scan': run(scanWhalePositions); break;
  case 'list': run(listWhales); break;
  default:
    console.log('Usage: node whale-monitor.mjs <refresh|scan|list>');
    process.exit(1);
}
