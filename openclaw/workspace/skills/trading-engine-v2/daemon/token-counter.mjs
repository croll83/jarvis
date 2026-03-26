/**
 * Token Counter — Tracks LLM API usage, costs, and sends budget alerts.
 *
 * NEVER pauses trading. Instead sends Telegram alerts at milestones:
 *   70%, 100%, 125%, 150%, 175%, 200%, ... (each fires once per day)
 *
 * Persists to state/token-usage.json.
 */

import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const STATE_DIR = resolve(__dirname, '..', 'state');
const STATE_FILE = resolve(STATE_DIR, 'token-usage.json');

// Cost per 1M tokens (USD) — includes cache token pricing
// Haiku 4.5: $1.00 input, $5.00 output, $1.25 cacheWrite, $0.10 cacheRead
// Sonnet 4.6: $3.00 input, $15.00 output, $3.75 cacheWrite, $0.30 cacheRead
const MODEL_COSTS = {
  'sonnet': { input: 3.00, output: 15.00, cacheWrite: 3.75, cacheRead: 0.30 },
  'haiku': { input: 1.00, output: 5.00, cacheWrite: 1.25, cacheRead: 0.10 },
  'gemini_flash_lite': { input: 0.25, output: 1.50 },
  'grok_fast': { input: 0.20, output: 0.50 },
};

const DEFAULT_ALERTS = {
  daily_budget_usd: 10.00,
  warn_at_pct: 70,
  // Alert milestones above budget (each fires once per day)
  over_budget_step_pct: 25, // alert at 100%, 125%, 150%, 175%, ...
};

export class TokenCounter {
  constructor(opts = {}) {
    this.alertCallback = opts.onAlert || null;
    this._state = this._load();
    this._dirty = false;
    this._firedMilestones = new Set(); // tracks which milestones fired today
    this._currentDay = this._today();
    this._flushTimer = setInterval(() => this.flush(), 30_000);
  }

  // Record token usage for a model call
  record(model, inputTokens, outputTokens, cacheWriteTokens = 0, cacheReadTokens = 0) {
    const today = this._today();

    // Reset milestones on day change
    if (today !== this._currentDay) {
      this._firedMilestones.clear();
      this._currentDay = today;
    }

    if (!this._state.daily[today]) {
      this._state.daily[today] = {};
    }
    const day = this._state.daily[today];
    if (!day[model]) {
      day[model] = { input: 0, output: 0, cacheWrite: 0, cacheRead: 0, cost_usd: 0, calls: 0 };
    }

    const entry = day[model];
    entry.input += inputTokens;
    entry.output += outputTokens;
    entry.cacheWrite = (entry.cacheWrite || 0) + cacheWriteTokens;
    entry.cacheRead = (entry.cacheRead || 0) + cacheReadTokens;
    entry.calls += 1;

    // Calculate cost including cache tokens
    const costs = MODEL_COSTS[model] || { input: 1.0, output: 5.0 };
    const cacheWriteRate = costs.cacheWrite || costs.input;
    const cacheReadRate = costs.cacheRead || costs.input;
    entry.cost_usd = parseFloat(
      ((entry.input / 1_000_000) * costs.input +
       (entry.output / 1_000_000) * costs.output +
       ((entry.cacheWrite || 0) / 1_000_000) * cacheWriteRate +
       ((entry.cacheRead || 0) / 1_000_000) * cacheReadRate).toFixed(4)
    );

    // Update total
    day.total_cost_usd = Object.entries(day)
      .filter(([k]) => k !== 'total_cost_usd')
      .reduce((sum, [, v]) => sum + (v.cost_usd || 0), 0);
    day.total_cost_usd = parseFloat(day.total_cost_usd.toFixed(4));

    this._dirty = true;
    this._checkMilestones(today);

    return { model, inputTokens, outputTokens, dailyTotal: day.total_cost_usd };
  }

