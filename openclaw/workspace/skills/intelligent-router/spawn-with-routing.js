#!/usr/bin/env node
/**
 * Spawn With Routing v1.1
 *
 * Automatically classifies a task, selects the right model, and spawns a sub-agent.
 * Handles fallback if the primary model fails.
 *
 * v1.1 changes:
 *   - Adds [routed] marker to spawned task prompts so the intelligent-routing
 *     plugin (before_model_resolve hook) skips re-routing for pre-routed subagents.
 *   - Without the marker, the hook would override the model selected here,
 *     causing routing conflicts.
 *
 * Usage:
 *   node spawn-with-routing.js "task description"
 *   node spawn-with-routing.js --tier COMPLEX "task description"
 *   node spawn-with-routing.js --model sonnet "task description"
 *   node spawn-with-routing.js --dry-run "task description"    # just show routing, don't spawn
 *
 * Output: JSON with routing decision and spawn result
 */

const { execSync } = require('child_process');
const path = require('path');
const { route } = require('./intelligent-router-hook');

// Marker prefix: the intelligent-routing plugin checks for this in before_model_resolve.
// When present, the hook returns {} (no override) so the model selected here prevails.
const ROUTED_MARKER = '[routed]';

function spawn(task, model, timeoutMinutes) {
  // Prepend [routed] marker so the plugin hook skips re-routing
  const routedTask = `${ROUTED_MARKER} ${task}`;

  // Build openclaw spawn command
  let cmd = `openclaw spawn`;
  cmd += ` --model "${model}"`;
  if (timeoutMinutes) cmd += ` --timeout ${timeoutMinutes * 60}`;
  cmd += ` --task "${routedTask.replace(/"/g, '\\"')}"`;

  try {
    const output = execSync(cmd, {
      encoding: 'utf-8',
      timeout: 30000,
      stdio: ['pipe', 'pipe', 'pipe']
    });
    return { success: true, output: output.trim() };
  } catch (e) {
    return { success: false, error: e.message };
  }
}

// Timeout heuristic per tier
const TIMEOUT_MAP = {
  SIMPLE: 2,
  MEDIUM: 5,
  COMPLEX: 10,
  REASONING: 30,
  CRITICAL: 60,
};

function main() {
  const args = process.argv.slice(2);
  let task = '';
  let overrideTier = null;
  let overrideModel = null;
  let dryRun = false;
  
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--tier' && args[i + 1]) {
      overrideTier = args[++i];
    } else if (args[i] === '--model' && args[i + 1]) {
      overrideModel = args[++i];
    } else if (args[i] === '--dry-run') {
      dryRun = true;
    } else {
      task += (task ? ' ' : '') + args[i];
    }
  }
  
  if (!task) {
    console.error('Usage: node spawn-with-routing.js [--tier TIER] [--model MODEL] [--dry-run] "task"');
    process.exit(1);
  }
  
  // Route
  const routing = route(task, overrideTier);
  
  // Override model if specified
  if (overrideModel) {
    routing.model = overrideModel;
    routing.modelAlias = overrideModel;
  }
  
  // Handle SIMPLE tier — should be handled directly by Haiku
  if (routing.handleDirectly && !overrideModel) {
    console.log(JSON.stringify({
      action: 'handle_directly',
      routing,
      message: 'SIMPLE tier — handle directly without spawning sub-agent',
    }, null, 2));
    process.exit(0);
  }
  
  if (dryRun) {
    console.log(JSON.stringify({
      action: 'dry_run',
      routing,
      timeout_minutes: TIMEOUT_MAP[routing.tier] || 5,
      spawn_command: buildSpawnCmd(task, routing),
    }, null, 2));
    process.exit(0);
  }
  
  // Try primary model
  const timeout = TIMEOUT_MAP[routing.tier] || 5;
  let result = spawn(task, routing.modelAlias, timeout);
  
  if (result.success) {
    console.log(JSON.stringify({
      action: 'spawned',
      routing,
      model_used: routing.model,
      result: result.output,
    }, null, 2));
    process.exit(0);
  }
  
  // Fallback chain
  for (let i = 0; i < routing.fallbacks.length; i++) {
    const fallbackModel = routing.fallbacks[i];
    console.error(`Primary failed, trying fallback ${i + 1}: ${fallbackModel}`);
    result = spawn(task, fallbackModel, timeout);
    
    if (result.success) {
      console.log(JSON.stringify({
        action: 'spawned_fallback',
        routing,
        model_used: fallbackModel,
        fallback_index: i + 1,
        result: result.output,
      }, null, 2));
      process.exit(0);
    }
  }
  
  // All failed
  console.log(JSON.stringify({
    action: 'all_failed',
    routing,
    error: 'All models in fallback chain failed',
  }, null, 2));
  process.exit(1);
}

function buildSpawnCmd(task, routing) {
  let cmd = `sessions_spawn(task="${ROUTED_MARKER} ${task}", model="${routing.modelAlias}"`;
  cmd += `)`;
  return cmd;
}

main();
