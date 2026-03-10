#!/usr/bin/env node
/**
 * daily-report — Generate daily trading portfolio report.
 * Usage: node daily-report.mjs [--json]
 *
 * Outputs: account summary, open positions, strategy performance, recent trades.
 */

import { HLClient } from './lib/hl-client.mjs';

const [,, ...args] = process.argv;
const hasFlag = (name) => args.includes(`--${name}`);

const ONTOLOGY_URL = process.env.ONTOLOGY_URL || 'http://127.0.0.1:8100';
const ONTOLOGY_SPEAKER = process.env.ONTOLOGY_SPEAKER || 'jarvis-agent';

async function ontologyRequest(method, path) {
  const resp = await fetch(`${ONTOLOGY_URL}${path}`, {
    method,
    headers: {
      'Content-Type': 'application/json',
      'X-Speaker-Id': ONTOLOGY_SPEAKER,
      'Authorization': `Bearer ${process.env.ONTOLOGY_API_TOKEN || ''}`,
    },
  });
  if (!resp.ok) return null;
  return resp.json();
}

async function main() {
  const hl = new HLClient();

  // Gather data in parallel
  const [balance, positions, fills, strategies] = await Promise.all([
    hl.getBalance(),
    hl.getPositions(),
    hl.getFills(50),
    ontologyRequest('GET', '/entities?type=Strategy').catch(() => null),
  ]);

  // Today's fills
  const today = new Date().toISOString().split('T')[0];
  const todayFills = fills.filter(f => f.time.startsWith(today));
  const todayPnl = todayFills.reduce((sum, f) => sum + parseFloat(f.pnl || 0), 0);

  const report = {
    timestamp: new Date().toISOString(),
    account: balance,
    positions: {
      count: positions.length,
      details: positions,
      totalUnrealizedPnl: positions.reduce((sum, p) => sum + parseFloat(p.pnl), 0).toFixed(2),
    },
    today: {
      trades: todayFills.length,
      realizedPnl: todayPnl.toFixed(2),
    },
    strategies: [],
  };

  // Parse strategy data
  const stratList = strategies?.entities || strategies || [];
  for (const s of stratList) {
    const p = s.properties || {};
    report.strategies.push({
      id: s.id,
      name: p.name,
      status: p.status,
      budget: p.budget_limit,
    });
  }

  if (hasFlag('json')) {
    console.log(JSON.stringify(report, null, 2));
    return;
  }

  // Formatted output
  console.log('=== JARVIS Trading Report ===');
  console.log(`Date: ${today}`);
  console.log('');
  console.log('--- Account Overview ---');
  console.log(`  Total: $${balance.overview}`);
  console.log(`  Perps:   $${balance.perps.equity}`);
  console.log(`  Spot (${balance.spot.count}): $${balance.spot.total}`);
  for (const s of balance.spot.balances) {
    console.log(`    ${s.coin}: ${s.amount} ($${s.usdValue.toFixed(2)})`);
  }
  console.log(`  Vault:   $${balance.vault}`);
  console.log(`  Staked:  $${balance.staked}`);
  if (parseFloat(balance.perps.marginUsed) > 0) {
    console.log(`  Margin Used: $${balance.perps.marginUsed} (${balance.perps.marginRatio})`);
  }
  console.log('');

  if (positions.length > 0) {
    console.log(`--- Open Positions (${positions.length}) ---`);
    for (const p of positions) {
      const pnlSign = parseFloat(p.pnl) >= 0 ? '+' : '';
      console.log(`  ${p.side} ${p.coin}: size ${p.size} @ $${p.entryPrice} | PnL: ${pnlSign}$${p.pnl} | ${p.leverage}x`);
    }
    console.log(`  Total Unrealized PnL: $${report.positions.totalUnrealizedPnl}`);
  } else {
    console.log('--- No Open Positions ---');
  }

  console.log('');
  console.log("--- Today's Activity ---");
  console.log(`  Trades: ${report.today.trades}`);
  console.log(`  Realized PnL: $${report.today.realizedPnl}`);

  if (report.strategies.length > 0) {
    console.log('');
    console.log('--- Strategies ---');
    for (const s of report.strategies) {
      console.log(`  [${s.status}] ${s.name} (budget: ${s.budget}%)`);
    }
  }
}

main().then(() => process.exit(0)).catch(e => { console.error('Error:', e.message); process.exit(1); });
