#!/usr/bin/env node
/**
 * risk-check — Cross-strategy pre-trade risk validation.
 *
 * Usage: node risk-check.mjs --coin BTC --direction long --size 0.01 --leverage 10 --strategy strat-scalping
 *
 * Checks:
 *   1. Total exposure across all strategies < max allocation
 *   2. No duplicate position on same coin from different strategies
 *   3. Correlation check (no 3+ same-direction positions in correlated group)
 *   4. BTC crash detection (BTC drops > 5% in 15min → block all new positions)
 *
 * No drawdown protection (removed per user request).
 */

import { HLClient } from './lib/hl-client.mjs';

const CORRELATION_GROUPS = [
  ['BTC', 'ETH'],
  ['SOL', 'AVAX', 'SUI'],
  ['DOGE', 'SHIB', 'WIF', 'PENGU'],
  ['LINK', 'AAVE'],
  ['ARB', 'OP'],
];

const MAX_SAME_DIRECTION_CORRELATED = 2;
const BTC_CRASH_THRESHOLD = -5; // percent
const BTC_CRASH_WINDOW_CANDLES = 15; // 1m candles

// Per-strategy margin limits: Copytrading holds positions for days (whale-driven),
// so it gets a higher margin allowance than fast strategies.
const STRATEGY_MARGIN_LIMITS = {
  'copytrading': 90,
  'scalping': 80,
  'sentiment': 80,
};
const DEFAULT_MARGIN_LIMIT = 80;

const [,, ...args] = process.argv;
const getArg = (name, def) => {
  const idx = args.indexOf(`--${name}`);
  return idx >= 0 && args[idx + 1] ? args[idx + 1] : def;
};

function normalizeCoin(c) {
  return c ? c.replace(/-PERP$/i, '') : c;
}

async function main() {
  const coin = normalizeCoin(getArg('coin', null));
  const direction = getArg('direction', null);
  const size = parseFloat(getArg('size', '0'));
  const leverage = parseInt(getArg('leverage', '1'));
  const strategy = getArg('strategy', 'unknown');

  if (!coin || !direction) {
    console.log('Usage: node risk-check.mjs --coin BTC --direction long --size 0.01 --leverage 10 --strategy strat-scalping');
    process.exit(1);
  }

  const hl = new HLClient();
  const checks = [];
  let approved = true;

  // 1. Get current positions
  const positions = await hl.getPositions();
  const balance = await hl.getBalance();
  const equity = parseFloat(balance.perps.equity);
  const marginUsed = parseFloat(balance.perps.marginUsed);

  // 2. Check duplicate position
  const existingPos = positions.find(p => p.coin === coin);
  if (existingPos) {
    if (existingPos.side.toLowerCase() === direction.toLowerCase()) {
      checks.push({ check: 'duplicate', status: 'warn', msg: `Already ${existingPos.side} ${coin} (size: ${existingPos.size}). Adding to position.` });
    } else {
      checks.push({ check: 'duplicate', status: 'warn', msg: `Opposing position: already ${existingPos.side} ${coin}. This will reduce/flip.` });
    }
  } else {
    checks.push({ check: 'duplicate', status: 'ok', msg: `No existing ${coin} position.` });
  }

  // 3. Correlation check
  const group = CORRELATION_GROUPS.find(g => g.includes(coin));
  if (group) {
    const sameDir = positions.filter(p =>
      group.includes(p.coin) && p.side.toLowerCase() === direction.toLowerCase()
    );
    if (sameDir.length >= MAX_SAME_DIRECTION_CORRELATED) {
      checks.push({
        check: 'correlation',
        status: 'block',
        msg: `Already ${sameDir.length} ${direction} positions in correlated group [${group.join(', ')}]: ${sameDir.map(p => p.coin).join(', ')}. Max ${MAX_SAME_DIRECTION_CORRELATED} allowed.`,
      });
      approved = false;
    } else {
      checks.push({ check: 'correlation', status: 'ok', msg: `Correlation group [${group.join(', ')}]: ${sameDir.length}/${MAX_SAME_DIRECTION_CORRELATED} same-direction positions.` });
    }
  } else {
    checks.push({ check: 'correlation', status: 'ok', msg: `${coin} not in any correlation group.` });
  }

  // 4. Margin usage check
  const strategyLower = (strategy || '').toLowerCase();
  const marginLimit = STRATEGY_MARGIN_LIMITS[strategyLower] || DEFAULT_MARGIN_LIMIT;
  const currentMarginPct = equity > 0 ? (marginUsed / equity * 100) : 100;

  // Block immediately if current margin already exceeds limit
  if (currentMarginPct >= marginLimit) {
    checks.push({ check: 'margin', status: 'block', msg: `Current margin already at ${currentMarginPct.toFixed(1)}% (>=${marginLimit}% limit for ${strategy}). No room for new trades.` });
    approved = false;
  } else {
    const price = await hl.getPrice(coin);
    if (!price || isNaN(price)) {
      checks.push({ check: 'margin', status: 'block', msg: `Could not fetch price for ${coin} — cannot calculate margin. Blocking trade.` });
      approved = false;
    } else {
      const notional = size * price;
      const newMargin = notional / leverage;
      const newMarginPct = ((marginUsed + newMargin) / equity * 100).toFixed(1);
      if (parseFloat(newMarginPct) > marginLimit) {
        checks.push({ check: 'margin', status: 'block', msg: `Margin usage would reach ${newMarginPct}% (>${marginLimit}% limit for ${strategy}).` });
        approved = false;
      } else {
        checks.push({ check: 'margin', status: 'ok', msg: `Margin usage: ${newMarginPct}% after trade (limit: ${marginLimit}% for ${strategy}).` });
      }
    }
  }

  // 5. BTC crash detection
  try {
    const btcCandles = await hl.getCandles('BTC', '1m', BTC_CRASH_WINDOW_CANDLES);
    if (btcCandles.length >= 2) {
      const oldest = btcCandles[0].close;
      const latest = btcCandles[btcCandles.length - 1].close;
      const changePct = ((latest - oldest) / oldest * 100).toFixed(2);
      if (parseFloat(changePct) < BTC_CRASH_THRESHOLD) {
        checks.push({ check: 'btc_crash', status: 'block', msg: `BTC crash detected: ${changePct}% in ${BTC_CRASH_WINDOW_CANDLES} min. All new positions blocked.` });
        approved = false;
      } else {
        checks.push({ check: 'btc_crash', status: 'ok', msg: `BTC ${BTC_CRASH_WINDOW_CANDLES}min change: ${changePct}%` });
      }
    }
  } catch (e) {
    checks.push({ check: 'btc_crash', status: 'warn', msg: `Could not check BTC: ${e.message}` });
  }

  // Result
  const result = {
    coin,
    direction,
    size,
    leverage,
    strategy,
    currentMarginPct: currentMarginPct.toFixed(1),
    approved,
    checks,
  };

  console.log(JSON.stringify(result, null, 2));
  process.exit(approved ? 0 : 1);
}

main().then(() => process.exit(0)).catch(e => { console.error('Error:', e.message); process.exit(1); });
