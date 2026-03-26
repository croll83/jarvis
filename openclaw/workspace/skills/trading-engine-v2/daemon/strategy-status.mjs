#!/usr/bin/env node
/**
 * Strategy Status — Lists all strategies with config and performance.
 *
 * Usage:
 *   node daemon/strategy-status.mjs                  # all strategies
 *   node daemon/strategy-status.mjs --status active   # filter by status
 *   node daemon/strategy-status.mjs --json            # machine-readable
 */

import { StrategyManager } from '../lib/strategy-manager.mjs';

const args = process.argv.slice(2);
const jsonOutput = args.includes('--json');
const statusFilter = (() => {
  const idx = args.indexOf('--status');
  return idx >= 0 && args[idx + 1] ? args[idx + 1] : null;
})();

async function main() {
  const sm = new StrategyManager();
  await sm.load();

  let strategies = sm.getAll();
  if (statusFilter) {
    strategies = strategies.filter(s => s.status === statusFilter);
  }

  if (strategies.length === 0) {
    console.log(jsonOutput ? '[]' : 'No strategies found.');
    return;
  }

  if (jsonOutput) {
    console.log(JSON.stringify(strategies.map(s => ({
      id: s.id,
      name: s.name,
      status: s.status,
      config: s.config,
      performance: s.performance,
      target_assets: s.target_assets,
    })), null, 2));
    return;
  }

  for (const s of strategies) {
    const c = s.config;
    const p = s.performance;
    const statusEmoji = { active: '🟢', 'dry-run': '🟡', paused: '🔴' }[s.status] || '⚪';

    console.log(`\n${statusEmoji} ${s.name} (${s.status})`);
    console.log(`   ID: ${s.id}`);
    console.log(`   Weights: technical=${c.weights?.technical || 0}% sentiment=${c.weights?.sentiment || 0}% whales=${c.weights?.whales || 0}%`);
    console.log(`   Hold: ${c.hold_time?.min || '?'}-${c.hold_time?.max || '?'} ${c.hold_time?.unit || 'min'}`);
    console.log(`   Leverage: ${c.leverage?.base || 3}-${c.leverage?.max || 5}x`);
    console.log(`   Risk: TP=${c.tp_pct || 2}% SL=${c.sl_pct || 2}% max_pos=${c.max_positions || 3}`);

    if (s.target_assets?.length > 0) {
      console.log(`   Assets: ${s.target_assets.join(', ')}`);
    } else {
      console.log(`   Assets: all`);
    }

    if (p) {
      const pnlEmoji = (p.pnl || 0) >= 0 ? '+' : '';
      console.log(`   Performance: PnL $${pnlEmoji}${p.pnl || 0} | WR ${p.winRate || 0}% | ${p.trades || 0} trades (${p.wins || 0}W)`);
    } else {
      console.log(`   Performance: no data`);
    }
  }
  console.log('');
}

main().catch(e => {
  console.error('Error:', e.message);
  process.exit(1);
});
