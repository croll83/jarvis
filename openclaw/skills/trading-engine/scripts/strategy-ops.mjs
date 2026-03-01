#!/usr/bin/env node
/**
 * strategy-ops — Strategy CRUD via ontology.
 *
 * Usage:
 *   node strategy-ops.mjs list                          — List all strategies
 *   node strategy-ops.mjs get --id <id>                 — Get strategy details
 *   node strategy-ops.mjs create --name X --config '{}'  — Create new strategy
 *   node strategy-ops.mjs update --id <id> --status active|paused
 *   node strategy-ops.mjs perf --id <id> --pnl X --trades N --wins W
 *   node strategy-ops.mjs init                          — Bootstrap default strategies
 */

const ONTOLOGY_URL = process.env.ONTOLOGY_URL || 'http://127.0.0.1:8100';
const ONTOLOGY_SPEAKER = process.env.ONTOLOGY_SPEAKER || 'jarvis-agent';

async function ontologyRequest(method, path, body = null) {
  const opts = {
    method,
    headers: {
      'Content-Type': 'application/json',
      'X-Speaker-Id': ONTOLOGY_SPEAKER,
      'Authorization': `Bearer ${process.env.ONTOLOGY_API_TOKEN || ''}`,
    },
  };
  if (body) opts.body = JSON.stringify(body);
  const resp = await fetch(`${ONTOLOGY_URL}${path}`, opts);
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`Ontology ${method} ${path}: ${resp.status} ${text}`);
  }
  return resp.json();
}

const DEFAULT_STRATEGIES = [
  {
    name: 'Scalping',
    description: 'Fast in/out trades targeting 1-2% PnL. High frequency, technical-driven.',
    topic: 'crypto-trading',
    target_assets: ['BTC', 'ETH', 'SOL', 'DOGE', 'AVAX', 'SUI', 'LINK', 'ARB', 'HYPE', 'WIF', 'INJ', 'TIA', 'ONDO', 'PENGU', 'AAVE', 'ENA'],
    status: 'active',
    visibility: 'private',
    rules: [
      { trigger: 'signal_score > 15', action: 'open LONG' },
      { trigger: 'signal_score < -15', action: 'open SHORT' },
      { trigger: 'pnl >= 1.5%', action: 'take profit' },
      { trigger: 'pnl <= -ATR*1.5', action: 'stop loss' },
    ],
    budget_limit: 40,
    _config: {
      weights: { technical: 70, sentiment: 25, whales: 5 },
      timeframe: '5m',
      hold_time: { min: 5, max: 60, unit: 'minutes' },
      leverage: { base: 5, max: 10, dynamic: true },
      max_positions: 3,
      tp_pct: 1.5,
      sl_mode: 'atr',
      sl_atr_multiplier: 1.5,
      trailing_stop: true,
    },
  },
  {
    name: 'Sentiment',
    description: 'Narrative-driven trades based on Twitter sentiment and market mood. Medium frequency.',
    topic: 'crypto-trading',
    target_assets: ['BTC', 'ETH', 'SOL', 'DOGE', 'HYPE', 'SUI', 'LINK', 'AVAX', 'WIF', 'PENGU'],
    status: 'paused',
    visibility: 'private',
    rules: [
      { trigger: 'sentiment_score > 40 AND confidence > 50', action: 'open LONG' },
      { trigger: 'sentiment_score < -40 AND confidence > 50', action: 'open SHORT' },
      { trigger: 'pnl >= 5%', action: 'take profit' },
      { trigger: 'pnl <= -3%', action: 'stop loss' },
    ],
    budget_limit: 30,
    _config: {
      weights: { technical: 10, sentiment: 70, whales: 20 },
      timeframe: '15m',
      hold_time: { min: 60, max: 1440, unit: 'minutes' },
      leverage: { base: 3, max: 5, dynamic: false },
      max_positions: 2,
      tp_pct: 5,
      sl_pct: 3,
      trailing_stop: true,
      min_sentiment_confidence: 50,
      sentiment_window_hours: 24,
    },
  },
  {
    name: 'Copytrading',
    description: 'Follow top HL whale traders. Low frequency, high conviction.',
    topic: 'crypto-trading',
    target_assets: [],
    status: 'active',
    visibility: 'private',
    rules: [
      { trigger: 'whale opens position AND technical_score > -20', action: 'mirror position' },
      { trigger: 'whale closes position', action: 'close mirrored position' },
      { trigger: 'pnl >= 10% OR whale exits', action: 'take profit' },
      { trigger: 'pnl <= -5%', action: 'stop loss' },
    ],
    budget_limit: 30,
    _config: {
      weights: { technical: 25, sentiment: 10, whales: 70 },
      timeframe: '1h',
      hold_time: { min: 60, max: 2880, unit: 'minutes' },
      leverage: { base: 5, max: 10, mirror_whale: true },
      max_positions: 3,
      tp_pct: 10,
      sl_pct: 5,
      trailing_stop: true,
      whale_min_position_usd: 10000,
    },
  },
];

