#!/usr/bin/env node
/**
 * postmortem — Analyze closed trades and update strategy performance.
 * Usage: node postmortem.mjs [--days 1] [--json]
 *
 * Reads recent fills, classifies trades, calculates per-strategy win rates.
 */

import { HLClient } from './lib/hl-client.mjs';

const ONTOLOGY_URL = process.env.ONTOLOGY_URL || 'http://127.0.0.1:8100';
const ONTOLOGY_SPEAKER = process.env.ONTOLOGY_SPEAKER || 'jarvis-agent';

const [,, ...args] = process.argv;
const getArg = (name, def) => {
  const idx = args.indexOf(`--${name}`);
  return idx >= 0 && args[idx + 1] ? args[idx + 1] : def;
};
const hasFlag = (name) => args.includes(`--${name}`);

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

async function main() {
  const days = parseInt(getArg('days', '1'));
  const jsonOutput = hasFlag('json');

  const hl = new HLClient();
  const fills = await hl.getFills(200);

  // Filter to requested time window
  const cutoff = Date.now() - days * 24 * 60 * 60 * 1000;
  const recentFills = fills.filter(f => new Date(f.time).getTime() > cutoff);

  // Group by coin to find round-trip trades (buy → sell or sell → buy)
  const closedTrades = recentFills.filter(f => parseFloat(f.pnl || '0') !== 0);

  if (closedTrades.length === 0) {
    console.log(`No closed trades in the last ${days} day(s).`);
    return;
  }

  // Analyze
  const summary = {
    period: `${days} day(s)`,
    totalTrades: closedTrades.length,
    totalPnl: 0,
    wins: 0,
    losses: 0,
    avgWin: 0,
    avgLoss: 0,
    bestTrade: null,
    worstTrade: null,
    byCoin: {},
  };

  const winPnls = [];
  const lossPnls = [];

  for (const t of closedTrades) {
    const pnl = parseFloat(t.pnl);
    summary.totalPnl += pnl;

    if (pnl > 0) {
      summary.wins++;
      winPnls.push(pnl);
    } else {
      summary.losses++;
      lossPnls.push(pnl);
    }

    if (!summary.bestTrade || pnl > parseFloat(summary.bestTrade.pnl)) summary.bestTrade = t;
    if (!summary.worstTrade || pnl < parseFloat(summary.worstTrade.pnl)) summary.worstTrade = t;

    if (!summary.byCoin[t.coin]) summary.byCoin[t.coin] = { pnl: 0, trades: 0 };
    summary.byCoin[t.coin].pnl += pnl;
    summary.byCoin[t.coin].trades++;
  }

  summary.totalPnl = parseFloat(summary.totalPnl.toFixed(2));
  summary.winRate = summary.totalTrades > 0 ? parseFloat((summary.wins / summary.totalTrades * 100).toFixed(1)) : 0;
  summary.avgWin = winPnls.length > 0 ? parseFloat((winPnls.reduce((a, b) => a + b, 0) / winPnls.length).toFixed(2)) : 0;
  summary.avgLoss = lossPnls.length > 0 ? parseFloat((lossPnls.reduce((a, b) => a + b, 0) / lossPnls.length).toFixed(2)) : 0;

  if (jsonOutput) {
    console.log(JSON.stringify(summary, null, 2));
    return;
  }

  console.log(`=== Trade Postmortem (last ${days} day${days > 1 ? 's' : ''}) ===`);
  console.log(`Total Trades: ${summary.totalTrades} (${summary.wins}W / ${summary.losses}L)`);
  console.log(`Win Rate: ${summary.winRate}%`);
  console.log(`Total PnL: $${summary.totalPnl}`);
  console.log(`Avg Win: +$${summary.avgWin} | Avg Loss: $${summary.avgLoss}`);

  if (summary.bestTrade) {
    console.log(`Best: ${summary.bestTrade.coin} ${summary.bestTrade.side} +$${summary.bestTrade.pnl}`);
  }
  if (summary.worstTrade) {
    console.log(`Worst: ${summary.worstTrade.coin} ${summary.worstTrade.side} $${summary.worstTrade.pnl}`);
  }

  console.log('\nBy Coin:');
  for (const [coin, data] of Object.entries(summary.byCoin).sort((a, b) => b[1].pnl - a[1].pnl)) {
    console.log(`  ${coin}: $${data.pnl.toFixed(2)} (${data.trades} trades)`);
  }
}

main().catch(e => { console.error('Error:', e.message); process.exit(1); });
