#!/usr/bin/env node
/**
 * Cron Routing Wrapper v1.0
 * 
 * Wraps cron job messages to include routing decision + logging.
 * Called at the START of each cron job message as a preamble.
 * 
 * This script:
 * 1. Classifies the task via intelligent-router-hook
 * 2. Logs the routing decision
 * 3. Outputs the routing result for the agent to use
 * 
 * Usage:
 *   node scripts/cron-routing-wrapper.mjs --job "HL Trading Engine" --task "execute hyperliquid trading bot with signals"
 */

import { execSync } from 'child_process';
import { logRouting } from '../routing-logger.mjs';
import { dirname, join } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROUTER_HOOK = join(__dirname, '..', 'skills', 'intelligent-router', 'intelligent-router-hook.js');

const args = process.argv.slice(2);
function getArg(name) {
  const i = args.indexOf(`--${name}`);
  return i >= 0 && args[i + 1] ? args[i + 1] : null;
}

const jobName = getArg('job') || 'unknown';
const taskDesc = getArg('task') || 'cron job execution';

try {
  // Classify
  const output = execSync(`node "${ROUTER_HOOK}" "${taskDesc.replace(/"/g, '\\"')}"`, {
    encoding: 'utf-8',
    timeout: 15000,
  });
  const routing = JSON.parse(output);
  
  // Log
  await logRouting({
    source: `cron:${jobName.toLowerCase().replace(/\s+/g, '-')}`,
    agentId: 'cron',
    jobName,
    taskDescription: taskDesc,
    tier: routing.tier,
    modelSelected: routing.modelAlias || routing.model,
    fallbacks: routing.fallbacks || [],
    confidence: routing.confidence || 0,
    notes: `Routed by cron-routing-wrapper`,
  });
  
  // Output for agent consumption
  console.log(JSON.stringify({
    status: 'routed',
    job: jobName,
    ...routing,
  }));
} catch (e) {
  // Fallback: log failure, continue with default
  await logRouting({
    source: `cron:${jobName.toLowerCase().replace(/\s+/g, '-')}`,
    agentId: 'cron',
    jobName,
    taskDescription: taskDesc,
    tier: 'MEDIUM',
    modelSelected: 'sonnet',
    fallbacks: ['gemini-3-flash', 'grok'],
    confidence: 0,
    success: false,
    notes: `Router failed: ${e.message}`,
  });
  
  console.log(JSON.stringify({
    status: 'fallback',
    job: jobName,
    tier: 'MEDIUM',
    model: 'sonnet',
    error: e.message,
  }));
}
