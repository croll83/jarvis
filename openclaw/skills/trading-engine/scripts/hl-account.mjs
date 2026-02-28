#!/usr/bin/env node
/**
 * hl-account — Account information tool.
 * Usage: node hl-account.mjs <command> [--address <addr>]
 * Commands: balance, positions, fills [--limit N], orders
 */

import { HLClient } from './lib/hl-client.mjs';

const [,, cmd, ...args] = process.argv;
const getArg = (name, def) => {
  const idx = args.indexOf(`--${name}`);
  return idx >= 0 && args[idx + 1] ? args[idx + 1] : def;
};
const address = getArg('address', undefined);

async function main() {
  const hl = new HLClient({ address });

  switch (cmd) {
    case 'balance': {
      const b = await hl.getBalance(address);
      console.log(JSON.stringify(b, null, 2));
      break;
    }
    case 'positions': {
      const positions = await hl.getPositions(address);
      if (positions.length === 0) {
        console.log('No open positions.');
      } else {
        console.log(JSON.stringify(positions, null, 2));
      }
      break;
    }
    case 'fills': {
      const limit = parseInt(getArg('limit', '20'));
      const fills = await hl.getFills(limit);
      console.log(JSON.stringify(fills, null, 2));
      break;
    }
    case 'orders': {
      const orders = await hl.getOrders();
      if (orders.length === 0) {
        console.log('No open orders.');
      } else {
        console.log(JSON.stringify(orders, null, 2));
      }
      break;
    }
    default:
      console.log('Usage: node hl-account.mjs <balance|positions|fills|orders> [--address <addr>] [--limit N]');
      process.exit(1);
  }
}

main().catch(e => { console.error('Error:', e.message); process.exit(1); });
