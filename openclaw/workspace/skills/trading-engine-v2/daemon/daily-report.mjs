#!/usr/bin/env node
/**
 * Daily Report — Generates trading performance report and sends to Telegram.
 *
 * Uses PositionManager for open positions when available (daemon mode).
 * Falls back to trade-log parsing when run standalone (CLI mode).
 *
 * Usage:
 *   - Standalone CLI: node daemon/daily-report.mjs [--days 1] [--stdout]
 *   - From daemon: import { DailyReporter } and call .sendReport()
 */

import { HLClient } from '../lib/hl-client.mjs';
import { readFileSync, existsSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const STATE_DIR = resolve(__dirname, '..', 'state');
const TRADE_LOG = resolve(STATE_DIR, 'trade-log.jsonl');
const STRATEGIES_CACHE = resolve(STATE_DIR, 'strategies.json');

const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN || '';
const TELEGRAM_CHAT_ID = '-1003803487367';
const TELEGRAM_TOPIC_ID = 14;

// --- Trade log reader ---

function readClosedTrades(maxAgeDays = 1) {
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

// --- Metrics computation ---

function computeMetrics(closedTrades, openPositions = []) {
  const pnls = closedTrades.map(t => parseFloat(t.pnl) || 0);
  const wins = pnls.filter(p => p > 0);
  const losses = pnls.filter(p => p < 0);
  const grossProfit = wins.reduce((s, p) => s + p, 0);
  const grossLoss = Math.abs(losses.reduce((s, p) => s + p, 0));
  const netPnl = grossProfit - grossLoss;
  const winRate = closedTrades.length > 0 ? wins.length / closedTrades.length : 0;
  const profitFactor = grossLoss > 0 ? grossProfit / grossLoss : grossProfit > 0 ? 999 : 0;

  return {
    closed: closedTrades.length,
    open: openPositions.length,
    wins: wins.length,
    losses: losses.length,
    winRate: parseFloat((winRate * 100).toFixed(1)),
    netPnl: parseFloat(netPnl.toFixed(2)),
    profitFactor: profitFactor === 999 ? '∞' : parseFloat(profitFactor.toFixed(2)),
    avgWin: wins.length > 0 ? parseFloat((grossProfit / wins.length).toFixed(2)) : 0,
    avgLoss: losses.length > 0 ? parseFloat((grossLoss / losses.length).toFixed(2)) : 0,
  };
}

// --- Load strategy names from cache ---

function loadStrategyNames() {
  try {
    if (!existsSync(STRATEGIES_CACHE)) return {};
    const data = JSON.parse(readFileSync(STRATEGIES_CACHE, 'utf8'));
    const map = {};
    for (const s of data.strategies || []) map[s.id] = s.name;
    return map;
  } catch {}
  return {};
}

// --- Compute unrealized PnL for open positions ---

function computeUnrealizedPnl(positions, prices) {
  let total = 0;
  for (const pos of positions) {
    const cp = parseFloat(prices[pos.coin] || prices[pos.coin + '-PERP'] || prices['xyz:' + pos.coin] || 0);
    if (!cp || !pos.entryPrice) continue;
    const isLong = pos.side === 'long';
    total += isLong
      ? ((cp - pos.entryPrice) / pos.entryPrice) * pos.size
      : ((pos.entryPrice - cp) / pos.entryPrice) * pos.size;
  }
  return parseFloat(total.toFixed(2));
}

// --- Format report for Telegram ---

function formatReport(closedTrades, openPositions, unrealizedPnl, days) {
  const strategyNames = loadStrategyNames();
  const now = new Date();
  const timeLabel = now.getHours() < 15 ? '🌅 Morning' : '🌙 Evening';
  const dateStr = now.toISOString().split('T')[0];

  // Split closed trades by mode
  const liveClosed = closedTrades.filter(t => !t.dryRun);
  const dryRunClosed = closedTrades.filter(t => t.dryRun);

  // Split open positions by mode
  const liveOpen = openPositions.filter(p => !p.dryRun);
  const dryRunOpen = openPositions.filter(p => p.dryRun);

  const groupByStrategy = (list) => {
    const groups = {};
    for (const t of list) {
      const key = t.strategyId || '_none';
      if (!groups[key]) groups[key] = { name: strategyNames[key] || t.strategyName || 'No Strategy', items: [] };
      groups[key].items.push(t);
    }
    return groups;
  };

  let report = `<b>${timeLabel} Report — ${dateStr}</b>\n`;
  report += `<i>Last ${days > 1 ? days + ' days' : '24h'} • ${closedTrades.length} closed, ${openPositions.length} open</i>\n`;

  // --- LIVE section ---
  if (liveClosed.length > 0 || liveOpen.length > 0) {
    report += `\n<b>💰 LIVE TRADING</b>\n`;
    const liveMetrics = computeMetrics(liveClosed, liveOpen);
    report += `• <b>PnL:</b> $${liveMetrics.netPnl} (${liveMetrics.wins}W/${liveMetrics.losses}L)\n`;
    report += `• <b>Win Rate:</b> ${liveMetrics.winRate}% • PF: ${liveMetrics.profitFactor}\n`;
    if (liveOpen.length > 0) {
      const liveUnrealized = unrealizedPnl; // already filtered
      report += `• <b>Open:</b> ${liveOpen.length} positions\n`;
    }

    const liveGroups = groupByStrategy(liveClosed);
    for (const [, group] of Object.entries(liveGroups)) {
      const m = computeMetrics(group.items);
      if (m.closed === 0) continue;
      const pnlEmoji = m.netPnl >= 0 ? '🟢' : '🔴';
      report += `  ${pnlEmoji} <b>${group.name}:</b> $${m.netPnl} (${m.wins}W/${m.losses}L, WR ${m.winRate}%)\n`;
    }

    if (liveClosed.length > 0) {
      const best = liveClosed.reduce((a, b) => (parseFloat(a.pnl) || 0) > (parseFloat(b.pnl) || 0) ? a : b);
      const worst = liveClosed.reduce((a, b) => (parseFloat(a.pnl) || 0) < (parseFloat(b.pnl) || 0) ? a : b);
      if ((parseFloat(best.pnl) || 0) > 0) report += `  ⭐ Best: ${best.coin} ${best.side} +$${best.pnl}\n`;
      if ((parseFloat(worst.pnl) || 0) < 0) report += `  💀 Worst: ${worst.coin} ${worst.side} $${worst.pnl}\n`;
    }
  } else {
    report += `\n<b>💰 LIVE:</b> No trades\n`;
  }

  // --- DRY-RUN section ---
  if (dryRunClosed.length > 0 || dryRunOpen.length > 0) {
    report += `\n<b>📝 DRY-RUN (paper)</b>\n`;
    const dryMetrics = computeMetrics(dryRunClosed, dryRunOpen);
    report += `• <b>Simulated PnL:</b> $${dryMetrics.netPnl} (${dryMetrics.wins}W/${dryMetrics.losses}L)\n`;
    report += `• <b>Win Rate:</b> ${dryMetrics.winRate}% • PF: ${dryMetrics.profitFactor}\n`;
    if (dryRunOpen.length > 0) {
      report += `• <b>Open:</b> ${dryRunOpen.length} positions\n`;
    }

    const dryGroups = groupByStrategy(dryRunClosed);
    for (const [, group] of Object.entries(dryGroups)) {
      const m = computeMetrics(group.items);
      if (m.closed === 0) continue;
      const pnlEmoji = m.netPnl >= 0 ? '🟢' : '🔴';
      report += `  ${pnlEmoji} <b>${group.name}:</b> $${m.netPnl} (${m.wins}W/${m.losses}L, WR ${m.winRate}%)\n`;
    }

    if (dryRunClosed.length > 0) {
      const best = dryRunClosed.reduce((a, b) => (parseFloat(a.pnl) || 0) > (parseFloat(b.pnl) || 0) ? a : b);
      const worst = dryRunClosed.reduce((a, b) => (parseFloat(a.pnl) || 0) < (parseFloat(b.pnl) || 0) ? a : b);
      if ((parseFloat(best.pnl) || 0) > 0) report += `  ⭐ Best: ${best.coin} ${best.side} +$${best.pnl}\n`;
      if ((parseFloat(worst.pnl) || 0) < 0) report += `  💀 Worst: ${worst.coin} ${worst.side} $${worst.pnl}\n`;
    }
  } else {
    report += `\n<b>📝 DRY-RUN:</b> No trades\n`;
  }

  // --- Token budget ---
  try {
    const tokenFile = resolve(STATE_DIR, 'token-usage.json');
    if (existsSync(tokenFile)) {
      const usage = JSON.parse(readFileSync(tokenFile, 'utf8'));
      if (usage.today) {
        const totalCost = Object.values(usage.today).reduce((s, m) => s + (m.cost_usd || 0), 0);
        const budget = usage.alerts?.daily_budget_usd || 10;
        report += `\n<b>⚡ Tokens:</b> $${totalCost.toFixed(2)}/$${budget} (${(totalCost / budget * 100).toFixed(0)}%)`;
      }
    }
  } catch {}

  return report;
}

// --- Telegram sender ---

export async function sendTelegram(text) {
  if (!TELEGRAM_BOT_TOKEN) {
    console.error('[DailyReport] TELEGRAM_BOT_TOKEN not set');
    return false;
  }

  try {
    const resp = await fetch(`https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        chat_id: TELEGRAM_CHAT_ID,
        message_thread_id: TELEGRAM_TOPIC_ID,
        text,
        parse_mode: 'HTML',
        disable_web_page_preview: true,
      }),
    });

    if (!resp.ok) {
      const err = await resp.text();
      console.error(`[DailyReport] Telegram API error: ${resp.status} ${err}`);
      return false;
    }

    console.log('[DailyReport] Report sent to Telegram');
    return true;
  } catch (e) {
    console.error(`[DailyReport] Send failed: ${e.message}`);
    return false;
  }
}

// --- Main class ---

export class DailyReporter {
  constructor(opts = {}) {
    this.hl = opts.hlClient || new HLClient();
    this.positionManager = opts.positionManager || null;
    this.days = opts.days || 1;
  }

  async sendReport(days) {
    days = days || this.days;

    const closedTrades = readClosedTrades(days);

    // Open positions: use PositionManager if available, else empty
    const openPositions = this.positionManager ? this.positionManager.getAll() : [];

    // Compute unrealized PnL
    let unrealizedPnl = 0;
    if (openPositions.length > 0) {
      try {
        const prices = await this.hl.getAllPrices(true) || {};
        unrealizedPnl = computeUnrealizedPnl(openPositions, prices);
      } catch {}
    }

    if (closedTrades.length === 0 && openPositions.length === 0) {
      const text = `<b>📊 Daily Report</b>\n\nNo trades in the last ${days > 1 ? days + ' days' : '24h'}.`;
      await sendTelegram(text);
      return { sent: true, trades: 0 };
    }

    const report = formatReport(closedTrades, openPositions, unrealizedPnl, days);
    const sent = await sendTelegram(report);
    return { sent, trades: closedTrades.length, open: openPositions.length, report };
  }

  async generateReport(days) {
    days = days || this.days;
    const closedTrades = readClosedTrades(days);
    const openPositions = this.positionManager ? this.positionManager.getAll() : [];

    let unrealizedPnl = 0;
    if (openPositions.length > 0) {
      try {
        const prices = await this.hl.getAllPrices(true) || {};
        unrealizedPnl = computeUnrealizedPnl(openPositions, prices);
      } catch {}
    }

    if (closedTrades.length === 0 && openPositions.length === 0) return 'No trades found.';
    return formatReport(closedTrades, openPositions, unrealizedPnl, days);
  }
}

// --- CLI entrypoint ---

if (process.argv[1] && process.argv[1].endsWith('daily-report.mjs')) {
  const args = process.argv.slice(2);
  const getArg = (name, def) => {
    const idx = args.indexOf(`--${name}`);
    return idx >= 0 && args[idx + 1] ? args[idx + 1] : def;
  };
  const stdoutOnly = args.includes('--stdout');
  const days = parseInt(getArg('days', '1'));

  // CLI mode: no PositionManager, reports closed trades only
  const reporter = new DailyReporter({ days });

  if (stdoutOnly) {
    reporter.generateReport(days).then(report => {
      console.log(report.replace(/<\/?[^>]+(>|$)/g, ''));
      process.exit(0);
    }).catch(e => {
      console.error('Error:', e.message);
      process.exit(1);
    });
  } else {
    reporter.sendReport(days).then(result => {
      console.log(`Report ${result.sent ? 'sent' : 'FAILED'} — ${result.trades} closed trades`);
      process.exit(result.sent ? 0 : 1);
    }).catch(e => {
      console.error('Error:', e.message);
      process.exit(1);
    });
  }
}
