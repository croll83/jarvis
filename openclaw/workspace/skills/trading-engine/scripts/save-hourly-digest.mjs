#!/usr/bin/env node
/**
 * save-hourly-digest — Append an hourly digest summary to daily memory.
 *
 * Takes markdown text via --data and appends it to memory/YYYY-MM-DD.md
 * with a heading "## Digest HH:00-HH:59 UTC".
 *
 * Usage:
 *   node save-hourly-digest.mjs --data 'Markdown summary text here...'
 *
 * Env:
 *   MEMORY_DIR — override memory directory
 *   WORKSPACE_DIR — override workspace root
 */

import { writeFileSync, readFileSync, mkdirSync, existsSync } from 'fs';
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
  // Read text from --data argument
  const dataArgIdx = process.argv.indexOf('--data');
  let text;
  if (dataArgIdx >= 0 && process.argv[dataArgIdx + 1]) {
    text = process.argv[dataArgIdx + 1];
  }

  if (!text || !text.trim()) {
    console.error('Error: no input provided. Use --data \'Markdown summary text...\'');
    process.exit(1);
  }

  // Determine the hour range for the heading
  const now = new Date();
  const hour = now.getUTCHours();
  // The digest covers the previous hour (since this runs at the top of each hour)
  const prevHour = hour === 0 ? 23 : hour - 1;
  const headingHour = String(prevHour).padStart(2, '0');

  // Build markdown block
  const lines = [];
  lines.push(`## Digest ${headingHour}:00-${headingHour}:59 UTC`);
  lines.push('');
  lines.push(text.trim());
  lines.push('');

  const markdown = lines.join('\n');

  // Determine memory file path
  const memoryDir = getMemoryDir();
  const today = now.toISOString().split('T')[0];
  const memoryFile = resolve(memoryDir, `${today}.md`);

  // Ensure memory directory exists
  if (!existsSync(memoryDir)) {
    mkdirSync(memoryDir, { recursive: true });
  }

  // Append to file (create if not exists)
  let existing = '';
  if (existsSync(memoryFile)) {
    existing = readFileSync(memoryFile, 'utf8');
  } else {
    existing = `# Daily Log — ${today}\n\n`;
  }

  const separator = existing.length > 0 && !existing.endsWith('\n\n') ? '\n' : '';
  writeFileSync(memoryFile, existing + separator + markdown, 'utf8');

  console.log(`Hourly digest saved to ${memoryFile}`);
  console.log(`  Period: ${headingHour}:00-${headingHour}:59 UTC`);
  console.log(`  Size: ${text.length} chars`);
}

main().then(() => process.exit(0)).catch(e => {
  console.error('Error:', e.message);
  process.exit(1);
});
