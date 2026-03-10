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
const hasFlag = (name) => args.includes(`--${name}`);

async function main() {
  const hl = new HLClient({ address });

  switch (cmd) {
    case 'balance': {
      const b = await hl.getBalance(address);
      if (hasFlag('json')) {
        console.log(JSON.stringify(b, null, 2));
      } else {
        console.log(`Overview: $${b.overview}`);
        console.log(`  Perps:   $${b.perps.equity}`);
        console.log(`  Spot (${b.spot.count}): $${b.spot.total}`);
        for (const s of b.spot.balances) {
          console.log(`    ${s.coin}: ${s.amount} ($${s.usdValue.toFixed(2)})`);
        }
        console.log(`  Vault:   $${b.vault}`);
        console.log(`  Staked:  $${b.staked}`);
        if (parseFloat(b.perps.marginUsed) > 0) {
          console.log(`  Margin Used: $${b.perps.marginUsed} (${b.perps.marginRatio})`);
        }
      }
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

main().then(() => process.exit(0)).catch(e => { console.error('Error:', e.message); process.exit(1); });