async function listStrategies() {
  const resp = await ontologyRequest('GET', '/entities?type=Strategy');
  const strategies = resp.entities || resp || [];
  if (strategies.length === 0) {
    console.log('No strategies found. Run "init" to bootstrap defaults.');
    return;
  }
  console.log(`=== ${strategies.length} Strategies ===`);
  for (const s of strategies) {
    const p = s.properties;
    console.log(`  [${s.id}] ${p.name} — ${p.status} (budget: ${p.budget_limit}%)`);
    if (p.description) console.log(`    ${p.description}`);
  }
}

async function getStrategy(id) {
  const strategy = await ontologyRequest('GET', `/entities/${id}`);
  console.log(JSON.stringify(strategy, null, 2));
}

async function updateStrategy(id, updates) {
  const result = await ontologyRequest('PATCH', `/entities/${id}`, { properties: updates });
  console.log(`Strategy ${id} updated:`, JSON.stringify(updates));
  return result;
}

async function updatePerformance(id, pnl, trades, wins) {
  const strategy = await ontologyRequest('GET', `/entities/${id}`);
  const props = strategy.properties || {};
  const currentRules = props.rules || [];

  // Performance metrics stored as description addendum
  const perfNote = `\n[Performance: PnL $${pnl}, Trades: ${trades}, Wins: ${wins}, WinRate: ${trades > 0 ? (wins / trades * 100).toFixed(1) : 0}%, Updated: ${new Date().toISOString()}]`;
  const desc = (props.description || '').replace(/\n\[Performance:.*\]/, '') + perfNote;

  await ontologyRequest('PATCH', `/entities/${id}`, { properties: { description: desc } });
  console.log(`Performance updated for strategy ${id}: PnL=$${pnl}, ${wins}/${trades} trades`);
}

async function initStrategies() {
  console.log('Bootstrapping default strategies...\n');
  for (const strat of DEFAULT_STRATEGIES) {
    const { _config, ...properties } = strat;
    // Store config as JSON string in description
    properties.description = `${properties.description}\n\n---CONFIG---\n${JSON.stringify(_config, null, 2)}`;

    const entity = { type: 'Strategy', properties };
    try {
      const created = await ontologyRequest('POST', '/entities', entity);
      console.log(`Created: ${strat.name} (id: ${created.id}, status: ${strat.status})`);
    } catch (e) {
      console.error(`Failed to create ${strat.name}: ${e.message}`);
    }
  }
  console.log('\nDone. Use "list" to see all strategies.');
}

const [,, cmd, ...cmdArgs] = process.argv;
const getArg = (name, def) => {
  const idx = cmdArgs.indexOf(`--${name}`);
  return idx >= 0 && cmdArgs[idx + 1] ? cmdArgs[idx + 1] : def;
};

const run = (fn) => fn().then(() => process.exit(0)).catch(e => { console.error(e.message); process.exit(1); });

switch (cmd) {
  case 'list': run(listStrategies); break;
  case 'get': {
    const id = getArg('id', null);
    if (!id) { console.log('--id required'); process.exit(1); }
    run(() => getStrategy(id));
    break;
  }
  case 'update': {
    const id = getArg('id', null);
    const status = getArg('status', null);
    if (!id) { console.log('--id required'); process.exit(1); }
    const updates = {};
    if (status) updates.status = status;
    run(() => updateStrategy(id, updates));
    break;
  }
  case 'perf': {
    const id = getArg('id', null);
    const pnl = getArg('pnl', '0');
    const trades = getArg('trades', '0');
    const wins = getArg('wins', '0');
    if (!id) { console.log('--id required'); process.exit(1); }
    run(() => updatePerformance(id, pnl, parseInt(trades), parseInt(wins)));
    break;
  }
  case 'init': run(initStrategies); break;
  default:
    console.log('Usage: node strategy-ops.mjs <list|get|update|perf|init>');
    process.exit(1);
}
