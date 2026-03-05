/**
 * Hyperliquid SDK wrapper — unified client for account, trading, and market data.
 * Private key from env JARVIS_WALLET (injected at runtime by TPM preboot).
 * Address from env HYPERLIQUID_ADDRESS.
 */

import { Hyperliquid } from 'hyperliquid';

const TICK_SIZES = [
  { max: 0.001, tick: 0.0000001, dp: 7 },
  { max: 0.01, tick: 0.000001, dp: 6 },
  { max: 0.1, tick: 0.00001, dp: 5 },
  { max: 1, tick: 0.0001, dp: 4 },
  { max: 10, tick: 0.001, dp: 3 },
  { max: 100, tick: 0.01, dp: 2 },
  { max: 1000, tick: 0.1, dp: 1 },
  { max: 10000, tick: 1, dp: 0 },
  { max: Infinity, tick: 1, dp: 0 },
];

function getTickInfo(price) {
  for (const t of TICK_SIZES) {
    if (price < t.max) return t;
  }
  return TICK_SIZES[TICK_SIZES.length - 1];
}

function roundToTick(price, tick, dp) {
  return parseFloat((Math.round(price / tick) * tick).toFixed(dp));
}

function slippagePrice(price, side, slippagePct = 0.5) {
  const { tick, dp } = getTickInfo(price);
  const factor = side === 'buy' ? 1 + slippagePct / 100 : 1 - slippagePct / 100;
  return roundToTick(price * factor, tick, dp);
}

export class HLClient {
  constructor(opts = {}) {
    this.address = opts.address || process.env.HYPERLIQUID_ADDRESS;
    this.privateKey = opts.privateKey || process.env.JARVIS_WALLET;
    this.testnet = opts.testnet || false;
    this._sdk = null;
    this._readOnly = !this.privateKey;
  }

  async sdk() {
    if (this._sdk) return this._sdk;
    this._sdk = new Hyperliquid({
      privateKey: this.privateKey || undefined,
      testnet: this.testnet,
      walletAddress: this.address || undefined, enableWs: false,
    });
    await this._sdk.connect();
    return this._sdk;
  }

  // --- Account ---

  async getBalance(address) {
    const sdk = await this.sdk();
    const target = address || this.address;

    // Fetch all account sections in parallel
    const [perpState, spotState, vaultEquities, delegatorSummary] = await Promise.all([
      sdk.info.perpetuals.getClearinghouseState(target),
      sdk.info.spot.getSpotClearinghouseState(target),
      sdk.info.getUserVaultEquities(target).catch(() => []),
      sdk.info.getDelegatorSummary(target).catch(() => null),
    ]);

    // Perps — in unified accounts, perp borrows collateral from spot USDC.
    // accountValue = totalRawUsd + position value. totalRawUsd is typically
    // negative (borrowed from spot). Don't add perpEquity to spot in overview.
    const perpEquity = parseFloat(perpState.marginSummary.accountValue);
    const margin = parseFloat(perpState.marginSummary.totalMarginUsed);
    const totalRawUsd = parseFloat(perpState.marginSummary.totalRawUsd || '0');
    // Unrealized PnL = accountValue - totalRawUsd (rawUsd is the deposited/borrowed base)
    const unrealizedPnl = perpEquity - totalRawUsd - margin;

    // Spot — value each token. USDC is 1:1, others need price lookup.
    const spotBalances = spotState.balances || [];
    let spotTotal = 0;
    const spotDetails = [];
    if (spotBalances.length > 0) {
      const mids = await sdk.info.getAllMids();
      for (const b of spotBalances) {
        const total = parseFloat(b.total);
        if (total === 0) continue;
        let usdValue;
        const coinName = b.coin.replace(/-SPOT$/, '');
        if (['USDC', 'USDT', 'USDT0', 'USDE', 'USDH'].includes(coinName)) {
          usdValue = total;
        } else {
          const price = parseFloat(mids[coinName] || 0);
          usdValue = total * price;
        }
        spotTotal += usdValue;
        spotDetails.push({ coin: coinName, amount: total, usdValue: usdValue });
      }
    }

    // Vaults
    const vaultTotal = (vaultEquities || []).reduce((sum, v) => sum + parseFloat(v.equity || 0), 0);

    // Staked
    const stakedTotal = delegatorSummary ? parseFloat(delegatorSummary.delegated || 0) : 0;

    // Overview — spot USDC already includes perp collateral in unified accounts,
    // so we only add vault + staked (NOT perpEquity to avoid double-counting).
    const overview = spotTotal + vaultTotal + stakedTotal;

    // Free spot = spot minus what's locked in perps (collateral)
    const spotFree = Math.max(0, spotTotal - perpEquity);

    return {
      overview: overview.toFixed(2),
      perps: {
        equity: perpEquity.toFixed(2),
        available: (perpEquity - margin).toFixed(2),
        marginUsed: margin.toFixed(2),
        marginRatio: perpEquity > 0 ? ((margin / perpEquity) * 100).toFixed(1) + '%' : '0%',
        unrealizedPnl: unrealizedPnl.toFixed(2),
      },
      spot: {
        total: spotFree.toFixed(2),
        count: spotDetails.length,
        balances: spotDetails,
      },
      vault: vaultTotal.toFixed(2),
      staked: stakedTotal.toFixed(2),
    };
  }

