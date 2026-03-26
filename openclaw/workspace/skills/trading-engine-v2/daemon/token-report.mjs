#!/usr/bin/env node
/**
 * Token Report — Shows token consumption for today and yesterday.
 *
 * Usage:
 *   node daemon/token-report.mjs              # today + yesterday
 *   node daemon/token-report.mjs --days 7     # last 7 days
 *   node daemon/token-report.mjs --json        # machine-readable
 */

import { readFileSync, existsSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const STATE_FILE = resolve(__dirname, '..', 'state', 'token-usage.json');

const args = process.argv.slice(2);
const jsonOutput = args.includes('--json');
const daysArg = (() => {
  const idx = args.indexOf('--days');
  return idx >= 0 && args[idx + 1] ? parseInt(args[idx + 1]) : 2;
})();

function getDateStr(daysAgo = 0) {
  const d = new Date();
  d.setDate(d.getDate() - daysAgo);
  return d.toISOString().split('T')[0];
}

function main() {
  if (!existsSync(STATE_FILE)) {
    console.log(jsonOutput ? '{}' : 'No token usage data found.');
    return;
  }

  const state = JSON.parse(readFileSync(STATE_FILE, 'utf8'));
  const budget = state.alerts?.daily_budget_usd || 10;
  const days = [];

  for (let i = 0; i < daysArg; i++) {
    const dateStr = getDateStr(i);
    const dayData = state.daily?.[dateStr];
    if (!dayData) {
      days.push({ date: dateStr, models: {}, total: 0, budget, pct: 0 });
      continue;
    }

    const models = {};
    let totalCost = 0;
    for (const [key, val] of Object.entries(dayData)) {
      if (key === 'total_cost_usd') continue;
      if (typeof val === 'object' && val.cost_usd != null) {
        models[key] = {
          input: val.input || 0,
          output: val.output || 0,
          calls: val.calls || 0,
          cost: val.cost_usd || 0,
        };
        totalCost += val.cost_usd || 0;
      }
    }

    days.push({
      date: dateStr,
      models,
      total: parseFloat(totalCost.toFixed(4)),
      budget,
      pct: parseFloat((totalCost / budget * 100).toFixed(1)),
    });
  }

  if (jsonOutput) {
    console.log(JSON.stringify(days, null, 2));
    return;
  }

  // Human-readable output
  for (const day of days) {
    const isToday = day.date === getDateStr(0);
    const label = isToday ? `${day.date} (today)` : day.date;
    const bar = '█'.repeat(Math.min(20, Math.round(day.pct / 5))) + '░'.repeat(Math.max(0, 20 - Math.round(day.pct / 5)));

    console.log(`\n📊 ${label}`);
    console.log(`   ${bar} $${day.total.toFixed(2)}/$${day.budget} (${day.pct}%)`);

    if (Object.keys(day.models).length === 0) {
      console.log('   No usage recorded');
      continue;
    }

    // Sort by cost descending
    const sorted = Object.entries(day.models).sort((a, b) => b[1].cost - a[1].cost);
    for (const [model, m] of sorted) {
      const inK = (m.input / 1000).toFixed(1);
      const outK = (m.output / 1000).toFixed(1);
      console.log(`   • ${model}: $${m.cost.toFixed(2)} (${m.calls} calls, ${inK}k in / ${outK}k out)`);
    }
  }

  // Summary if multiple days
  if (days.length > 1) {
    const totalAll = days.reduce((s, d) => s + d.total, 0);
    const avgDaily = totalAll / days.length;
    console.log(`\n📈 ${days.length}-day total: $${totalAll.toFixed(2)} (avg $${avgDaily.toFixed(2)}/day)`);
  }

  console.log('');
}

main();
