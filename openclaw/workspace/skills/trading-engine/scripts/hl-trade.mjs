#!/usr/bin/env node
/**
 * hl-trade — Trading execution tool.
 * Usage: node hl-trade.mjs <command> --coin <COIN> [options]
 *
 * Commands:
 *   market-buy   --coin BTC --size 0.01 --strategy Scalping [--slippage 0.5]
 *   market-sell  --coin BTC --size 0.01 --strategy Scalping [--slippage 0.5]
 *   limit-buy    --coin BTC --size 0.01 --price 80000 --strategy Sentiment
 *   limit-sell   --coin BTC --size 0.01 --price 90000 --strategy Scalping
 *   close        --coin BTC --strategy Scalping [--slippage 0.5]
 *   set-leverage --coin BTC --leverage 10 [--mode isolated|cross]
 *   cancel-all   [--coin BTC]
 *
 * --strategy: Name or entity ID of the Strategy.
 * Each order's oid is logged as TradeAttribution in the ontology,
 * linking the HL order to a strategy. Positions and P&L come from HL
 * directly (source of truth).
 */

import { HLClient } from './lib/hl-client.mjs';

const ONTOLOGY_URL = process.env.ONTOLOGY_URL || 'http://127.0.0.1:8100';
const ONTOLOGY_SPEAKER = process.env.ONTOLOGY_SPEAKER || 'jarvis-agent';

const [,, cmd, ...args] = process.argv;
const getArg = (name, def) => {
  const idx = args.indexOf(`--${name}`);
  return idx >= 0 && args[idx + 1] ? args[idx + 1] : def;
};

function normalizeCoin(c) {
  return c ? c.replace(/-PERP$/, '') : c;
}

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
  if (!resp.ok) return null;
  return resp.json();
}

async function resolveStrategyId(strategyHint) {
  if (!strategyHint) return null;
  if (strategyHint.startsWith('stra_')) return strategyHint;
  try {
    const strategies = await ontologyRequest('POST', '/entities/query?type=Strategy', {
      name: strategyHint,
    });
    return strategies?.[0]?.id || null;
  } catch { return null; }
}

/**
 * Extract oid from HL SDK placeOrder response.
 * Response format: { response: { data: { statuses: [{ filled: { oid } }] } } }
 */
function extractOid(result) {
  const statuses = result?.response?.data?.statuses;
  if (statuses?.[0]) {
    const s = statuses[0];
    return s.filled?.oid ?? s.resting?.oid ?? null;
  }
  return null;
}

/**
 * Log TradeAttribution: links an HL order (oid) to a strategy.
 * This is the ONLY ontology write that hl-trade performs.
 * Dashboard joins HL fills by oid to get per-strategy P&L.
 */
async function logTradeAttribution(coin, oid, strategyId, action) {
  if (!oid || !strategyId) {
    console.error(`TradeAttribution skipped: oid=${oid} strategyId=${strategyId}`);
    return;
  }
  try {
    const created = await ontologyRequest('POST', '/entities', {
      type: 'TradeAttribution',
      properties: {
        oid: String(oid),
        coin,
        strategy_id: strategyId,
        action,
        visibility: 'family',
      },
    });
    if (created?.id) {
      await ontologyRequest('POST', '/relations', {
        from_id: created.id, to_id: strategyId, rel_type: 'belongs_to',
      });
    }
    console.log(`TradeAttribution: ${created?.id} | oid=${oid} | ${coin} → ${strategyId} (${action})`);
  } catch (e) {
    console.error(`logTradeAttribution error: ${e.message}`);
  }
}

