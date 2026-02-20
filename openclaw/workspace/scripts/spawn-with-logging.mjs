#!/usr/bin/env node
/**
 * Spawn with Routing Logging v1.0
 * 
 * Wrapper for sub-agent spawns that logs routing decisions.
 * 
 * Usage as module:
 *   import { spawnWithRouting, logMainRouting } from './spawn-with-logging.mjs';
 *   await spawnWithRouting("complex task description", { tierOverride: "COMPLEX" });
 *   await logMainRouting("simple question", "SIMPLE", "haiku", true);
 * 
 * Usage as CLI (for testing):
 *   node scripts/spawn-with-logging.mjs route --task "analyze code" 
 *   node scripts/spawn-with-logging.mjs log-main --task "what time is it" --tier SIMPLE --model haiku --direct
 */

import { execSync } from 'child_process';
import { logRouting } from '../routing-logger.mjs';
import { dirname, join } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROUTER_HOOK = join(__dirname, '..', 'skills', 'intelligent-router', 'intelligent-router-hook.js');

/**
 * Classify a task via the intelligent router hook.
 * Returns { tier, model, modelAlias, fallbacks, thinking, confidence, handleDirectly }
 */
function classify(taskDesc, tierOverride) {
  const tierArg = tierOverride ? `--tier ${tierOverride}` : '';
  const escaped = taskDesc.replace(/"/g, '\\"');
  const output = execSync(
    `node "${ROUTER_HOOK}" ${tierArg} "${escaped}"`,
    { encoding: 'utf-8', timeout: 15000 }
  );
  return JSON.parse(output);
}

/**
 * Log a routing decision for a sub-agent spawn.
 * Call this BEFORE spawning the sub-agent, then call logSpawnOutcome after.
 */
export async function logSubagentRouting(taskDesc, routing, jobName = '', agentId = 'main') {
  return logRouting({
    source: `subagent:${jobName || taskDesc.slice(0, 30).toLowerCase().replace(/\s+/g, '-')}`,
    agentId,
    jobName,
    taskDescription: taskDesc,
    tier: routing.tier,
    modelSelected: routing.modelAlias || routing.model,
    fallbacks: routing.fallbacks || [],
    confidence: routing.confidence || 0,
    notes: `Sub-agent spawn: model=${routing.modelAlias}, thinking=${routing.thinking || 'off'}`,
  });
}

/**
 * Log outcome of a sub-agent spawn (success/failure).
 */
export async function logSpawnOutcome(taskDesc, routing, { success = true, executionTimeMs = 0, notes = '', agentId = 'main' } = {}) {
  return logRouting({
    source: `subagent-outcome:${taskDesc.slice(0, 30).toLowerCase().replace(/\s+/g, '-')}`,
    agentId,
    taskDescription: taskDesc,
    tier: routing.tier,
    modelSelected: routing.modelAlias || routing.model,
    success,
    executionTimeMs,
    notes: notes || (success ? 'Sub-agent completed' : 'Sub-agent failed'),
  });
}

/**
 * Full spawn flow with routing + logging.
 * Returns { routing, logged } — caller handles actual sessions_spawn().
 * 
 * Why not spawn here? Because sessions_spawn is an OpenClaw agent tool,
 * not callable from Node. This prepares everything and logs the decision.
 */
export async function spawnWithRouting(taskDesc, { tierOverride, jobName = '', agentId = 'main' } = {}) {
  let routing;
  try {
    routing = classify(taskDesc, tierOverride);
  } catch (e) {
    // Fallback on classifier failure
    routing = {
      tier: tierOverride || 'MEDIUM',
      model: 'anthropic/claude-sonnet-4-6',
      modelAlias: 'sonnet',
      fallbacks: ['gemini-3-flash', 'grok'],
      thinking: null,
      confidence: 0,
      handleDirectly: false,
      classifierFailed: true,
    };
  }

  const logged = await logSubagentRouting(taskDesc, routing, jobName, agentId);

  return {
    routing,
    logged,
    spawnCommand: routing.thinking
      ? `sessions_spawn(task="${taskDesc}", model="${routing.modelAlias}", thinking="${routing.thinking}")`
      : `sessions_spawn(task="${taskDesc}", model="${routing.modelAlias}")`,
  };
}

/**
 * Log a main session routing decision (Haiku handling directly or delegating).
 */
export async function logMainRouting(taskDesc, tier, model, handleDirectly = false, agentId = 'main') {
  return logRouting({
    source: `main:${handleDirectly ? 'direct' : 'delegation'}`,
    agentId,
    taskDescription: taskDesc,
    tier,
    modelSelected: model,
    confidence: 1.0,
    notes: handleDirectly
      ? 'Haiku handled directly (SIMPLE)'
      : `Delegated to ${model} via sub-agent`,
  });
}

// CLI mode
if (process.argv[1] && process.argv[1].includes('spawn-with-logging')) {
  const args = process.argv.slice(2);
  const cmd = args[0];

  function getArg(name) {
    const i = args.indexOf(`--${name}`);
    return i >= 0 && args[i + 1] ? args[i + 1] : null;
  }
  const hasFlag = (name) => args.includes(`--${name}`);

  if (cmd === 'route') {
    const task = getArg('task') || 'test task';
    const tier = getArg('tier') || undefined;
    const agent = getArg('agent') || 'main';
    const result = await spawnWithRouting(task, { tierOverride: tier, agentId: agent });
    console.log(JSON.stringify(result, null, 2));
  } else if (cmd === 'log-main') {
    const task = getArg('task') || 'test task';
    const tier = getArg('tier') || 'SIMPLE';
    const model = getArg('model') || 'haiku';
    const direct = hasFlag('direct');
    const agent = getArg('agent') || 'main';
    const entry = await logMainRouting(task, tier, model, direct, agent);
    console.log('Logged:', JSON.stringify(entry));
  } else {
    console.log('Usage:');
    console.log('  node spawn-with-logging.mjs route --task "..." [--tier COMPLEX]');
    console.log('  node spawn-with-logging.mjs log-main --task "..." --tier SIMPLE --model haiku [--direct]');
  }
}
