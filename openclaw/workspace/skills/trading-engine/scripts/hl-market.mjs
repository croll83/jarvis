#!/usr/bin/env node
/**
 * hl-market — Market data tool.
 * Usage: node hl-market.mjs <command> [options]
 *
 * Commands:
 *   price     --coin BTC (or --all for all mids)
 *   candles   --coin BTC [--interval 5m] [--limit 100]
 *   funding   --coin BTC
 *   meta      (list all available perpetuals)
 *   overview  (top movers, funding extremes)
 */

import { HLClient } from './lib/hl-client.mjs';

const [,, cmd, ...args] = process.argv;
const getArg = (name, def) => {
  const idx = args.indexOf(`--${name}`);
  return idx >= 0 && args[idx + 1] ? args[idx + 1] : def;
};
const hasFlag = (name) => args.includes(`--${name}`);

async function main() {
  const hl = new HLClient();

  switch (cmd) {
    case 'price': {
      if (hasFlag('all')) {
        const prices = await hl.getAllPrices();
        const sorted = Object.entries(prices).sort((a, b) => a[0].localeCompare(b[0]));
        for (const [coin, price] of sorted) {
          console.log(`${coin}: $${parseFloat(price).toLocaleString()}`);
        }
      } else {
        const coin = getArg('coin', null);
        if (!coin) throw new Error('--coin required (or --all)');
        const price = await hl.getPrice(coin);
        console.log(JSON.stringify({ coin, price }, null, 2));
      }
      break;
    }
    case 'candles': {
      const coin = getArg('coin', null);
      if (!coin) throw new Error('--coin required');
      const interval = getArg('interval', '5m');
      const limit = parseInt(getArg('limit', '100'));
      const candles = await hl.getCandles(coin, interval, limit);
      console.log(JSON.stringify({ coin, interval, count: candles.length, candles: candles.slice(-10) }, null, 2));
      break;
    }
    case 'funding': {
      const coin = getArg('coin', null);
      if (!coin) throw new Error('--coin required');
      const data = await hl.getFundingRate(coin);
      if (!data) throw new Error(`No data for ${coin}`);
      const annualized = (data.fundingRate * 3 * 365 * 100).toFixed(2);
      console.log(JSON.stringify({ ...data, annualizedPct: `${annualized}%` }, null, 2));
      break;
    }
    case 'meta': {
      const meta = await hl.getMeta();
      const coins = meta.universe.map(u => u.name);
      console.log(`${coins.length} perpetuals available: ${coins.join(', ')}`);
      break;
    }
    case 'overview': {
      const meta = await hl.getMeta();
      const sdk = await hl.sdk();
      const [metaInfo, contexts] = await sdk.info.perpetuals.getMetaAndAssetCtxs();
      const data = metaInfo.universe.map((u, i) => ({
        coin: u.name,
        price: parseFloat(contexts[i].markPx),
        funding: parseFloat(contexts[i].funding),
        oi: parseFloat(contexts[i].openInterest),
        dayChange: parseFloat(contexts[i].dayNtlVlm || 0),
      }));

      // Top funding (positive = longs paying)
      const topFunding = [...data].sort((a, b) => Math.abs(b.funding) - Math.abs(a.funding)).slice(0, 5);
      console.log('=== Extreme Funding Rates ===');
      for (const d of topFunding) {
        const ann = (d.funding * 3 * 365 * 100).toFixed(1);
        console.log(`  ${d.coin}: ${ann}% annualized (${d.funding > 0 ? 'longs pay' : 'shorts pay'})`);
      }

      // Highest OI
      const topOI = [...data].sort((a, b) => b.oi - a.oi).slice(0, 5);
      console.log('\n=== Highest Open Interest ===');
      for (const d of topOI) {
        console.log(`  ${d.coin}: $${(d.oi * d.price).toLocaleString()} OI`);
      }
      break;
    }
    default:
      console.log('Usage: node hl-market.mjs <price|candles|funding|meta|overview> [options]');
      process.exit(1);
  }
}

main().then(() => process.exit(0)).catch(e => { console.error('Error:', e.message); process.exit(1); });
