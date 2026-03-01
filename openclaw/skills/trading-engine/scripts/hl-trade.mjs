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
 * --strategy: Name or entity ID of the Strategy (creates originated_from relation).
 */

import { HLClient } from './lib/hl-client.mjs';

const ONTOLOGY_URL = process.env.ONTOLOGY_URL || 'http://127.0.0.1:8100';
const ONTOLOGY_SPEAKER = process.env.ONTOLOGY_SPEAKER || 'jarvis-agent';

const [,, cmd, ...args] = process.argv;
const getArg = (name, def) => {
  const idx = args.indexOf(`--${name}`);
  return idx >= 0 && args[idx + 1] ? args[idx + 1] : def;
};

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

async function getOwnedAccountId() {
  try {
    const accounts = await ontologyRequest('POST', '/entities/query?type=Account', {
      service: 'jarvis wallet',
    });
    return accounts?.[0]?.id || null;
  } catch { return null; }
}

async function resolveStrategyId(strategyHint) {
  if (!strategyHint) return null;
  // Already an entity ID (stra_xxxx)
  if (strategyHint.startsWith('stra_')) return strategyHint;
  // Resolve by name
  try {
    const strategies = await ontologyRequest('POST', '/entities/query?type=Strategy', {
      name: strategyHint,
    });
    return strategies?.[0]?.id || null;
  } catch { return null; }
}

async function getAgentTradingId() {
  try {
    const agents = await ontologyRequest('POST', '/entities/query?type=Person', {
      name: 'Agent Trading',
    });
    return agents?.[0]?.id || null;
  } catch { return null; }
}

async function createRelation(fromId, relType, toId) {
  try {
    await ontologyRequest('POST', '/relations', {
      from_id: fromId,
      rel_type: relType,
      to_id: toId,
    });
  } catch (e) {
    console.error(`Failed to create relation ${relType}: ${e.message}`);
  }
}

async function logTransaction({ type, coin, size, price, strategy }) {
  try {
    const accountId = await getOwnedAccountId();
    const isBuy = type === 'buy';
    const notional = size * price;
    const entity = {
      type: 'Transaction',
      properties: {
        type,
        account: accountId || 'unknown',
        asset_in: isBuy ? 'USDC' : coin,
        amount_in: isBuy ? parseFloat(notional.toFixed(2)) : size,
        asset_out: isBuy ? coin : 'USDC',
        amount_out: isBuy ? size : parseFloat(notional.toFixed(2)),
        status: 'confirmed',
        timestamp: new Date().toISOString(),
        executor: ONTOLOGY_SPEAKER,
        visibility: 'family',
      },
    };
    const created = await ontologyRequest('POST', '/entities', entity);
    if (!created?.id) return;
    console.log(`Transaction logged: ${created.id}`);

    // Create relations: originated_from Strategy, affects_account Account, executed_by Agent
    const [strategyId, agentId] = await Promise.all([
      resolveStrategyId(strategy),
      getAgentTradingId(),
    ]);
    const relPromises = [];
    if (strategyId) relPromises.push(createRelation(created.id, 'originated_from', strategyId));
    if (accountId) relPromises.push(createRelation(created.id, 'affects_account', accountId));
    if (agentId) relPromises.push(createRelation(created.id, 'executed_by', agentId));
    await Promise.all(relPromises);

    const relCount = [strategyId, accountId, agentId].filter(Boolean).length;
    console.log(`Relations created: ${relCount}/3`);
  } catch (e) {
    console.error(`Failed to log transaction: ${e.message}`);
  }
}

async function main() {
  const hl = new HLClient();
  const coin = getArg('coin', null);

  switch (cmd) {
    case 'market-buy': {
      if (!coin) throw new Error('--coin required');
      const size = parseFloat(getArg('size', '0'));
      const slippage = parseFloat(getArg('slippage', '0.5'));
      if (size <= 0) throw new Error('--size must be > 0');
      const result = await hl.marketBuy(coin, size, slippage);
      console.log(JSON.stringify({ action: 'market-buy', coin, size, result }, null, 2));
      const buyPrice = await hl.getPrice(coin);
      await logTransaction({ type: 'buy', coin, size, price: buyPrice, strategy: getArg('strategy', null) });
      break;
    }
    case 'market-sell': {
      if (!coin) throw new Error('--coin required');
      const size = parseFloat(getArg('size', '0'));
      const slippage = parseFloat(getArg('slippage', '0.5'));
      if (size <= 0) throw new Error('--size must be > 0');
      const result = await hl.marketSell(coin, size, slippage);
      console.log(JSON.stringify({ action: 'market-sell', coin, size, result }, null, 2));
      const sellPrice = await hl.getPrice(coin);
      await logTransaction({ type: 'sell', coin, size, price: sellPrice, strategy: getArg('strategy', null) });
      break;
    }
    case 'limit-buy': {
      if (!coin) throw new Error('--coin required');
      const size = parseFloat(getArg('size', '0'));
      const price = parseFloat(getArg('price', '0'));
      if (size <= 0 || price <= 0) throw new Error('--size and --price required and > 0');
      const result = await hl.limitBuy(coin, size, price);
      console.log(JSON.stringify({ action: 'limit-buy', coin, size, price, result }, null, 2));
      await logTransaction({ type: 'buy', coin, size, price, strategy: getArg('strategy', null) });
      break;
    }
    case 'limit-sell': {
      if (!coin) throw new Error('--coin required');
      const size = parseFloat(getArg('size', '0'));
      const price = parseFloat(getArg('price', '0'));
      if (size <= 0 || price <= 0) throw new Error('--size and --price required and > 0');
      const result = await hl.limitSell(coin, size, price);
      console.log(JSON.stringify({ action: 'limit-sell', coin, size, price, result }, null, 2));
      await logTransaction({ type: 'sell', coin, size, price, strategy: getArg('strategy', null) });
      break;
    }
    case 'close': {
      if (!coin) throw new Error('--coin required');
      const slippage = parseFloat(getArg('slippage', '0.5'));
      const positions = await hl.getPositions();
      const pos = positions.find(p => p.coin === coin);
      const result = await hl.closePosition(coin, slippage);
      console.log(JSON.stringify({ action: 'close', coin, result }, null, 2));
      if (pos) {
        const closeType = pos.side === 'LONG' ? 'sell' : 'buy';
        await logTransaction({ type: closeType, coin, size: pos.size, price: pos.markPrice, strategy: getArg('strategy', null) });
      }
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

main().catch(e => { console.error('Error:', e.message); process.exit(1); });
