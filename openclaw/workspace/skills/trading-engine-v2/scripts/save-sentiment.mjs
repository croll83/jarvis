#!/usr/bin/env node
/**
 * save-sentiment.mjs — Persist sentiment scan results for the trading daemon.
 *
 * Writes structured JSON to state/sentiment.json (consumed by the daemon's
 * loadSentiment()) and appends a formatted markdown block to workspace-burry
 * memory for agent recall.
 *
 * Usage:
 *   node save-sentiment.mjs --data '{"scores":[...],"macro":[...],"sources":[...]}'
 *   echo '{"scores":[...]}' | node save-sentiment.mjs
 *
 * Input JSON:
 *   {
 *     scores:  [{ coin, score (-100..100), confidence (0..100), trend }],
 *     macro:   ["event1", "event2"],
 *     sources: [{ account, summary }]
 *   }
 */

import { writeFileSync, readFileSync, mkdirSync, existsSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));

// Paths
const STATE_DIR = resolve(__dirname, '..', 'state');
const SENTIMENT_FILE = resolve(STATE_DIR, 'sentiment.json');
const BURRY_MEMORY = '/home/jarvis/.openclaw/workspace-burry/memory';

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', chunk => { data += chunk; });
    process.stdin.on('end', () => resolve(data));
    process.stdin.on('error', reject);
    setTimeout(() => { if (!data) reject(new Error('No stdin data after 10s')); }, 10000);
  });
}

function buildMarkdown(data) {
  const timeStr = new Date().toISOString().slice(11, 16);
  const lines = [`## Sentiment Scan — ${timeStr} UTC`, ''];

  if (data.scores?.length > 0) {
    lines.push('| Coin | Score | Conf | Trend |');
    lines.push('|------|-------|------|-------|');
    for (const s of data.scores) {
      const sc = s.score > 0 ? `+${s.score}` : `${s.score}`;
      lines.push(`| ${s.coin} | ${sc} | ${s.confidence}% | ${s.trend || '-'} |`);
    }
    lines.push('');
  }

  if (data.macro?.length > 0) {
    lines.push('### Macro');
    for (const e of data.macro) lines.push(`- ${e}`);
    lines.push('');
  }

  return lines.join('\n');
}

async function main() {
  // Read input
  const dataArgIdx = process.argv.indexOf('--data');
  let raw;
  if (dataArgIdx >= 0 && process.argv[dataArgIdx + 1]) {
    raw = process.argv[dataArgIdx + 1];
  } else {
    raw = await readStdin();
  }
  if (!raw?.trim()) {
    console.error('Error: no input. Use --data \'{"scores":[...]}\' or pipe JSON to stdin');
    process.exit(1);
  }

  let data;
  try { data = JSON.parse(raw.trim()); } catch (e) {
    console.error(`Error: invalid JSON: ${e.message}`);
    process.exit(1);
  }

  if (!data.scores?.length) {
    console.error('Error: "scores" array required with at least one entry');
    process.exit(1);
  }

  // 1. Write structured JSON for daemon (primary output)
  if (!existsSync(STATE_DIR)) mkdirSync(STATE_DIR, { recursive: true });
  writeFileSync(SENTIMENT_FILE, JSON.stringify({
    timestamp: new Date().toISOString(),
    scores: data.scores,
    macro: data.macro || [],
    sources: (data.sources || []).map(s => ({ account: s.account, summary: s.summary })),
  }, null, 2));
  console.log(`Daemon sentiment: ${SENTIMENT_FILE}`);

  // 2. Append markdown to burry memory (for agent recall)
  try {
    if (!existsSync(BURRY_MEMORY)) mkdirSync(BURRY_MEMORY, { recursive: true });
    const today = new Date().toISOString().split('T')[0];
    const memFile = resolve(BURRY_MEMORY, `${today}-sentiment.md`);
    const md = buildMarkdown(data);
    let existing = existsSync(memFile) ? readFileSync(memFile, 'utf8') : '';
    const sep = existing.length > 0 && !existing.endsWith('\n\n') ? '\n' : '';
    writeFileSync(memFile, existing + sep + md, 'utf8');
    console.log(`Burry memory: ${memFile}`);
  } catch (e) {
    console.error(`Warning: burry memory write failed: ${e.message}`);
  }

  console.log(`Coins: ${data.scores.map(s => s.coin).join(', ')}`);
  console.log(`Macro: ${data.macro?.length || 0} | Sources: ${data.sources?.length || 0}`);
}

main().then(() => process.exit(0)).catch(e => {
  console.error('Error:', e.message);
  process.exit(1);
});
