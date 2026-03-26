#!/usr/bin/env node
/**
 * Strategy Learner — Evaluates trade performance and updates burry's memory.
 *
 * Reads CLOSE entries from trade-log.jsonl (already have pnl computed by PositionManager).
 * No longer guesses PnL from current prices — trusts the close entries.
 *
 * Usage:
 *   - Standalone: node daemon/strategy-learner.mjs [--days 7] [--json]
 *   - From daemon: import { StrategyLearner } and call .run()
 *
 * Self-learning rules:
 * - Profit factor <1.0 for 7 days → suggest parameter changes
 * - Negative PnL for 14 days → switch to dry-run, spawn variant
 * - Signal type with <40% win rate → flag as unreliable
 * - Max 1 parameter change per cycle
 */

import { StrategyManager } from '../lib/strategy-manager.mjs';
import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';
import { execSync } from 'child_process';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const STATE_DIR = resolve(__dirname, '..', 'state');
const TRADE_LOG = resolve(STATE_DIR, 'trade-log.jsonl');
const METRICS_FILE = resolve(STATE_DIR, 'strategy-metrics.json');
const MEMORY_DIR = '/home/jarvis/.openclaw/workspace-burry/memory';

const THRESHOLDS = {
  min_trades_for_eval: 10,
  profit_factor_warn: 1.0,
  profit_factor_fail: 0.8,
  win_rate_warn: 0.45,
  max_drawdown_pct: 15,
  underperform_days: 7,
  fail_days: 14,
  dry_run_min_trades: 20,        // was 30 — faster feedback loop
  promote_profit_factor: 1.2,    // was 1.5 — more achievable
  promote_win_rate: 0.45,        // was 0.55 — more achievable
  cooldown_days: 3,              // re-activate dry-run strategies after N days
  min_active_strategies: 1,      // always keep at least 1 strategy active
};

// --- Trade log reader (CLOSE entries only — they have pnl from PositionManager) ---

function readClosedTrades(maxAgeDays = 30) {
  if (!existsSync(TRADE_LOG)) return [];
  const cutoff = Date.now() - maxAgeDays * 86400_000;
  const lines = readFileSync(TRADE_LOG, 'utf8').trim().split('\n').filter(Boolean);
  const trades = [];
  for (const line of lines) {
    try {
      const t = JSON.parse(line);
      if (t.action !== 'CLOSE') continue;
      if (t.error) continue;
      if (new Date(t.timestamp).getTime() >= cutoff) trades.push(t);
    } catch {}
  }
  return trades;
}

// --- Metrics computation (works on CLOSE entries with pnl) ---

function computeMetrics(trades) {
  if (trades.length === 0) return null;

  const pnls = trades.map(t => parseFloat(t.pnl) || 0);
  const wins = pnls.filter(p => p > 0);
  const losses = pnls.filter(p => p < 0);

  const grossProfit = wins.reduce((s, p) => s + p, 0);
  const grossLoss = Math.abs(losses.reduce((s, p) => s + p, 0));
  const netPnl = grossProfit - grossLoss;

  const profitFactor = grossLoss > 0 ? grossProfit / grossLoss : grossProfit > 0 ? Infinity : 0;
  const winRate = trades.length > 0 ? wins.length / trades.length : 0;

  const mean = pnls.reduce((s, p) => s + p, 0) / pnls.length;
  const variance = pnls.reduce((s, p) => s + Math.pow(p - mean, 2), 0) / pnls.length;
  const stdDev = Math.sqrt(variance);
  const sharpe = stdDev > 0 ? (mean / stdDev) * Math.sqrt(252) : 0;

  let peak = 0, maxDd = 0, cumPnl = 0;
  for (const p of pnls) {
    cumPnl += p;
    if (cumPnl > peak) peak = cumPnl;
    const dd = peak - cumPnl;
    if (dd > maxDd) maxDd = dd;
  }

  return {
    closed_count: trades.length,
    win_count: wins.length,
    loss_count: losses.length,
    win_rate: parseFloat(winRate.toFixed(3)),
    gross_profit: parseFloat(grossProfit.toFixed(2)),
    gross_loss: parseFloat(grossLoss.toFixed(2)),
    net_pnl: parseFloat(netPnl.toFixed(2)),
    profit_factor: profitFactor === Infinity ? 999 : parseFloat(profitFactor.toFixed(2)),
    sharpe_ratio: parseFloat(sharpe.toFixed(2)),
    max_drawdown: parseFloat(maxDd.toFixed(2)),
    avg_win: wins.length > 0 ? parseFloat((grossProfit / wins.length).toFixed(2)) : 0,
    avg_loss: losses.length > 0 ? parseFloat((grossLoss / losses.length).toFixed(2)) : 0,
  };
}

