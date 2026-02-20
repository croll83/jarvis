#!/usr/bin/env node
/**
 * Routing Stats CLI v1.0
 * 
 * Query and analyze routing decisions from state/routing-log.jsonl
 * 
 * Usage:
 *   node scripts/routing-stats.mjs                           # summary
 *   node scripts/routing-stats.mjs --tier MEDIUM --group-by model
 *   node scripts/routing-stats.mjs --source cron --cost
 *   node scripts/routing-stats.mjs --last 100 --json
 *   node scripts/routing-stats.mjs --model sonnet --success-rate
 *   node scripts/routing-stats.mjs --since 2026-02-19
 */

import { readFileSync, existsSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const LOG_PATH = join(__dirname, '..', 'state', 'routing-log.jsonl');

function loadEntries() {
  if (!existsSync(LOG_PATH)) return [];
  return readFileSync(LOG_PATH, 'utf-8').trim().split('\n').filter(Boolean).map(l => JSON.parse(l));
}

function parseArgs() {
  const args = process.argv.slice(2);
  const opts = {};
  for (let i = 0; i < args.length; i++) {
    if (args[i].startsWith('--')) {
      const key = args[i].slice(2);
      if (args[i + 1] && !args[i + 1].startsWith('--')) {
        opts[key] = args[++i];
      } else {
        opts[key] = true;
      }
    }
  }
  return opts;
}

function groupBy(entries, field) {
  const groups = {};
  for (const e of entries) {
    const key = e[field] || 'unknown';
    if (!groups[key]) groups[key] = [];
    groups[key].push(e);
  }
  return groups;
}

const opts = parseArgs();
let entries = loadEntries();

if (entries.length === 0) {
  console.log('No routing log entries found.');
  process.exit(0);
}

// Filters
if (opts.agent) entries = entries.filter(e => e.agentId === opts.agent);
if (opts.source) entries = entries.filter(e => e.source === opts.source);
if (opts.tier) entries = entries.filter(e => e.tier === opts.tier);
if (opts.model) entries = entries.filter(e => e.modelSelected === opts.model || e.modelSelected?.includes(opts.model));
if (opts.job) entries = entries.filter(e => e.jobName?.includes(opts.job));
if (opts.since) entries = entries.filter(e => e.timestamp >= opts.since);
if (opts.last) entries = entries.slice(-parseInt(opts.last));

// Output modes
if (opts.json) {
  console.log(JSON.stringify(entries, null, 2));
  process.exit(0);
}

if (opts['group-by']) {
  const field = opts['group-by'] === 'model' ? 'modelSelected' : opts['group-by'];
  const groups = groupBy(entries, field);
  console.log(`\n📊 Grouped by ${opts['group-by']}:`);
  for (const [key, items] of Object.entries(groups).sort((a, b) => b[1].length - a[1].length)) {
    const cost = items.reduce((s, e) => s + (e.costEstimate || 0), 0);
    const successRate = items.filter(e => e.success).length / items.length * 100;
    console.log(`  ${key}: ${items.length} tasks | $${cost.toFixed(4)} | ${successRate.toFixed(0)}% success`);
  }
  process.exit(0);
}

if (opts.cost) {
  const totalCost = entries.reduce((s, e) => s + (e.costEstimate || 0), 0);
  const byTier = groupBy(entries, 'tier');
  console.log(`\n💰 Cost Analysis (${entries.length} entries):`);
  console.log(`  Total: $${totalCost.toFixed(4)}`);
  for (const [tier, items] of Object.entries(byTier)) {
    const cost = items.reduce((s, e) => s + (e.costEstimate || 0), 0);
    console.log(`  ${tier}: $${cost.toFixed(4)} (${items.length} tasks)`);
  }
  process.exit(0);
}

if (opts['success-rate']) {
  const byModel = groupBy(entries, 'modelSelected');
  console.log(`\n✅ Success Rate by Model:`);
  for (const [model, items] of Object.entries(byModel)) {
    const rate = items.filter(e => e.success).length / items.length * 100;
    console.log(`  ${model}: ${rate.toFixed(1)}% (${items.length} tasks)`);
  }
  process.exit(0);
}

// Default: summary
const bySource = groupBy(entries, 'source');
const byTier = groupBy(entries, 'tier');
const byModel = groupBy(entries, 'modelSelected');
const totalCost = entries.reduce((s, e) => s + (e.costEstimate || 0), 0);
const successRate = entries.filter(e => e.success).length / entries.length * 100;

console.log(`\n🧠 Routing Stats Summary`);
console.log(`${'─'.repeat(40)}`);
console.log(`Total entries: ${entries.length}`);
console.log(`Success rate: ${successRate.toFixed(1)}%`);
console.log(`Total cost: $${totalCost.toFixed(4)}`);
console.log(`\nBy Source:`);
for (const [k, v] of Object.entries(bySource)) console.log(`  ${k}: ${v.length}`);
console.log(`\nBy Tier:`);
for (const [k, v] of Object.entries(byTier)) console.log(`  ${k}: ${v.length}`);
console.log(`\nBy Model:`);
for (const [k, v] of Object.entries(byModel)) console.log(`  ${k}: ${v.length}`);