async function main() {
  const hl = new HLClient();
  const coin = normalizeCoin(getArg('coin', null));

  const strategy = getArg('strategy', null);
  const needsStrategy = ['market-buy', 'market-sell', 'limit-buy', 'limit-sell', 'close'].includes(cmd);
  if (needsStrategy && !strategy) {
    throw new Error('--strategy required. Pass strategy name or ID (e.g. --strategy Scalping).');
  }

  const strategyId = needsStrategy ? await resolveStrategyId(strategy) : null;
  if (needsStrategy && !strategyId) {
    throw new Error(`Could not resolve strategy "${strategy}". Check ontology.`);
  }

  switch (cmd) {
    case 'market-buy': {
      if (!coin) throw new Error('--coin required');
      const size = parseFloat(getArg('size', '0'));
      const slippage = parseFloat(getArg('slippage', '0.5'));
      if (size <= 0) throw new Error('--size must be > 0');
      const result = await hl.marketBuy(coin, size, slippage);
      const oid = extractOid(result);
      console.log(JSON.stringify({ action: 'market-buy', coin, size, oid, result }, null, 2));
      await logTradeAttribution(coin, oid, strategyId, 'market-buy');
      break;
    }
    case 'market-sell': {
      if (!coin) throw new Error('--coin required');
      const size = parseFloat(getArg('size', '0'));
      const slippage = parseFloat(getArg('slippage', '0.5'));
      if (size <= 0) throw new Error('--size must be > 0');
      const result = await hl.marketSell(coin, size, slippage);
      const oid = extractOid(result);
      console.log(JSON.stringify({ action: 'market-sell', coin, size, oid, result }, null, 2));
      await logTradeAttribution(coin, oid, strategyId, 'market-sell');
      break;
    }
    case 'limit-buy': {
      if (!coin) throw new Error('--coin required');
      const size = parseFloat(getArg('size', '0'));
      const price = parseFloat(getArg('price', '0'));
      if (size <= 0 || price <= 0) throw new Error('--size and --price required and > 0');
      const result = await hl.limitBuy(coin, size, price);
      const oid = extractOid(result);
      console.log(JSON.stringify({ action: 'limit-buy', coin, size, price, oid, result }, null, 2));
      await logTradeAttribution(coin, oid, strategyId, 'limit-buy');
      break;
    }
    case 'limit-sell': {
      if (!coin) throw new Error('--coin required');
      const size = parseFloat(getArg('size', '0'));
      const price = parseFloat(getArg('price', '0'));
      if (size <= 0 || price <= 0) throw new Error('--size and --price required and > 0');
      const result = await hl.limitSell(coin, size, price);
      const oid = extractOid(result);
      console.log(JSON.stringify({ action: 'limit-sell', coin, size, price, oid, result }, null, 2));
      await logTradeAttribution(coin, oid, strategyId, 'limit-sell');
      break;
    }
    case 'close': {
      if (!coin) throw new Error('--coin required');
      const slippage = parseFloat(getArg('slippage', '0.5'));
      const result = await hl.closePosition(coin, slippage);
      const oid = extractOid(result);
      console.log(JSON.stringify({ action: 'close', coin, oid, result }, null, 2));
      await logTradeAttribution(coin, oid, strategyId, 'close');
      break;
    }
    case 'set-leverage': {
      if (!coin) throw new Error('--coin required');
      const leverage = parseInt(getArg('leverage', '0'));
      const mode = getArg('mode', 'isolated');
      if (leverage <= 0) throw new Error('--leverage required and > 0');
      const result = await hl.setLeverage(coin, leverage, mode);
      console.log(JSON.stringify({ action: 'set-leverage', coin, leverage, mode, result }, null, 2));
      break;
    }
    case 'cancel-all': {
      const result = await hl.cancelAll(coin || undefined);
      console.log(JSON.stringify({ action: 'cancel-all', coin: coin || 'all', result }, null, 2));
      break;
    }
    default:
      console.log('Usage: node hl-trade.mjs <market-buy|market-sell|limit-buy|limit-sell|close|set-leverage|cancel-all> --coin <COIN> [options]');
      process.exit(1);
  }
}

main().then(() => process.exit(0)).catch(e => { console.error('Error:', e.message); process.exit(1); });