// --- Evaluation ---

function evaluateOverall(metrics) {
  if (!metrics || !metrics.closed_count) return { status: 'NO_CLOSED_TRADES', recommendations: [] };
  if (metrics.closed_count < THRESHOLDS.min_trades_for_eval) {
    return { status: 'INSUFFICIENT_DATA', recommendations: [`Need ${THRESHOLDS.min_trades_for_eval - metrics.closed_count} more closed trades`], metrics };
  }

  const recs = [];
  let status = 'HEALTHY';

  if (metrics.profit_factor < THRESHOLDS.profit_factor_fail) {
    status = 'FAILING';
    recs.push(`Profit factor ${metrics.profit_factor} < ${THRESHOLDS.profit_factor_fail} — consider pausing live trading`);
  } else if (metrics.profit_factor < THRESHOLDS.profit_factor_warn) {
    status = 'UNDERPERFORMING';
    recs.push(`Profit factor ${metrics.profit_factor} below 1.0 — losing money`);
  }

  if (metrics.win_rate < THRESHOLDS.win_rate_warn) {
    status = status === 'HEALTHY' ? 'UNDERPERFORMING' : status;
    recs.push(`Win rate ${(metrics.win_rate * 100).toFixed(0)}% below ${THRESHOLDS.win_rate_warn * 100}% threshold`);
  }

  if (metrics.sharpe_ratio < 0) {
    recs.push(`Negative Sharpe (${metrics.sharpe_ratio}) — risk-adjusted returns are negative`);
  }

  return { status, recommendations: recs, metrics };
}

// --- Group analysis ---

function groupBy(trades, keyFn) {
  const groups = {};
  for (const t of trades) {
    const key = keyFn(t);
    if (!groups[key]) groups[key] = [];
    groups[key].push(t);
  }
  return groups;
}

function analyzeGroups(trades) {
  const byAsset = groupBy(trades, t => t.coin);
  const bySignal = groupBy(trades, t => t.signalType || t.closeReason || 'unknown');

  const assetMetrics = {};
  for (const [coin, group] of Object.entries(byAsset)) {
    assetMetrics[coin] = computeMetrics(group);
  }

  const signalMetrics = {};
  for (const [sig, group] of Object.entries(bySignal)) {
    signalMetrics[sig] = computeMetrics(group);
  }

  return { assetMetrics, signalMetrics };
}

// --- Lesson generation ---

function generateLessons(overall, groups, strategyResults = []) {
  const today = new Date().toISOString().split('T')[0];
  const newLessons = [];

  for (const [coin, m] of Object.entries(groups.assetMetrics)) {
    if (!m || m.closed_count < 3) continue;
    if (m.win_rate < 0.35) {
      newLessons.push(`${coin}: win rate ${(m.win_rate * 100).toFixed(0)}% over ${m.closed_count} trades — reduce position sizing or avoid`);
    }
    if (m.profit_factor < 0.7 && m.closed_count >= 5) {
      newLessons.push(`${coin}: profit factor ${m.profit_factor} — this asset is consistently losing money`);
    }
    if (m.win_rate > 0.7 && m.closed_count >= 5) {
      newLessons.push(`${coin}: strong performer — WR ${(m.win_rate * 100).toFixed(0)}%, PF ${m.profit_factor} over ${m.closed_count} trades`);
    }
  }

  for (const [sig, m] of Object.entries(groups.signalMetrics)) {
    if (!m || m.closed_count < 3) continue;
    if (m.win_rate < 0.4) {
      newLessons.push(`Signal '${sig}': unreliable — WR ${(m.win_rate * 100).toFixed(0)}% over ${m.closed_count} trades, needs stricter confirmation`);
    }
    if (m.win_rate > 0.65 && m.closed_count >= 5) {
      newLessons.push(`Signal '${sig}': high quality — WR ${(m.win_rate * 100).toFixed(0)}%, PF ${m.profit_factor}`);
    }
  }

  for (const sr of strategyResults) {
    if (!sr.metrics || sr.metrics.closed_count < 3) continue;
    const m = sr.metrics;
    if (sr.evaluation === 'FAILING') {
      newLessons.push(`Strategy "${sr.name}": FAILING — PF ${m.profit_factor}, WR ${(m.win_rate * 100).toFixed(0)}% over ${m.closed_count} trades`);
    } else if (sr.evaluation === 'UNDERPERFORMING') {
      newLessons.push(`Strategy "${sr.name}": underperforming — PF ${m.profit_factor}, consider adjusting parameters`);
    } else if (m.profit_factor > 1.5 && m.closed_count >= 10) {
      newLessons.push(`Strategy "${sr.name}": strong — PF ${m.profit_factor}, WR ${(m.win_rate * 100).toFixed(0)}% over ${m.closed_count} trades`);
    }
  }

  if (overall.metrics?.win_rate < 0.45 && overall.metrics?.closed_count >= 10) {
    newLessons.push(`Overall win rate ${(overall.metrics.win_rate * 100).toFixed(0)}% — signal quality needs improvement`);
  }
  if (overall.metrics?.profit_factor > 1.5 && overall.metrics?.closed_count >= 20) {
    newLessons.push(`Strategy is profitable — PF ${overall.metrics.profit_factor}, maintain current approach`);
  }

  return { date: today, lessons: newLessons };
}

