#!/usr/bin/env node
/**
 * signal-analyze — Technical signal analysis for a coin.
 * Usage: node signal-analyze.mjs --coin BTC [--timeframe 5m] [--json]
 *
 * Outputs a structured signal score (-100 to +100) with indicator breakdown.
 */

import { HLClient } from './lib/hl-client.mjs';
import { analyzeCandles, computeSignalScore } from './lib/indicators.mjs';

const [,, ...args] = process.argv;
const getArg = (name, def) => {
  const idx = args.indexOf(`--${name}`);
  return idx >= 0 && args[idx + 1] ? args[idx + 1] : def;
};
const hasFlag = (name) => args.includes(`--${name}`);

async function main() {
  const coin = getArg('coin', null);
  if (!coin) { console.log('Usage: node signal-analyze.mjs --coin BTC [--timeframe 5m]'); process.exit(1); }

  const timeframe = getArg('timeframe', '5m');
  const jsonOutput = hasFlag('json');

  const hl = new HLClient();
  const candles = await hl.getCandles(coin, timeframe, 100);

  if (candles.length < 30) {
    console.error(`Insufficient candle data for ${coin} (${candles.length} candles)`);
    process.exit(1);
  }

  const analysis = analyzeCandles(candles, {
    rsiPeriod: 9, emaFast: 9, emaSlow: 21,
    macdFast: 5, macdSlow: 13, macdSignal: 6,
    bbPeriod: 20, bbStdDev: 2, atrPeriod: 14,
  });

  const signal = computeSignalScore(analysis);

  const result = {
    coin,
    timeframe,
    timestamp: new Date().toISOString(),
    analysis,
    signal,
  };

  if (jsonOutput) {
    console.log(JSON.stringify(result, null, 2));
  } else {
    console.log(`=== ${coin} Signal Analysis (${timeframe}) ===`);
    console.log(`Price: $${analysis.price}`);
    console.log(`Score: ${signal.score} → ${signal.direction} (confidence: ${signal.confidence}%)`);
    console.log(`\nIndicators:`);
    console.log(`  RSI(9): ${analysis.rsi}`);
    console.log(`  EMA(9/21): ${analysis.ema.trend} ${analysis.ema.cross !== 'none' ? `[${analysis.ema.cross} cross!]` : ''}`);
    console.log(`  MACD: ${analysis.macd.trend} (hist: ${analysis.macd.histogram})`);
    console.log(`  BB: ${analysis.bb.position} band ${analysis.bb.squeeze ? '[SQUEEZE]' : ''}`);
    console.log(`  VWAP delta: ${analysis.vwapDelta}%`);
    console.log(`  ATR: ${analysis.atr}`);
    console.log(`\nSignals:`);
    for (const s of signal.signals) console.log(`  - ${s}`);
  }
}

main().then(() => process.exit(0)).catch(e => { console.error('Error:', e.message); process.exit(1); });
