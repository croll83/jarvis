#!/usr/bin/env node
/**
 * Routing Logger v2.0
 * 
 * Logs routing decisions to state/routing-log.jsonl.
 * Source format: agent:context (e.g. main:telegram, cron:trading-engine, subagent:classifier-fix)
 * 
 * Usage as module:
 *   import { logRouting } from './routing-logger.mjs';
 *   await logRouting({ source: 'cron:trading-engine', taskDescription: '...', tier: 'MEDIUM', modelSelected: 'sonnet' });
 * 
 * Usage as CLI:
 *   node routing-logger.mjs log --source "cron:trading-engine" --task "..." --tier MEDIUM --model sonnet --agent main
 */

import { appendFile, mkdir } from 'fs/promises';
import { dirname, join } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const LOG_FILE = join(__dirname, 'state', 'routing-log.jsonl');

/**
 * Log a routing decision.
 * @param {Object} entry
 * @param {string} [entry.source] - Format: agent:context (e.g. main:telegram, cron:trading-engine)
 * @param {string} [entry.agentId] - Agent identifier (main, family:ada, etc.)
 * @param {string} [entry.jobName] - Job name for cron jobs
 * @param {string} [entry.taskDescription] - Description of the task
 * @param {string} [entry.tier] - SIMPLE, MEDIUM, COMPLEX, CRITICAL
 * @param {string} [entry.modelSelected] - Model used
 * @param {string[]} [entry.fallbacks] - Fallback models
 * @param {number} [entry.confidence] - Classification confidence
 * @param {number} [entry.executionTimeMs] - Execution time in ms
 * @param {boolean} [entry.success] - Whether the routing succeeded
 * @param {number} [entry.costEstimate] - Estimated cost
 * @param {string} [entry.classificationMethod] - How classification was done
 * @param {string} [entry.notes] - Additional notes
 * @returns {Object} The logged entry
 */
export async function logRouting(entry = {}) {
  const logEntry = {
    timestamp: new Date().toISOString(),
    source: entry.source || 'main:direct',
    agentId: entry.agentId || 'main',
    jobName: entry.jobName || '',
    taskDescription: (entry.taskDescription || '').slice(0, 500),
    tier: entry.tier || 'UNKNOWN',
    modelSelected: entry.modelSelected || '',
    fallbacks: entry.fallbacks || [],
    confidence: entry.confidence || 0,
    executionTimeMs: entry.executionTimeMs || 0,
    success: entry.success !== false,
    costEstimate: entry.costEstimate || 0,
    classificationMethod: entry.classificationMethod || '',
    notes: entry.notes || '',
  };

  try {
    await mkdir(dirname(LOG_FILE), { recursive: true });
    await appendFile(LOG_FILE, JSON.stringify(logEntry) + '\n');
  } catch (e) {
    console.error(`[routing-logger] Failed to write log: ${e.message}`);
  }

  return logEntry;
}

// CLI mode
if (process.argv[1] && process.argv[1].includes('routing-logger')) {
  const args = process.argv.slice(2);
  const cmd = args[0];

  function getArg(name) {
    const i = args.indexOf(`--${name}`);
    return i >= 0 && args[i + 1] ? args[i + 1] : null;
  }

  if (cmd === 'log') {
    const entry = await logRouting({
      source: getArg('source') || 'cli:manual',
      agentId: getArg('agent') || 'main',
      jobName: getArg('job') || '',
      taskDescription: getArg('task') || '',
      tier: getArg('tier') || 'UNKNOWN',
      modelSelected: getArg('model') || '',
      notes: getArg('notes') || '',
    });
    console.log(JSON.stringify(entry));
  } else {
    console.log('Usage:');
    console.log('  node routing-logger.mjs log --source "cron:trading-engine" --task "..." --tier MEDIUM --model sonnet');
  }
}