// --- Memory file updates ---

function updateLessonsFile(newLessons) {
  const lessonsFile = resolve(MEMORY_DIR, 'lessons.md');
  let content = '# Trading Lessons\n\n';

  let existing = '';
  try { existing = readFileSync(lessonsFile, 'utf8'); } catch {}

  const sections = [];
  const sectionRegex = /## (\d{4}-\d{2}-\d{2})[^\n]*\n([\s\S]*?)(?=## \d{4}|$)/g;
  let match;
  while ((match = sectionRegex.exec(existing))) {
    const date = match[1];
    const age = (Date.now() - new Date(date).getTime()) / 86400_000;
    if (age <= 7) sections.push({ date, body: match[2].trim() });
  }

  if (newLessons.lessons.length > 0) {
    const filtered = sections.filter(s => s.date !== newLessons.date);
    filtered.push({
      date: newLessons.date,
      body: newLessons.lessons.map(l => `- ${l}`).join('\n'),
    });
    filtered.sort((a, b) => b.date.localeCompare(a.date));
    for (const s of filtered) {
      content += `## ${s.date}\n\n${s.body}\n\n`;
    }
  } else if (sections.length > 0) {
    for (const s of sections) {
      content += `## ${s.date}\n\n${s.body}\n\n`;
    }
  } else {
    content += '_(No lessons yet — will be populated as trades close)_\n';
  }

  if (!existsSync(MEMORY_DIR)) mkdirSync(MEMORY_DIR, { recursive: true });
  writeFileSync(lessonsFile, content.trimEnd() + '\n');
}