  // Budget exceeded flag (informational only — never blocks trading)
  isOverBudget() {
    const today = this._today();
    const day = this._state.daily[today];
    if (!day) return false;
    return day.total_cost_usd >= this._state.alerts.daily_budget_usd;
  }

  // Get today's usage summary
  getTodaySummary() {
    const today = this._today();
    const day = this._state.daily[today] || {};
    return {
      date: today,
      total_cost_usd: day.total_cost_usd || 0,
      budget_usd: this._state.alerts.daily_budget_usd,
      budget_used_pct: day.total_cost_usd
        ? ((day.total_cost_usd / this._state.alerts.daily_budget_usd) * 100).toFixed(1)
        : '0.0',
      over_budget: this.isOverBudget(),
      models: Object.fromEntries(
        Object.entries(day).filter(([k]) => k !== 'total_cost_usd')
      ),
    };
  }

  // Update alert configuration
  setAlerts(alerts) {
    Object.assign(this._state.alerts, alerts);
    this._dirty = true;
  }

  // Force flush to disk
  flush() {
    if (!this._dirty) return;
    if (!existsSync(STATE_DIR)) mkdirSync(STATE_DIR, { recursive: true });
    writeFileSync(STATE_FILE, JSON.stringify(this._state, null, 2));
    this._dirty = false;
  }

  // Cleanup old entries (keep last 30 days)
  prune(keepDays = 30) {
    const cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - keepDays);
    const cutoffStr = cutoff.toISOString().split('T')[0];

    for (const date of Object.keys(this._state.daily)) {
      if (date < cutoffStr) delete this._state.daily[date];
    }
    this._dirty = true;
  }

  destroy() {
    this.flush();
    if (this._flushTimer) clearInterval(this._flushTimer);
  }

  // --- Internal ---

  _today() {
    return new Date().toISOString().split('T')[0];
  }

  _load() {
    const defaults = { daily: {}, alerts: { ...DEFAULT_ALERTS } };
    try {
      if (existsSync(STATE_FILE)) {
        const data = JSON.parse(readFileSync(STATE_FILE, 'utf8'));
        return { daily: data.daily || {}, alerts: { ...DEFAULT_ALERTS, ...data.alerts } };
      }
    } catch (e) {
      console.error('[TokenCounter] Failed to load state:', e.message);
    }
    return defaults;
  }

  /**
   * Check and fire milestone alerts. Each milestone fires ONCE per day.
   * Milestones: 70% (warn), 100%, 125%, 150%, 175%, 200%, ...
   */
  _checkMilestones(today) {
    const day = this._state.daily[today];
    if (!day) return;

    const budget = this._state.alerts.daily_budget_usd;
    const usedPct = (day.total_cost_usd / budget) * 100;
    const step = this._state.alerts.over_budget_step_pct || 25;

    // Warning milestone at 70%
    const warnPct = this._state.alerts.warn_at_pct || 70;
    if (usedPct >= warnPct && !this._firedMilestones.has(warnPct)) {
      this._firedMilestones.add(warnPct);
      this._alert('WARN', `Token budget at ${usedPct.toFixed(0)}%: $${day.total_cost_usd.toFixed(2)}/$${budget}`);
    }

    // Over-budget milestones: 100%, 125%, 150%, ...
    if (usedPct >= 100) {
      // Find the highest milestone we've crossed
      let milestone = 100;
      while (milestone + step <= usedPct) {
        milestone += step;
      }

      if (!this._firedMilestones.has(milestone)) {
        this._firedMilestones.add(milestone);
        const emoji = milestone >= 200 ? '🔥🔥🔥' : milestone >= 150 ? '🔥🔥' : '🔥';
        this._alert('OVER_BUDGET', `${emoji} Token budget at ${usedPct.toFixed(0)}%: $${day.total_cost_usd.toFixed(2)}/$${budget} — trading continues`);
      }
    }
  }

  _alert(level, message) {
    console.log(`[TokenCounter] [${level}] ${message}`);
    if (this.alertCallback) {
      this.alertCallback({ level, message, timestamp: new Date().toISOString() });
    }
  }
}
