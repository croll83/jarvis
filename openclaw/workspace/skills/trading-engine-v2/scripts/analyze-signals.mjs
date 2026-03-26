import { readFileSync } from 'fs';

const lines = readFileSync('/home/jarvis/.openclaw/workspace/skills/trading-engine-v2/state/dry-run-signals.jsonl', 'utf8').trim().split('\n');
const signals = lines.map(l => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);

console.log('=== SIGNAL SUMMARY (6h collection) ===');
console.log('Total signals:', signals.length);
console.log();

// By type
const byType = {};
signals.forEach(s => { byType[s.type] = (byType[s.type] || 0) + 1; });
console.log('--- By Signal Type ---');
Object.entries(byType).sort((a, b) => b[1] - a[1]).forEach(([t, c]) => console.log(`  ${t}: ${c}`));
console.log();

// By coin
const byCoin = {};
signals.forEach(s => { byCoin[s.coin] = (byCoin[s.coin] || 0) + 1; });
console.log('--- By Coin (top 15) ---');
Object.entries(byCoin).sort((a, b) => b[1] - a[1]).slice(0, 15).forEach(([c, n]) => console.log(`  ${c}: ${n}`));
console.log();

// By asset class
const cryptoSignals = signals.filter(s => !s.coin.startsWith('xyz:'));
const xyzSignals = signals.filter(s => s.coin.startsWith('xyz:'));
console.log('--- By Asset Class ---');
console.log('  Crypto:', cryptoSignals.length);
console.log('  XYZ (commodities/equities/forex):', xyzSignals.length);
console.log();

// Whale trades analysis
const whales = signals.filter(s => s.type === 'whale_trade');
if (whales.length > 0) {
  const totalNotional = whales.reduce((s, w) => s + (w.notional || 0), 0);
  const avgNotional = totalNotional / whales.length;
  const maxWhale = whales.reduce((m, w) => (w.notional || 0) > (m.notional || 0) ? w : m, whales[0]);
  const buys = whales.filter(w => w.side === 'B').length;
  const sells = whales.filter(w => w.side === 'A').length;
  console.log('--- Whale Trades ---');
  console.log('  Count:', whales.length);
  console.log('  Total notional: $' + (totalNotional / 1e6).toFixed(2) + 'M');
  console.log('  Avg notional: $' + Math.round(avgNotional).toLocaleString());
  console.log('  Largest: $' + maxWhale.notional.toLocaleString() + ' on ' + maxWhale.coin + ' (' + (maxWhale.side === 'B' ? 'BUY' : 'SELL') + ')');
  console.log('  Buy/Sell ratio:', buys + '/' + sells);
  const whaleByCoin = {};
  whales.forEach(w => { whaleByCoin[w.coin] = (whaleByCoin[w.coin] || 0) + 1; });
  console.log('  Per coin:');
  Object.entries(whaleByCoin).sort((a, b) => b[1] - a[1]).forEach(([c, n]) => console.log(`    ${c}: ${n}`));
}
console.log();

// RSI analysis
const rsis = signals.filter(s => s.type === 'rsi_extreme');
if (rsis.length > 0) {
  const overbought = rsis.filter(r => r.condition === 'OVERBOUGHT');
  const oversold = rsis.filter(r => r.condition === 'OVERSOLD');
  console.log('--- RSI Extremes ---');
  console.log('  Overbought:', overbought.length, '| Oversold:', oversold.length);
  const rsiCoins = {};
  rsis.forEach(r => { rsiCoins[r.coin] = (rsiCoins[r.coin] || 0) + 1; });
  console.log('  Most active:', Object.entries(rsiCoins).sort((a, b) => b[1] - a[1]).slice(0, 8).map(([c, n]) => c + ':' + n).join(', '));

  // Average RSI values for oversold/overbought
  if (overbought.length > 0) {
    const avgOB = overbought.reduce((s, r) => s + r.rsi, 0) / overbought.length;
    console.log('  Avg overbought RSI:', avgOB.toFixed(1));
  }
  if (oversold.length > 0) {
    const avgOS = oversold.reduce((s, r) => s + r.rsi, 0) / oversold.length;
    console.log('  Avg oversold RSI:', avgOS.toFixed(1));
  }
}
console.log();

// EMA cross analysis
const emaCrosses = signals.filter(s => s.type === 'ema_cross');
if (emaCrosses.length > 0) {
  const bullish = emaCrosses.filter(e => e.direction === 'BULLISH').length;
  const bearish = emaCrosses.filter(e => e.direction === 'BEARISH').length;
  console.log('--- EMA Crosses ---');
  console.log('  Total:', emaCrosses.length, '| Bullish:', bullish, '| Bearish:', bearish);
  const emaByCoin = {};
  emaCrosses.forEach(e => { emaByCoin[e.coin] = (emaByCoin[e.coin] || 0) + 1; });
  console.log('  Noisiest:', Object.entries(emaByCoin).sort((a, b) => b[1] - a[1]).slice(0, 5).map(([c, n]) => c + ':' + n).join(', '));
}
console.log();