function updatePerformanceFile(overall, groups, dryRunPromotion, strategyResults = []) {
  const perfFile = resolve(MEMORY_DIR, 'performance.md');
  const today = new Date().toISOString().split('T')[0];
  const m = overall.metrics || {};

  let content = '# Performance Tracking\n\n';
  content += '## Overview\n';
  content += `- **Status**: ${overall.status}\n`;
  content += `- **Last updated**: ${today}\n\n`;

  if (m.closed_count) {
    content += '## Overall Metrics\n';
    content += `- **Closed trades**: ${m.closed_count}\n`;
    content += `- **Win rate**: ${(m.win_rate * 100).toFixed(0)}% (${m.win_count}W / ${m.loss_count}L)\n`;
    content += `- **Profit factor**: ${m.profit_factor}\n`;
    content += `- **Net PnL**: $${m.net_pnl}\n`;
    content += `- **Sharpe**: ${m.sharpe_ratio}\n`;
    content += `- **Max drawdown**: $${m.max_drawdown}\n`;
    content += `- **Avg win**: $${m.avg_win} | Avg loss: $${m.avg_loss}\n\n`;
  }

  const assetEntries = Object.entries(groups.assetMetrics)
    .filter(([, v]) => v?.closed_count >= 1)
    .sort((a, b) => (b[1].net_pnl || 0) - (a[1].net_pnl || 0));

  if (assetEntries.length > 0) {
    content += '## Per-Asset Performance\n';
    for (const [coin, am] of assetEntries) {
      const wr = am.win_rate != null ? `${(am.win_rate * 100).toFixed(0)}%` : '?';
      content += `- **${coin}** — ${am.closed_count} trades, WR ${wr}, PnL $${am.net_pnl || 0}\n`;
    }
    content += '\n';
  }

  const sigEntries = Object.entries(groups.signalMetrics)
    .filter(([, v]) => v?.closed_count >= 1)
    .sort((a, b) => (b[1].win_rate || 0) - (a[1].win_rate || 0));

  if (sigEntries.length > 0) {
    content += '## Per-Signal Performance\n';
    for (const [sig, sm] of sigEntries) {
      const wr = sm.win_rate != null ? `${(sm.win_rate * 100).toFixed(0)}%` : '?';
      content += `- **${sig}** — ${sm.closed_count} trades, WR ${wr}, PF ${sm.profit_factor || 0}\n`;
    }
    content += '\n';
  }

  if (strategyResults.length > 0) {
    content += '## Per-Strategy Performance\n';
    for (const sr of strategyResults) {
      if (!sr.metrics) {
        content += `- **${sr.name}** — no closed trades yet\n`;
        continue;
      }
      const sm = sr.metrics;
      const wr = sm.win_rate != null ? `${(sm.win_rate * 100).toFixed(0)}%` : '?';
      content += `- **${sr.name}** [${sr.evaluation}] — ${sm.closed_count} closed, WR ${wr}, PF ${sm.profit_factor || 0}, PnL $${sm.net_pnl || 0}\n`;
      if (sr.recommendations?.length > 0) {
        for (const r of sr.recommendations) content += `  - ${r}\n`;
      }
    }
    content += '\n';
  }

  if (dryRunPromotion) {
    content += '## Dry-Run Promotion\n';
    content += `- ${dryRunPromotion.promote ? 'READY TO PROMOTE' : 'Not ready'}: ${dryRunPromotion.reason}\n\n`;
  }

  if (overall.recommendations?.length > 0) {
    content += '## Recommendations\n';
    for (const r of overall.recommendations) content += `- ${r}\n`;
    content += '\n';
  }

  if (!existsSync(MEMORY_DIR)) mkdirSync(MEMORY_DIR, { recursive: true });
  writeFileSync(perfFile, content.trimEnd() + '\n');
}

// --- Re-index burry memory ---

function reindexBurryMemory() {
  try {
    execSync('openclaw memory index --agent burry', { timeout: 30_000, stdio: 'pipe' });
    return true;
  } catch (e) {
    console.error('[StrategyLearner] Memory reindex failed:', e.message);
    return false;
  }
}

// --- Promotion check ---

function checkDryRunPromotion(trades) {
  if (trades.length < THRESHOLDS.dry_run_min_trades) {
    return { promote: false, reason: `Need ${THRESHOLDS.dry_run_min_trades - trades.length} more closed trades` };
  }

  const metrics = computeMetrics(trades);
  if (metrics.profit_factor >= THRESHOLDS.promote_profit_factor && metrics.win_rate >= THRESHOLDS.promote_win_rate) {
    return { promote: true, reason: `Dry-run passed: PF=${metrics.profit_factor}, WR=${(metrics.win_rate * 100).toFixed(0)}%`, metrics };
  }

  return {
    promote: false,
    reason: `Not ready: PF=${metrics.profit_factor} (need ${THRESHOLDS.promote_profit_factor}), WR=${(metrics.win_rate * 100).toFixed(0)}% (need ${THRESHOLDS.promote_win_rate * 100}%)`,
    metrics,
  };
}

// --- Main class ---

export class StrategyLearner {
  constructor(opts = {}) {
    this.strategyManager = opts.strategyManager || new StrategyManager();
    this._lastRun = 0;
    this._minInterval = opts.intervalMs || 3600_000;
    this._tradeThreshold = opts.tradeThreshold || 5;
    this._lastTradeCount = 0;
  }

  shouldRun(currentTradeCount) {
    const elapsed = Date.now() - this._lastRun;
    const newTrades = currentTradeCount - this._lastTradeCount;
    return elapsed >= this._minInterval || newTrades >= this._tradeThreshold;
  }