  async getPositions(address) {
    const sdk = await this.sdk();
    const target = address || this.address;
    const state = await sdk.info.perpetuals.getClearinghouseState(target);
    return state.assetPositions
      .map(p => p.position)
      .filter(p => parseFloat(p.szi) !== 0)
      .map(p => ({
        coin: p.coin,
        side: parseFloat(p.szi) > 0 ? 'LONG' : 'SHORT',
        size: Math.abs(parseFloat(p.szi)),
        entryPrice: parseFloat(p.entryPx),
        markPrice: parseFloat(p.positionValue) / Math.abs(parseFloat(p.szi)),
        pnl: parseFloat(p.unrealizedPnl).toFixed(2),
        leverage: p.leverage?.value || 'cross',
        liquidationPrice: p.liquidationPx ? parseFloat(p.liquidationPx) : null,
      }));
  }

  async getOrders() {
    const sdk = await this.sdk();
    const orders = await sdk.info.getUserOpenOrders(this.address);
    return orders.map(o => ({
      coin: o.coin,
      side: o.side,
      size: o.sz,
      price: o.limitPx,
      orderId: o.oid,
      timestamp: o.timestamp,
    }));
  }

  async getFills(limit = 20) {
    const sdk = await this.sdk();
    const fills = await sdk.info.getUserFills(this.address);
    return fills.slice(0, limit).map(f => ({
      coin: f.coin,
      side: f.side,
      size: f.sz,
      price: f.px,
      fee: f.fee,
      pnl: f.closedPnl,
      time: new Date(f.time).toISOString(),
    }));
  }

  // --- Market Data ---

  async getPrice(coin) {
    const sdk = await this.sdk();
    const mids = await sdk.info.getAllMids();
    return parseFloat(mids[coin]);
  }

  async getAllPrices() {
    const sdk = await this.sdk();
    return await sdk.info.getAllMids();
  }

  async getMeta() {
    const sdk = await this.sdk();
    return await sdk.info.perpetuals.getMeta();
  }

  async getCandles(coin, interval = '5m', limit = 100) {
    const sdk = await this.sdk();
    const now = Date.now();
    const intervalMs = {
      '1m': 60000, '5m': 300000, '15m': 900000, '1h': 3600000, '4h': 14400000,
    };
    const ms = intervalMs[interval] || 300000;
    const startTime = now - ms * limit;
    const candles = await sdk.info.getCandleSnapshot(coin, interval, startTime, now);
    return candles.map(c => ({
      time: c.t,
      open: parseFloat(c.o),
      high: parseFloat(c.h),
      low: parseFloat(c.l),
      close: parseFloat(c.c),
      volume: parseFloat(c.v),
    }));
  }

  async getFundingRate(coin) {
    const sdk = await this.sdk();
    const meta = await sdk.info.perpetuals.getMetaAndAssetCtxs();
    const assets = meta[1];
    const metaInfo = meta[0];
    const idx = metaInfo.universe.findIndex(u => u.name === coin);
    if (idx === -1) return null;
    const ctx = assets[idx];
    return {
      coin,
      fundingRate: parseFloat(ctx.funding),
      openInterest: parseFloat(ctx.openInterest),
      markPrice: parseFloat(ctx.markPx),
      oraclePrice: parseFloat(ctx.oraclePx),
    };
  }