// MACD cross analysis
const macdCrosses = signals.filter(s => s.type === 'macd_cross');
if (macdCrosses.length > 0) {
  const bullish = macdCrosses.filter(e => e.direction === 'BULLISH').length;
  const bearish = macdCrosses.filter(e => e.direction === 'BEARISH').length;
  console.log('--- MACD Crosses ---');
  console.log('  Total:', macdCrosses.length, '| Bullish:', bullish, '| Bearish:', bearish);
}
console.log();

// VWAP deviation
const vwaps = signals.filter(s => s.type === 'vwap_deviation');
if (vwaps.length > 0) {
  const above = vwaps.filter(v => v.direction === 'ABOVE').length;
  const below = vwaps.filter(v => v.direction === 'BELOW').length;
  const avgDev = vwaps.reduce((s, v) => s + Math.abs(v.deviation || 0), 0) / vwaps.length;
  console.log('--- VWAP Deviation ---');
  console.log('  Total:', vwaps.length, '| Above:', above, '| Below:', below);
  console.log('  Avg deviation:', avgDev.toFixed(2) + '%');
}
console.log();

// Volume spikes
const volSpikes = signals.filter(s => s.type === 'volume_spike');
if (volSpikes.length > 0) {
  const avgMult = volSpikes.reduce((s, v) => s + parseFloat(v.multiplier || 0), 0) / volSpikes.length;
  console.log('--- Volume Spikes ---');
  console.log('  Total:', volSpikes.length);
  console.log('  Avg multiplier:', avgMult.toFixed(1) + 'x');
  const volByCoin = {};
  volSpikes.forEach(v => { volByCoin[v.coin] = (volByCoin[v.coin] || 0) + 1; });
  console.log('  Per coin:', Object.entries(volByCoin).sort((a, b) => b[1] - a[1]).slice(0, 5).map(([c, n]) => c + ':' + n).join(', '));
}
console.log();

// Signal frequency over time
const hourBuckets = {};
signals.forEach(s => {
  const h = new Date(s.timestamp).toISOString().slice(0, 13);
  hourBuckets[h] = (hourBuckets[h] || 0) + 1;
});
console.log('--- Signals Per Hour ---');
Object.entries(hourBuckets).sort().forEach(([h, c]) => console.log(`  ${h}: ${c}`));
console.log();

// Swarm decisions from daemon log
try {
  const log = readFileSync('/home/jarvis/.openclaw/workspace/skills/trading-engine-v2/state/daemon.log', 'utf8');
  const decisions = log.split('\n').filter(l => l.includes('DECISION:'));
  console.log('--- Swarm Decisions ---');
  console.log('  Total:', decisions.length);

  const decisionTypes = {};
  decisions.forEach(d => {
    const match = d.match(/DECISION: (\w+)/);
    if (match) decisionTypes[match[1]] = (decisionTypes[match[1]] || 0) + 1;
  });
  Object.entries(decisionTypes).sort((a, b) => b[1] - a[1]).forEach(([t, c]) => console.log(`  ${t}: ${c}`));

  // Show some interesting decisions
  const nonHold = decisions.filter(d => !d.includes('HOLD'));
  if (nonHold.length > 0) {
    console.log('\n  Notable decisions (non-HOLD):');
    nonHold.slice(-10).forEach(d => {
      const clean = d.replace(/^\[.*?\]\s*/, '').substring(0, 200);
      console.log('    ' + clean);
    });
  }
} catch {}

// Signal quality assessment
console.log('\n=== SIGNAL QUALITY ASSESSMENT ===');

// Noise ratio: signals that fire too frequently per coin
const signalFreq = {};
signals.forEach(s => {
  const key = `${s.coin}:${s.type}`;
  if (!signalFreq[key]) signalFreq[key] = [];
  signalFreq[key].push(s.timestamp);
});

let noisyCount = 0;
let cleanCount = 0;
Object.entries(signalFreq).forEach(([key, times]) => {
  if (times.length > 1) {
    const intervals = [];
    for (let i = 1; i < times.length; i++) {
      intervals.push(times[i] - times[i - 1]);
    }
    const avgInterval = intervals.reduce((s, i) => s + i, 0) / intervals.length;
    if (avgInterval < 120_000) { // less than 2 min avg = noisy
      noisyCount++;
    } else {
      cleanCount++;
    }
  } else {
    cleanCount++;
  }
});

console.log('  Clean signal pairs:', cleanCount);
console.log('  Noisy signal pairs (avg <2min interval):', noisyCount);
console.log('  Signal-to-noise quality:', ((cleanCount / (cleanCount + noisyCount)) * 100).toFixed(0) + '%');