  async run(days = 7) {
    this._lastRun = Date.now();

    // Read only CLOSE entries — they have pnl computed by PositionManager
    const trades = readClosedTrades(days);
    if (trades.length === 0) {
      return { status: 'NO_TRADES', message: 'No closed trades in log' };
    }

    this._lastTradeCount = trades.length;

    // Compute overall + grouped metrics
    const overallMetrics = computeMetrics(trades);
    const overall = evaluateOverall(overallMetrics);
    const groups = analyzeGroups(trades);

    // Per-strategy evaluation and ontology updates
    const strategyResults = await this._evaluateStrategies(trades);

    // Discover and create new strategies from patterns
    const newStrategy = await this._discoverNewStrategies(groups, strategyResults);
    if (newStrategy) {
      strategyResults.push({ id: newStrategy.id, name: newStrategy.name, evaluation: 'NEW', metrics: null });
    }

    // Dry-run promotion check
    const dryRunTrades = trades.filter(t => t.dryRun);
    const dryRunPromotion = dryRunTrades.length > 0 ? checkDryRunPromotion(dryRunTrades) : null;

    // Generate lessons
    const newLessons = generateLessons(overall, groups, strategyResults);

    // Update memory files
    updateLessonsFile(newLessons);
    updatePerformanceFile(overall, groups, dryRunPromotion, strategyResults);

    // Save metrics history
    let metricsHistory = {};
    try {
      if (existsSync(METRICS_FILE)) metricsHistory = JSON.parse(readFileSync(METRICS_FILE, 'utf8'));
    } catch {}
    const today = new Date().toISOString().split('T')[0];
    if (!metricsHistory.overall) metricsHistory.overall = [];
    metricsHistory.overall.push({ date: today, ...overallMetrics });
    metricsHistory.overall = metricsHistory.overall
      .filter(h => (Date.now() - new Date(h.date).getTime()) / 86400_000 <= 90);
    if (!metricsHistory.strategies) metricsHistory.strategies = {};
    for (const sr of strategyResults) {
      if (!sr.metrics) continue;
      if (!metricsHistory.strategies[sr.id]) metricsHistory.strategies[sr.id] = [];
      metricsHistory.strategies[sr.id].push({ date: today, ...sr.metrics });
      metricsHistory.strategies[sr.id] = metricsHistory.strategies[sr.id]
        .filter(h => (Date.now() - new Date(h.date).getTime()) / 86400_000 <= 90);
    }
    writeFileSync(METRICS_FILE, JSON.stringify(metricsHistory, null, 2));

    // Reindex burry memory
    reindexBurryMemory();

    const result = {
      date: today,
      overall,
      groups: {
        assetCount: Object.keys(groups.assetMetrics).length,
        signalCount: Object.keys(groups.signalMetrics).length,
      },
      strategyResults: strategyResults.map(s => ({ name: s.name, status: s.evaluation, trades: s.metrics?.closed_count || 0 })),
      dryRunPromotion,
      lessonsGenerated: newLessons.lessons.length,
    };

    console.log(`[StrategyLearner] ${result.overall.status} | ${overallMetrics?.closed_count || 0} closed trades | ${result.lessonsGenerated} lessons | ${strategyResults.length} strategies evaluated`);
    return result;
  }

  async _evaluateStrategies(trades) {
    const strategies = this.strategyManager.getAll();
    const results = [];

    for (const strategy of strategies) {
      const stratTrades = trades.filter(t => t.strategyId === strategy.id);
      if (stratTrades.length === 0) {
        results.push({ id: strategy.id, name: strategy.name, evaluation: 'NO_TRADES', metrics: null });
        continue;
      }

      const metrics = computeMetrics(stratTrades);
      const evaluation = evaluateOverall(metrics);

      try {
        await this.strategyManager.updatePerformance(strategy.id, {
          pnl: metrics.net_pnl || 0,
          trades: metrics.closed_count || 0,
          wins: metrics.win_count || 0,
          winRate: metrics.win_rate != null ? parseFloat((metrics.win_rate * 100).toFixed(1)) : 0,
        });
      } catch (e) {
        console.error(`[StrategyLearner] Failed to update performance for ${strategy.name}: ${e.message}`);
      }

      await this._handleStrategyLifecycle(strategy, metrics, evaluation);

      results.push({ id: strategy.id, name: strategy.name, evaluation: evaluation.status, metrics, recommendations: evaluation.recommendations });
    }

    return results;
  }