  async getLeaderboard() {
    const resp = await fetch('https://stats-data.hyperliquid.xyz/Mainnet/leaderboard', {
      headers: { 'User-Agent': 'JARVIS/1.0' },
    });
    if (!resp.ok) throw new Error(`Leaderboard fetch failed: HTTP ${resp.status}`);
    const data = await resp.json();
    // Normalize to flat format for whale-monitor compatibility
    return (data.leaderboardRows || []).map(r => {
      const allTime = r.windowPerformances?.find(w => w[0] === 'allTime')?.[1] || {};
      const week = r.windowPerformances?.find(w => w[0] === 'week')?.[1] || {};
      return {
        ethAddress: r.ethAddress,
        displayName: r.displayName || null,
        accountValue: r.accountValue,
        allTimePnl: allTime.pnl || '0',
        allTimeRoi: allTime.roi || '0',
        windowPnl: week.pnl || allTime.pnl || '0',
      };
    });
  }

  async getUserState(address) {
    const sdk = await this.sdk();
    return await sdk.info.perpetuals.getClearinghouseState(address);
  }

  // --- Trading ---

  async marketBuy(coin, size, slippagePct = 0.5) {
    if (this._readOnly) throw new Error('No private key — read-only mode');
    const price = await this.getPrice(coin);
    const px = slippagePrice(price, 'buy', slippagePct);
    const sdk = await this.sdk();
    return await sdk.exchange.placeOrder({
      coin, is_buy: true, sz: size, limit_px: px, order_type: { limit: { tif: 'Ioc' } }, reduce_only: false,
    });
  }

  async marketSell(coin, size, slippagePct = 0.5) {
    if (this._readOnly) throw new Error('No private key — read-only mode');
    const price = await this.getPrice(coin);
    const px = slippagePrice(price, 'sell', slippagePct);
    const sdk = await this.sdk();
    return await sdk.exchange.placeOrder({
      coin, is_buy: false, sz: size, limit_px: px, order_type: { limit: { tif: 'Ioc' } }, reduce_only: false,
    });
  }

  async limitBuy(coin, size, price) {
    if (this._readOnly) throw new Error('No private key — read-only mode');
    const { tick, dp } = getTickInfo(price);
    const px = roundToTick(price, tick, dp);
    const sdk = await this.sdk();
    return await sdk.exchange.placeOrder({
      coin, is_buy: true, sz: size, limit_px: px, order_type: { limit: { tif: 'Gtc' } }, reduce_only: false,
    });
  }

  async limitSell(coin, size, price) {
    if (this._readOnly) throw new Error('No private key — read-only mode');
    const { tick, dp } = getTickInfo(price);
    const px = roundToTick(price, tick, dp);
    const sdk = await this.sdk();
    return await sdk.exchange.placeOrder({
      coin, is_buy: false, sz: size, limit_px: px, order_type: { limit: { tif: 'Gtc' } }, reduce_only: false,
    });
  }

  async closePosition(coin, slippagePct = 0.5) {
    if (this._readOnly) throw new Error('No private key — read-only mode');
    const positions = await this.getPositions();
    const pos = positions.find(p => p.coin === coin);
    if (!pos) throw new Error(`No open position for ${coin}`);
    if (pos.side === 'LONG') {
      return await this.marketSell(coin, pos.size, slippagePct);
    } else {
      return await this.marketBuy(coin, pos.size, slippagePct);
    }
  }

  async setLeverage(coin, leverage, mode = 'isolated') {
    if (this._readOnly) throw new Error('No private key — read-only mode');
    const sdk = await this.sdk();
    const meta = await this.getMeta();
    const asset = meta.universe.findIndex(u => u.name === coin);
    if (asset === -1) throw new Error(`Unknown coin: ${coin}`);
    return await sdk.exchange.updateLeverage(asset, mode === 'cross', leverage);
  }

  async cancelAll(coin) {
    if (this._readOnly) throw new Error('No private key — read-only mode');
    const sdk = await this.sdk();
    const orders = await this.getOrders();
    const toCancel = coin ? orders.filter(o => o.coin === coin) : orders;
    if (toCancel.length === 0) return { cancelled: 0 };
    const results = [];
    for (const o of toCancel) {
      const r = await sdk.exchange.cancelOrder({ coin: o.coin, o: o.orderId });
      results.push(r);
    }
    return { cancelled: results.length };
  }
}

// --- CLI entrypoint ---
if (process.argv[1] && process.argv[1].endsWith('hl-client.mjs')) {
  console.log('HLClient is a library module. Import it in your scripts.');
  console.log('Required env: JARVIS_WALLET (private key), HYPERLIQUID_ADDRESS');
  process.exit(0);
}
