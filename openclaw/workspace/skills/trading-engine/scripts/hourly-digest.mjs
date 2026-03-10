#!/usr/bin/env node
/**
 * hourly-digest — Read scan buffer and output formatted text for Haiku digest.
 *
 * Reads the JSONL buffer written by save-scan-output.mjs and formats it
 * as readable text for the Haiku LLM to summarize.
 *
 * Usage:
 *   node hourly-digest.mjs             # read today's buffer
 *   node hourly-digest.mjs --clear     # read and delete buffer after output
 *   node hourly-digest.mjs --date 2026-03-04  # read specific date
 *
 * Env:
 *   MEMORY_DIR — override memory directory
 *   WORKSPACE_DIR — override workspace root
 */

import { readFileSync, unlinkSync, existsSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const [,, ...args] = process.argv;
const getArg = (name, def) => {
  const idx = args.indexOf(`--${name}`);
  return idx >= 0 && args[idx + 1] ? args[idx + 1] : def;
};
const hasFlag = (name) => args.includes(`--${name}`);

function getMemoryDir() {
  if (process.env.MEMORY_DIR) return process.env.MEMORY_DIR;
  const workspaceDir = process.env.WORKSPACE_DIR || resolve(__dirname, '..', '..', '..');
  return resolve(workspaceDir, 'memory');
}

async function main() {
  const memoryDir = getMemoryDir();
  const date = getArg('date', new Date().toISOString().split('T')[0]);
  const bufferFile = resolve(memoryDir, 'buffer', `${date}.jsonl`);

  if (!existsSync(bufferFile)) {
    console.log(`No buffer file found for ${date}. Nothing to digest.`);
    console.log('EMPTY_BUFFER');
    return;
  }

  const raw = readFileSync(bufferFile, 'utf8').trim();
  if (!raw) {
    console.log('Buffer file is empty. Nothing to digest.');
    console.log('EMPTY_BUFFER');
    return;
  }

  const entries = raw.split('\n').map((line, i) => {
    try {
      return JSON.parse(line);
    } catch {
      return null;
    }
  }).filter(Boolean);

  if (entries.length === 0) {
    console.log('No valid entries in buffer. Nothing to digest.');
    console.log('EMPTY_BUFFER');
    return;
  }

  // Format output for Haiku
  console.log(`=== SCAN BUFFER (${date}) ===`);
  console.log(`Entries: ${entries.length}`);
  const firstTs = entries[0].ts || 'unknown';
  const lastTs = entries[entries.length - 1].ts || 'unknown';
  console.log(`Period: ${firstTs.slice(11, 16)} — ${lastTs.slice(11, 16)} UTC`);
  console.log('');

  for (const entry of entries) {
    const time = (entry.ts || '').slice(11, 16) || '??:??';
    console.log(`[${time}] Decision: ${entry.decision || 'n/a'}`);
    if (entry.positions) {
      const posStr = Array.isArray(entry.positions) ? entry.positions.join(', ') : entry.positions;
      console.log(`  Positions: ${posStr}`);
    }
    if (entry.signals) {
      console.log(`  Signals: ${entry.signals}`);
    }
    if (entry.reasoning) {
      console.log(`  Reasoning: ${entry.reasoning}`);
    }
    if (entry.trades_executed) {
      const tradesStr = Array.isArray(entry.trades_executed) ? entry.trades_executed.join(', ') : entry.trades_executed;
      console.log(`  TRADES: ${tradesStr}`);
    }
    console.log('');
  }

  // Clear buffer if requested
  if (hasFlag('clear')) {
    unlinkSync(bufferFile);
    console.log(`Buffer cleared: ${bufferFile}`);
  }
}

main().then(() => process.exit(0)).catch(e => {
  console.error('Error:', e.message);
  process.exit(1);
});