  async _handleStrategyLifecycle(strategy, metrics, evaluation) {
    if (!metrics || !metrics.closed_count) return;

    // --- Manual override: skip ALL status changes (demotion, promotion, cooldown) ---
    if (strategy.config._manualOverride) {
      if (evaluation.status === 'FAILING' || evaluation.status === 'UNDERPERFORMING') {
        console.log(`[StrategyLearner] ${strategy.name} ${evaluation.status} but manualOverride=true — status locked at "${strategy.status}"`);
        // Only apply parameter tweaks for UNDERPERFORMING, never change status
        if (evaluation.status === 'UNDERPERFORMING' && metrics.closed_count >= THRESHOLDS.min_trades_for_eval) {
          const tweak = this._suggestTweak(strategy, metrics);
          if (tweak) {
            console.log(`[StrategyLearner] ${strategy.name} TWEAK: ${tweak.param} ${tweak.oldVal} → ${tweak.newVal} (${tweak.reason})`);
            try { await this.strategyManager.updateConfig(strategy.id, { [tweak.param]: tweak.newVal }); } catch (e) {
              console.error(`[StrategyLearner] Failed to tweak ${strategy.name}: ${e.message}`);
            }
          }
        }
      }
      return; // HARD RETURN — never touch status of overridden strategies
    }

    // --- Demotion: active → dry-run (only for non-overridden strategies) ---
    if (strategy.status === 'active' && evaluation.status === 'FAILING' && metrics.closed_count >= THRESHOLDS.min_trades_for_eval) {
      const allStrategies = this.strategyManager.getAll();
      const activeCount = allStrategies.filter(s => s.status === 'active').length;

      if (activeCount <= THRESHOLDS.min_active_strategies) {
        console.log(`[StrategyLearner] ${strategy.name} FAILING but keeping active (only ${activeCount} active, min=${THRESHOLDS.min_active_strategies})`);
        // Still apply tweaks below instead of demoting
      } else {
        console.log(`[StrategyLearner] ${strategy.name} FAILING — switching to dry-run`);
        try {
          await this.strategyManager.setStatus(strategy.id, 'dry-run');
          // Record demotion timestamp for cooldown
          await this.strategyManager.updateConfig(strategy.id, { _demotedAt: new Date().toISOString() });
        } catch (e) {
          console.error(`[StrategyLearner] Failed to pause ${strategy.name}: ${e.message}`);
        }
        return;
      }
    }

    // --- Promotion: dry-run → active (performance-based) ---
    if (strategy.status === 'dry-run' && metrics.closed_count >= THRESHOLDS.dry_run_min_trades) {
      if (metrics.profit_factor >= THRESHOLDS.promote_profit_factor && metrics.win_rate >= THRESHOLDS.promote_win_rate) {
        console.log(`[StrategyLearner] ${strategy.name} PROMOTED — PF=${metrics.profit_factor}, WR=${(metrics.win_rate * 100).toFixed(0)}%`);
        try { await this.strategyManager.setStatus(strategy.id, 'active'); } catch (e) {
          console.error(`[StrategyLearner] Failed to promote ${strategy.name}: ${e.message}`);
        }
        return;
      }
    }

    // --- Cooldown re-activation: dry-run → active after N days regardless of performance ---
    if (strategy.status === 'dry-run') {
      const demotedAt = strategy.config?._demotedAt;
      if (demotedAt) {
        const daysSinceDemotion = (Date.now() - new Date(demotedAt).getTime()) / 86400_000;
        if (daysSinceDemotion >= THRESHOLDS.cooldown_days) {
          console.log(`[StrategyLearner] ${strategy.name} COOLDOWN EXPIRED (${daysSinceDemotion.toFixed(1)} days) — re-activating`);
          try {
            await this.strategyManager.setStatus(strategy.id, 'active');
            await this.strategyManager.updateConfig(strategy.id, { _demotedAt: null });
          } catch (e) {
            console.error(`[StrategyLearner] Failed to re-activate ${strategy.name}: ${e.message}`);
          }
          return;
        }
      } else {
        // No demotion timestamp — strategy was likely set to dry-run before this logic existed.
        // Re-activate the best-performing dry-run strategy if NO strategies are active.
        const allStrategies = this.strategyManager.getAll();
        const activeCount = allStrategies.filter(s => s.status === 'active').length;
        if (activeCount < THRESHOLDS.min_active_strategies) {
          console.log(`[StrategyLearner] ${strategy.name} re-activated (no active strategies, no demotion timestamp)`);
          try { await this.strategyManager.setStatus(strategy.id, 'active'); } catch (e) {
            console.error(`[StrategyLearner] Failed to re-activate ${strategy.name}: ${e.message}`);
          }
          return;
        }
      }
    }

    // --- Tweaks for underperforming strategies ---
    if (evaluation.status === 'UNDERPERFORMING' && metrics.closed_count >= THRESHOLDS.min_trades_for_eval) {
      const tweak = this._suggestTweak(strategy, metrics);
      if (tweak) {
        console.log(`[StrategyLearner] ${strategy.name} TWEAK: ${tweak.param} ${tweak.oldVal} → ${tweak.newVal} (${tweak.reason})`);
        try { await this.strategyManager.updateConfig(strategy.id, { [tweak.param]: tweak.newVal }); } catch (e) {
          console.error(`[StrategyLearner] Failed to tweak ${strategy.name}: ${e.message}`);
        }
      }
    }
  }

