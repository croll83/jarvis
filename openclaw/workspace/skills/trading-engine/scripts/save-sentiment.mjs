#!/usr/bin/env node
/**
 * save-sentiment — Persist sentiment scan results to daily memory file.
 *
 * Reads JSON from --data argument or stdin, and appends a formatted markdown
 * block to memory/YYYY-MM-DD.md in the trader workspace.
 *
 * Usage:
 *   node save-sentiment.mjs --data '{"scores":[...],"macro":[...],"sources":[...]}'
 *   echo '{"scores":[...]}' | node save-sentiment.mjs
 *
 * Input JSON schema:
 *   {
 *     scores:  [{ coin, score (-100..100), confidence (0..100), trend }],
 *     macro:   ["event1", "event2"],            // optional
 *     sources: [{ account, summary }]           // optional
 *   }
 *
 * Env:
 *   MEMORY_DIR — override memory directory (default: {workspace}/memory)
 */

import { writeFileSync, readFileSync, mkdirSync, existsSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

function getMemoryDir() {
  if (process.env.MEMORY_DIR) return process.env.MEMORY_DIR;
  // Default: workspace-trader root is 3 levels up from scripts/
  // scripts/ -> trading-engine/ -> skills/ -> workspace/
  // But on the LXC, the workspace-trader is at ~/.openclaw/workspace-trader/
  // The WORKSPACE_DIR env can override this.
  const workspaceDir = process.env.WORKSPACE_DIR || resolve(__dirname, '..', '..', '..');
  return resolve(workspaceDir, 'memory');
}

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', chunk => { data += chunk; });
    process.stdin.on('end', () => resolve(data));
    process.stdin.on('error', reject);

    // Timeout after 10 seconds if no data
    setTimeout(() => {
      if (!data) reject(new Error('No data received on stdin after 10s'));
    }, 10000);
  });
}

function buildMarkdown(data) {
  const now = new Date();
  const timeStr = now.toISOString().slice(11, 16); // HH:MM

  const lines = [];
  lines.push(`## Sentiment Scan \u2014 ${timeStr} UTC`);
  lines.push('');

  // Scores table
  if (data.scores && data.scores.length > 0) {
    lines.push('### Scores');
    lines.push('| Coin | Score | Confidence | Trend |');
    lines.push('|------|-------|------------|-------|');
    for (const s of data.scores) {
      const scoreStr = s.score > 0 ? `+${s.score}` : `${s.score}`;
      lines.push(`| ${s.coin} | ${scoreStr} | ${s.confidence}% | ${s.trend || 'n/a'} |`);
    }
    lines.push('');
  }

  // Macro events
  if (data.macro && data.macro.length > 0) {
    lines.push('### Macro Events');
    for (const event of data.macro) {
      lines.push(`- ${event}`);
    }
    lines.push('');
  }

  // Raw sources
  if (data.sources && data.sources.length > 0) {
    lines.push('### Sources');
    for (const src of data.sources) {
      lines.push(`- ${src.account}: ${src.summary}`);
    }
    lines.push('');
  }

  return lines.join('\n');
}

async function main() {
  // Read JSON from --data argument or stdin
  const dataArgIdx = process.argv.indexOf('--data');
  let raw;
  if (dataArgIdx >= 0 && process.argv[dataArgIdx + 1]) {
    raw = process.argv[dataArgIdx + 1];
  } else {
    raw = await readStdin();
  }
  if (!raw || !raw.trim()) {
    console.error('Error: no input provided. Use --data \'{"scores":[...]}\' or pipe JSON to stdin');
    process.exit(1);
  }

  let data;
  try {
    data = JSON.parse(raw.trim());
  } catch (e) {
    console.error(`Error: invalid JSON input: ${e.message}`);
    process.exit(1);
  }

  if (!data.scores || !Array.isArray(data.scores) || data.scores.length === 0) {
    console.error('Error: input must contain "scores" array with at least one entry');
    process.exit(1);
  }

  // Build markdown block
  const markdown = buildMarkdown(data);

  // Determine memory file path
  const memoryDir = getMemoryDir();
  const today = new Date().toISOString().split('T')[0]; // YYYY-MM-DD
  const memoryFile = resolve(memoryDir, `${today}.md`);

  // Ensure memory directory exists
  if (!existsSync(memoryDir)) {
    mkdirSync(memoryDir, { recursive: true });
  }

  // Append to file (create if not exists)
  let existing = '';
  if (existsSync(memoryFile)) {
    existing = readFileSync(memoryFile, 'utf8');
  }

  const separator = existing.length > 0 && !existing.endsWith('\n\n') ? '\n' : '';
  writeFileSync(memoryFile, existing + separator + markdown, 'utf8');

  console.log(`Sentiment saved to ${memoryFile} at ${new Date().toISOString()}`);
  console.log(`  Coins: ${data.scores.map(s => s.coin).join(', ')}`);
  console.log(`  Macro events: ${data.macro?.length || 0}`);
  console.log(`  Sources: ${data.sources?.length || 0}`);
}

main().then(() => process.exit(0)).catch(e => {
  console.error('Error:', e.message);
  process.exit(1);
});
