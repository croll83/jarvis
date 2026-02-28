/**
 * Technical indicators library for trading signal analysis.
 * All functions are pure — take candle data, return computed values.
 */

export function sma(data, period) {
  const result = [];
  for (let i = 0; i < data.length; i++) {
    if (i < period - 1) { result.push(null); continue; }
    const slice = data.slice(i - period + 1, i + 1);
    result.push(slice.reduce((a, b) => a + b, 0) / period);
  }
  return result;
}

export function ema(data, period) {
  const result = [];
  const k = 2 / (period + 1);
  let prev = null;
  for (let i = 0; i < data.length; i++) {
    if (i < period - 1) { result.push(null); continue; }
    if (prev === null) {
      prev = data.slice(0, period).reduce((a, b) => a + b, 0) / period;
    } else {
      prev = data[i] * k + prev * (1 - k);
    }
    result.push(prev);
  }
  return result;
}

export function rsi(closes, period = 14) {
  const gains = [];
  const losses = [];
  for (let i = 1; i < closes.length; i++) {
    const delta = closes[i] - closes[i - 1];
    gains.push(delta > 0 ? delta : 0);
    losses.push(delta < 0 ? Math.abs(delta) : 0);
  }
  if (gains.length < period) return [];

  let avgGain = gains.slice(0, period).reduce((a, b) => a + b, 0) / period;
  let avgLoss = losses.slice(0, period).reduce((a, b) => a + b, 0) / period;

  const result = new Array(period).fill(null);
  result.push(avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss));

  for (let i = period; i < gains.length; i++) {
    avgGain = (avgGain * (period - 1) + gains[i]) / period;
    avgLoss = (avgLoss * (period - 1) + losses[i]) / period;
    result.push(avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss));
  }
  return result;
}

export function macd(closes, fast = 12, slow = 26, signal = 9) {
  const emaFast = ema(closes, fast);
  const emaSlow = ema(closes, slow);
  const macdLine = emaFast.map((f, i) =>
    f !== null && emaSlow[i] !== null ? f - emaSlow[i] : null
  );
  const validMacd = macdLine.filter(v => v !== null);
  const signalLine = ema(validMacd, signal);

  const padded = new Array(macdLine.length - validMacd.length).fill(null);
  const fullSignal = [...padded, ...signalLine];

  const histogram = macdLine.map((m, i) =>
    m !== null && fullSignal[i] !== null ? m - fullSignal[i] : null
  );

  return { macdLine, signalLine: fullSignal, histogram };
}

export function bollingerBands(closes, period = 20, stdDev = 2) {
  const mid = sma(closes, period);
  const upper = [];
  const lower = [];
  const bandwidth = [];

  for (let i = 0; i < closes.length; i++) {
    if (mid[i] === null) {
      upper.push(null); lower.push(null); bandwidth.push(null);
      continue;
    }
    const slice = closes.slice(i - period + 1, i + 1);
    const mean = mid[i];
    const std = Math.sqrt(slice.reduce((s, v) => s + (v - mean) ** 2, 0) / period);
    upper.push(mean + stdDev * std);
    lower.push(mean - stdDev * std);
    bandwidth.push(mid[i] > 0 ? ((stdDev * 2 * std) / mid[i]) * 100 : 0);
  }

  return { upper, middle: mid, lower, bandwidth };
}

export function atr(candles, period = 14) {
  const trs = [];
  for (let i = 0; i < candles.length; i++) {
    if (i === 0) {
      trs.push(candles[i].high - candles[i].low);
      continue;
    }
    const prevClose = candles[i - 1].close;
    const tr = Math.max(
      candles[i].high - candles[i].low,
      Math.abs(candles[i].high - prevClose),
      Math.abs(candles[i].low - prevClose)
    );
    trs.push(tr);
  }

  const result = [];
  for (let i = 0; i < trs.length; i++) {
    if (i < period - 1) { result.push(null); continue; }
    if (i === period - 1) {
      result.push(trs.slice(0, period).reduce((a, b) => a + b, 0) / period);
    } else {
      result.push((result[result.length - 1] * (period - 1) + trs[i]) / period);
    }
  }
  return result;
}

export function vwap(candles) {
  let cumVolPrice = 0;
  let cumVol = 0;
  return candles.map(c => {
    const typical = (c.high + c.low + c.close) / 3;
    cumVolPrice += typical * c.volume;
    cumVol += c.volume;
    return cumVol > 0 ? cumVolPrice / cumVol : c.close;
  });
}