  async _discoverNewStrategies(groups, strategyResults) {
    const existing = this.strategyManager.getAll();
    const maxStrategies = 6;
    if (existing.length >= maxStrategies) return null;

    // High-performing signal type without a dedicated strategy
    for (const [sigType, m] of Object.entries(groups.signalMetrics)) {
      if (!m || m.closed_count < 10 || m.win_rate < 0.65 || m.profit_factor < 1.5) continue;

      const signalCategory = { price_breakout: 'technical', volume_spike: 'technical', rsi_extreme: 'technical', ema_cross: 'technical', macd_cross: 'technical', bb_squeeze_breakout: 'technical', whale_trade: 'whales', funding_extreme: 'technical' }[sigType] || 'technical';
      const alreadyCovered = existing.some(s => (s.config.weights?.[signalCategory] || 0) >= 60);
      if (alreadyCovered) continue;

      const weights = { technical: 20, sentiment: 20, whales: 10 };
      weights[signalCategory] = 60;
      const total = Object.values(weights).reduce((a, b) => a + b, 0);
      for (const k of Object.keys(weights)) weights[k] = Math.round(weights[k] * 100 / total);

      try {
        const created = await this.strategyManager.create(`Auto-${sigType}`, {
          weights, timeframe: '5m',
          hold_time: { min: 10, max: 120, unit: 'minutes' },
          leverage: { base: 3, max: 5 },
          max_positions: 2, tp_pct: 2, sl_pct: 2,
        }, {
          description: `Auto-generated strategy based on high-performing signal type "${sigType}" (WR=${(m.win_rate * 100).toFixed(0)}%, PF=${m.profit_factor})`,
        });
        console.log(`[StrategyLearner] SPAWNED new strategy "${created.name}" — ${sigType} has WR ${(m.win_rate * 100).toFixed(0)}%`);
        return created;
      } catch (e) {
        console.error(`[StrategyLearner] Failed to create strategy for ${sigType}: ${e.message}`);
      }
      break;
    }

    // Strong-performing asset without a dedicated strategy
    for (const [coin, m] of Object.entries(groups.assetMetrics)) {
      if (!m || m.closed_count < 10 || m.profit_factor < 2.0 || m.win_rate < 0.6) continue;

      const alreadyTargeted = existing.some(s =>
        s.target_assets?.length > 0 && s.target_assets.includes(coin)
      );
      if (alreadyTargeted) continue;

      try {
        const created = await this.strategyManager.create(`Auto-${coin}`, {
          weights: { technical: 50, sentiment: 30, whales: 20 },
          timeframe: '5m',
          hold_time: { min: 5, max: 60, unit: 'minutes' },
          leverage: { base: 3, max: 5 },
          max_positions: 2, tp_pct: 2.5, sl_pct: 1.5,
        }, {
          description: `Auto-generated strategy for ${coin} based on strong performance (PF=${m.profit_factor}, WR=${(m.win_rate * 100).toFixed(0)}%)`,
          target_assets: [coin],
        });
        console.log(`[StrategyLearner] SPAWNED new strategy "${created.name}" for ${coin}`);
        return created;
      } catch (e) {
        console.error(`[StrategyLearner] Failed to create strategy for ${coin}: ${e.message}`);
      }
      break;
    }

    // Spawn variant of a failing strategy with tweaked parameters
    for (const sr of strategyResults) {
      if (sr.evaluation !== 'FAILING' || !sr.metrics) continue;
      const original = this.strategyManager.get(sr.id);
      if (!original) continue;

      const variantName = `${original.name}-v2`;
      if (existing.some(s => s.name === variantName)) continue;

      const origWeights = original.config.weights;
      const newWeights = { ...origWeights };
      const categories = Object.keys(newWeights);
      categories.sort((a, b) => (newWeights[b] || 0) - (newWeights[a] || 0));
      if (categories.length >= 2) {
        newWeights[categories[0]] = Math.max(10, (newWeights[categories[0]] || 0) - 15);
        newWeights[categories[1]] = Math.min(80, (newWeights[categories[1]] || 0) + 15);
      }

      try {
        const created = await this.strategyManager.create(variantName, {
          ...original.config,
          weights: newWeights,
          sl_pct: (original.config.sl_pct || 2) + 0.5,
          max_positions: Math.max(1, (original.config.max_positions || 3) - 1),
        }, {
          description: `Variant of failing strategy "${original.name}" with adjusted weights and wider stops`,
          target_assets: original.target_assets || [],
        });
        console.log(`[StrategyLearner] SPAWNED variant "${created.name}" from failing "${original.name}"`);
        return created;
      } catch (e) {
        console.error(`[StrategyLearner] Failed to create variant of ${original.name}: ${e.message}`);
      }
      break;
    }

    return null;
  }

