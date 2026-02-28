#!/usr/bin/env node
/**
 * hl-trade — Trading execution tool.
 * Usage: node hl-trade.mjs <command> --coin <COIN> [options]
 *
 * Commands:
 *   market-buy   --coin BTC --size 0.01 [--slippage 0.5]
 *   market-sell  --coin BTC --size 0.01 [--slippage 0.5]
 *   limit-buy    --coin BTC --size 0.01 --price 80000
 *   limit-sell   --coin BTC --size 0.01 --price 90000
 *   close        --coin BTC [--slippage 0.5]
 *   set-leverage --coin BTC --leverage 10 [--mode isolated|cross]
 *   cancel-all   [--coin BTC]
 */

import { HLClient } from './lib/hl-client.mjs';

const [,, cmd, ...args] = process.argv;
const getArg = (name, def) => {
  const idx = args.indexOf(`--${name}`);
  return idx >= 0 && args[idx + 1] ? args[idx + 1] : def;
};

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
      break;
    }
    case 'market-sell': {
      if (!coin) throw new Error('--coin required');
      const size = parseFloat(getArg('size', '0'));
      const slippage = parseFloat(getArg('slippage', '0.5'));
      if (size <= 0) throw new Error('--size must be > 0');
      const result = await hl.marketSell(coin, size, slippage);
      console.log(JSON.stringify({ action: 'market-sell', coin, size, result }, null, 2));
      break;
    }
    case 'limit-buy': {
      if (!coin) throw new Error('--coin required');
      const size = parseFloat(getArg('size', '0'));
      const price = parseFloat(getArg('price', '0'));
      if (size <= 0 || price <= 0) throw new Error('--size and --price required and > 0');
      const result = await hl.limitBuy(coin, size, price);
      console.log(JSON.stringify({ action: 'limit-buy', coin, size, price, result }, null, 2));
      break;
    }
    case 'limit-sell': {
      if (!coin) throw new Error('--coin required');
      const size = parseFloat(getArg('size', '0'));
      const price = parseFloat(getArg('price', '0'));
      if (size <= 0 || price <= 0) throw new Error('--size and --price required and > 0');
      const result = await hl.limitSell(coin, size, price);
      console.log(JSON.stringify({ action: 'limit-sell', coin, size, price, result }, null, 2));
      break;
    }
    case 'close': {
      if (!coin) throw new Error('--coin required');
      const slippage = parseFloat(getArg('slippage', '0.5'));
      const result = await hl.closePosition(coin, slippage);
      console.log(JSON.stringify({ action: 'close', coin, result }, null, 2));
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