/**
 * Compute all indicators for a set of candles.
 * Returns structured object with latest values.
 */
export function analyzeCandles(candles, opts = {}) {
  const {
    rsiPeriod = 9,
    emaFast = 9,
    emaSlow = 21,
    macdFast = 5,
    macdSlow = 13,
    macdSignal = 6,
    bbPeriod = 20,
    bbStdDev = 2,
    atrPeriod = 14,
  } = opts;

  const closes = candles.map(c => c.close);
  const last = (arr) => { for (let i = arr.length - 1; i >= 0; i--) if (arr[i] !== null) return arr[i]; return null; };
  const prev = (arr) => { let found = 0; for (let i = arr.length - 1; i >= 0; i--) { if (arr[i] !== null) { if (found++ === 1) return arr[i]; } } return null; };

  const rsiValues = rsi(closes, rsiPeriod);
  const emaFastValues = ema(closes, emaFast);
  const emaSlowValues = ema(closes, emaSlow);
  const macdResult = macd(closes, macdFast, macdSlow, macdSignal);
  const bbResult = bollingerBands(closes, bbPeriod, bbStdDev);
  const atrValues = atr(candles, atrPeriod);
  const vwapValues = vwap(candles);

  const currentPrice = closes[closes.length - 1];
  const currentRSI = last(rsiValues);
  const currentEmaFast = last(emaFastValues);
  const currentEmaSlow = last(emaSlowValues);
  const prevEmaFast = prev(emaFastValues);
  const prevEmaSlow = prev(emaSlowValues);
  const currentMacd = last(macdResult.macdLine);
  const currentMacdSignal = last(macdResult.signalLine);
  const currentMacdHist = last(macdResult.histogram);
  const currentBBUpper = last(bbResult.upper);
  const currentBBLower = last(bbResult.lower);
  const currentBBBandwidth = last(bbResult.bandwidth);
  const currentATR = last(atrValues);
  const currentVWAP = last(vwapValues);

  // EMA cross detection
  let emaCross = 'none';
  if (prevEmaFast !== null && prevEmaSlow !== null && currentEmaFast !== null && currentEmaSlow !== null) {
    if (prevEmaFast <= prevEmaSlow && currentEmaFast > currentEmaSlow) emaCross = 'bullish';
    if (prevEmaFast >= prevEmaSlow && currentEmaFast < currentEmaSlow) emaCross = 'bearish';
  }

  // BB squeeze detection (bandwidth < 2%)
  const bbSqueeze = currentBBBandwidth !== null && currentBBBandwidth < 2;

  // BB position
  let bbPosition = 'middle';
  if (currentPrice && currentBBUpper && currentBBLower) {
    const range = currentBBUpper - currentBBLower;
    if (range > 0) {
      const pct = (currentPrice - currentBBLower) / range;
      if (pct > 0.9) bbPosition = 'upper';
      else if (pct < 0.1) bbPosition = 'lower';
    }
  }

  return {
    price: currentPrice,
    rsi: currentRSI ? parseFloat(currentRSI.toFixed(2)) : null,
    ema: {
      fast: currentEmaFast ? parseFloat(currentEmaFast.toFixed(4)) : null,
      slow: currentEmaSlow ? parseFloat(currentEmaSlow.toFixed(4)) : null,
      cross: emaCross,
      trend: currentEmaFast > currentEmaSlow ? 'bullish' : 'bearish',
    },
    macd: {
      line: currentMacd ? parseFloat(currentMacd.toFixed(6)) : null,
      signal: currentMacdSignal ? parseFloat(currentMacdSignal.toFixed(6)) : null,
      histogram: currentMacdHist ? parseFloat(currentMacdHist.toFixed(6)) : null,
      trend: currentMacdHist > 0 ? 'bullish' : 'bearish',
    },
    bb: {
      upper: currentBBUpper ? parseFloat(currentBBUpper.toFixed(4)) : null,
      lower: currentBBLower ? parseFloat(currentBBLower.toFixed(4)) : null,
      bandwidth: currentBBBandwidth ? parseFloat(currentBBBandwidth.toFixed(2)) : null,
      squeeze: bbSqueeze,
      position: bbPosition,
    },
    atr: currentATR ? parseFloat(currentATR.toFixed(6)) : null,
    vwap: currentVWAP ? parseFloat(currentVWAP.toFixed(4)) : null,
    vwapDelta: currentVWAP && currentPrice ? parseFloat(((currentPrice - currentVWAP) / currentVWAP * 100).toFixed(2)) : null,
  };
}