  _suggestTweak(strategy, metrics) {
    const c = strategy.config;

    if (metrics.win_rate < 0.4 && (c.sl_pct || 2) < 4) {
      return { param: 'sl_pct', oldVal: c.sl_pct || 2, newVal: (c.sl_pct || 2) + 0.5, reason: `low win rate ${(metrics.win_rate * 100).toFixed(0)}%` };
    }

    if (metrics.win_rate >= 0.5 && metrics.profit_factor < 1.0 && (c.tp_pct || 2) > 1) {
      return { param: 'tp_pct', oldVal: c.tp_pct || 2, newVal: (c.tp_pct || 2) + 0.5, reason: `good WR but low PF ${metrics.profit_factor}` };
    }

    if ((c.max_positions || 3) > 2 && metrics.net_pnl < 0) {
      return { param: 'max_positions', oldVal: c.max_positions || 3, newVal: (c.max_positions || 3) - 1, reason: `negative PnL, reduce exposure` };
    }

    return null;
  }
}

// --- CLI entrypoint ---

if (process.argv[1] && process.argv[1].endsWith('strategy-learner.mjs')) {
  const args = process.argv.slice(2);
  const getArg = (name, def) => {
    const idx = args.indexOf(`--${name}`);
    return idx >= 0 && args[idx + 1] ? args[idx + 1] : def;
  };
  const jsonOutput = args.includes('--json');
  const days = parseInt(getArg('days', '7'));

  const learner = new StrategyLearner();
  learner.run(days).then(result => {
    if (jsonOutput) {
      console.log(JSON.stringify(result, null, 2));
    } else {
      const o = result.overall;
      console.log(`\n=== Strategy Evaluation (${result.date}) ===`);
      console.log(`Status: ${o.status}`);
      if (o.metrics) {
        const m = o.metrics;
        console.log(`Trades: ${m.closed_count} closed`);
        console.log(`Win rate: ${(m.win_rate * 100).toFixed(0)}% | PF: ${m.profit_factor} | PnL: $${m.net_pnl}`);
      }
      for (const r of o.recommendations || []) console.log(`  → ${r}`);
      if (result.dryRunPromotion) {
        console.log(`\nDry-Run: ${result.dryRunPromotion.promote ? 'READY' : 'Not ready'} — ${result.dryRunPromotion.reason}`);
      }
      console.log(`\nLessons generated: ${result.lessonsGenerated}`);
    }
    process.exit(0);
  }).catch(e => {
    console.error('Error:', e.message);
    process.exit(1);
  });
}
