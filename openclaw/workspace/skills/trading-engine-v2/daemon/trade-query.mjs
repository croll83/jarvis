#!/usr/bin/env node
/**
 * Trade Query — Query recent trades from the trade log.
 *
 * Usage:
 *   node daemon/trade-query.mjs                        # last 10 trades
 *   node daemon/trade-query.mjs --coin BTC              # filter by coin
 *   node daemon/trade-query.mjs --strategy Scalping     # filter by strategy
 *   node daemon/trade-query.mjs --mode live             # only live trades
 *   node daemon/trade-query.mjs --mode dry-run          # only dry-run
 *   node daemon/trade-query.mjs --last 20 --days 3      # 20 trades, 3 days
 *   node daemon/trade-query.mjs --json                  # machine-readable
 */

import { readFileSync, existsSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const STATE_DIR = resolve(__dirname, '..', 'state');
const TRADE_LOG = resolve(STATE_DIR, 'trade-log.jsonl');
const STRATEGIES_CACHE = resolve(STATE_DIR, 'strategies.json');

const args = process.argv.slice(2);
const getArg = (name, def) => {
  const idx = args.indexOf(`--${name}`);
  return idx >= 0 && args[idx + 1] ? args[idx + 1] : def;
};

const jsonOutput = args.includes('--json');
const coinFilter = getArg('coin', null);
const strategyFilter = getArg('strategy', null);
const modeFilter = getArg('mode', null); // 'live' or 'dry-run'
const limit = parseInt(getArg('last', '10'));
const days = parseInt(getArg('days', '7'));

function loadStrategyNames() {
  try {
    if (!existsSync(STRATEGIES_CACHE)) return {};
    const data = JSON.parse(readFileSync(STRATEGIES_CACHE, 'utf8'));
    const map = {};
    for (const s of data.strategies || []) map[s.id] = s.name;
    return map;
  } catch {}
  return {};
}

function main() {
  if (!existsSync(TRADE_LOG)) {
    console.log(jsonOutput ? '[]' : 'No trade log found.');
    return;
  }

  const cutoff = Date.now() - days * 86400_000;
  const lines = readFileSync(TRADE_LOG, 'utf8').trim().split('\n').filter(Boolean);
  const strategyNames = loadStrategyNames();

  let trades = [];
  for (const line of lines) {
    try {
      const t = JSON.parse(line);
      if (new Date(t.timestamp).getTime() < cutoff) continue;

      // Apply filters
      if (coinFilter && !t.coin?.toLowerCase().includes(coinFilter.toLowerCase())) continue;
      if (modeFilter === 'live' && t.dryRun) continue;
      if (modeFilter === 'dry-run' && !t.dryRun) continue;
      if (strategyFilter) {
        const sName = t.strategyName || strategyNames[t.strategyId] || '';
        if (!sName.toLowerCase().includes(strategyFilter.toLowerCase())) continue;
      }

      // Enrich with strategy name
      if (t.strategyId && !t.strategyName) {
        t.strategyName = strategyNames[t.strategyId] || t.strategyId;
      }

      trades.push(t);
    } catch {}
  }

  // Most recent first, limited
  trades = trades.reverse().slice(0, limit);

  if (trades.length === 0) {
    console.log(jsonOutput ? '[]' : 'No trades match filters.');
    return;
  }

  if (jsonOutput) {
    console.log(JSON.stringify(trades, null, 2));
    return;
  }

  // Human-readable output
  console.log(`\nShowing ${trades.length} trade(s) (last ${days} day${days > 1 ? 's' : ''}):\n`);

  for (const t of trades) {
    const mode = t.dryRun ? '📝' : '💰';
    const pnlStr = t.pnl != null ? ` PnL=$${t.pnl}` : '';
    const strategy = t.strategyName ? ` [${t.strategyName}]` : '';
    const time = t.timestamp ? new Date(t.timestamp).toLocaleString('it-IT', { timeZone: 'Europe/Rome' }) : '?';
    const exec = t.executed ? ' EXEC' : t.dryRun ? ' DRY' : '';

    console.log(`${mode} ${time} | ${t.action} ${t.coin} ${t.side || ''} $${t.size || 0} ${t.leverage || ''}x${strategy}${pnlStr}${exec}`);
    if (t.reason) console.log(`   → ${t.reason}`);
    if (t.error) console.log(`   ⚠ ${t.error}`);
  }
  console.log('');
}

main();
