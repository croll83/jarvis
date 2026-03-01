#!/usr/bin/env node
/**
 * signal-analyze — Technical signal analysis for a coin.
 * Usage: node signal-analyze.mjs --coin BTC [--timeframe 5m] [--json]
 *
 * Outputs a structured signal score (-100 to +100) with indicator breakdown.
 */

import { HLClient } from './lib/hl-client.mjs';
import { analyzeCandles } from './lib/indicators.mjs';

const [,, ...args] = process.argv;
const getArg = (name, def) => {
  const idx = args.indexOf(`--${name}`);
  return idx >= 0 && args[idx + 1] ? args[idx + 1] : def;
};
const hasFlag = (name) => args.includes(`--${name}`);

function computeSignalScore(analysis) {
  let score = 0;
  const signals = [];

  // RSI (weight: 25)
  if (analysis.rsi !== null) {
    if (analysis.rsi < 25) { score += 25; signals.push(`RSI ${analysis.rsi} (oversold, bullish)`); }
    else if (analysis.rsi < 35) { score += 12; signals.push(`RSI ${analysis.rsi} (approaching oversold)`); }
    else if (analysis.rsi > 75) { score -= 25; signals.push(`RSI ${analysis.rsi} (overbought, bearish)`); }
    else if (analysis.rsi > 65) { score -= 12; signals.push(`RSI ${analysis.rsi} (approaching overbought)`); }
    else { signals.push(`RSI ${analysis.rsi} (neutral)`); }
  }

  // EMA cross (weight: 20)
  if (analysis.ema.cross === 'bullish') { score += 20; signals.push('EMA bullish cross'); }
  else if (analysis.ema.cross === 'bearish') { score -= 20; signals.push('EMA bearish cross'); }
  else if (analysis.ema.trend === 'bullish') { score += 8; signals.push('EMA trend bullish'); }
  else { score -= 8; signals.push('EMA trend bearish'); }

  // MACD (weight: 20)
  if (analysis.macd.histogram !== null) {
    if (analysis.macd.histogram > 0 && analysis.macd.trend === 'bullish') {
      score += 15; signals.push('MACD bullish momentum');
    } else if (analysis.macd.histogram < 0 && analysis.macd.trend === 'bearish') {
      score -= 15; signals.push('MACD bearish momentum');
    }
  }

  // Bollinger Bands (weight: 15)
  if (analysis.bb.position === 'lower') {
    score += 15; signals.push('Price at BB lower band (potential bounce)');
  } else if (analysis.bb.position === 'upper') {
    score -= 15; signals.push('Price at BB upper band (potential reversal)');
  }
  if (analysis.bb.squeeze) {
    signals.push('BB squeeze detected (breakout imminent)');
  }

  // VWAP (weight: 10)
  if (analysis.vwapDelta !== null) {
    if (analysis.vwapDelta < -1) { score += 10; signals.push(`Price ${analysis.vwapDelta}% below VWAP (undervalued)`); }
    else if (analysis.vwapDelta > 1) { score -= 10; signals.push(`Price ${analysis.vwapDelta}% above VWAP (overvalued)`); }
  }

  // ATR context
  if (analysis.atr !== null && analysis.price > 0) {
    const atrPct = (analysis.atr / analysis.price * 100).toFixed(2);
    signals.push(`ATR: ${atrPct}% (${parseFloat(atrPct) > 2 ? 'high' : 'normal'} volatility)`);
  }

  return {
    score: Math.max(-100, Math.min(100, score)),
    direction: score > 15 ? 'LONG' : score < -15 ? 'SHORT' : 'NEUTRAL',
    confidence: Math.min(100, Math.abs(score)),
    signals,
  };
}

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
