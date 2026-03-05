#!/usr/bin/env node
/**
 * save-scan-output — Append Market Scan output to a JSONL buffer.
 *
 * The buffer is consumed hourly by the Hourly Digest cron (Haiku)
 * which synthesizes entries into a compact summary for daily memory.
 *
 * Usage:
 *   node save-scan-output.mjs --data '{"decision":"HOLD","positions":["BTC SHORT"],"reasoning":"..."}'
 *
 * Each call appends one JSON line to: memory/buffer/YYYY-MM-DD.jsonl
 *
 * Env:
 *   MEMORY_DIR — override memory directory (default: {workspace}/memory)
 *   WORKSPACE_DIR — override workspace root
 */

import { appendFileSync, mkdirSync, existsSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

function getMemoryDir() {
  if (process.env.MEMORY_DIR) return process.env.MEMORY_DIR;
  const workspaceDir = process.env.WORKSPACE_DIR || resolve(__dirname, '..', '..', '..');
  return resolve(workspaceDir, 'memory');
}

async function main() {
  // Read JSON from --data argument
  const dataArgIdx = process.argv.indexOf('--data');
  let raw;
  if (dataArgIdx >= 0 && process.argv[dataArgIdx + 1]) {
    raw = process.argv[dataArgIdx + 1];
  }

  if (!raw || !raw.trim()) {
    console.error('Error: no input provided. Use --data \'{"decision":"...","reasoning":"..."}\'');
    process.exit(1);
  }

  let data;
  try {
    data = JSON.parse(raw.trim());
  } catch (e) {
    console.error(`Error: invalid JSON input: ${e.message}`);
    process.exit(1);
  }

  // Add timestamp
  data.ts = new Date().toISOString();

  // Determine buffer file path
  const memoryDir = getMemoryDir();
  const bufferDir = resolve(memoryDir, 'buffer');
  const today = new Date().toISOString().split('T')[0];
  const bufferFile = resolve(bufferDir, `${today}.jsonl`);

  // Ensure buffer directory exists
  if (!existsSync(bufferDir)) {
    mkdirSync(bufferDir, { recursive: true });
  }

  // Append JSON line
  appendFileSync(bufferFile, JSON.stringify(data) + '\n', 'utf8');

  console.log(`Scan output saved to ${bufferFile}`);
  console.log(`  Decision: ${data.decision || 'n/a'}`);
}

main().then(() => process.exit(0)).catch(e => {
  console.error('Error:', e.message);
  process.exit(1);
});
