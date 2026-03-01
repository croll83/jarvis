#!/usr/bin/env node
/**
 * reconcile-transactions.mjs
 * Reconciles ontology Transaction entities with actual Hyperliquid fills.
 *
 * Logic:
 * 1. Fetch all Transaction entities from ontology
 * 2. Fetch recent fills from Hyperliquid
 * 3. Group HL fills by (coin, side, timestamp) to reconstruct orders
 * 4. For ontology txns with closed_pnl == 0 or null:
 *    - Match to HL fill group by hl_oid, or coin+side+timestamp(±2min)
 *    - If matched fill has pnl != 0 → PATCH closed_pnl + status="closed"
 * 5. For HL fill groups with pnl != 0 and no matching ontology txn:
 *    - Create new Transaction entity
 * 6. Chiama assign-trade-ids per assicurarsi che tutte le tx abbiano trade_id
 *
 * Usage: node scripts/reconcile-transactions.mjs [--dry-run]
 */

import { HLClient } from '/home/jarvis/.openclaw/workspace/skills/trading-engine/scripts/lib/hl-client.mjs';

const ONTOLOGY_URL = process.env.ONTOLOGY_URL || 'http://127.0.0.1:8100';
const ONTOLOGY_TOKEN = process.env.ONTOLOGY_API_TOKEN;
const HL_ADDRESS = '0x2bA250B38673Ff0Ac9c8d931B1AF17Ee6f66871b';
const DRY_RUN = process.argv.includes('--dry-run');
const MATCH_WINDOW_MS = 2 * 60 * 1000; // ±2 minutes

if (!ONTOLOGY_TOKEN) {
  console.error('Missing ONTOLOGY_API_TOKEN env var');
  process.exit(1);
}

const headers = {
  'X-Speaker-Id': 'marco',
  'Authorization': `Bearer ${ONTOLOGY_TOKEN}`,
  'Content-Type': 'application/json',
};

// --- Ontology helpers ---

async function fetchTransactions() {
  const res = await fetch(`${ONTOLOGY_URL}/entities?type=Transaction`, { headers });
  if (!res.ok) throw new Error(`Ontology GET failed: ${res.status} ${await res.text()}`);
  return await res.json();
}

async function patchTransaction(id, props) {
  if (DRY_RUN) {
    console.log(`  [DRY-RUN] Would PATCH ${id}:`, JSON.stringify(props));
    return { id, patched: true };
  }
  const res = await fetch(`${ONTOLOGY_URL}/entities/${id}`, {
    method: 'PATCH',
    headers,
    body: JSON.stringify({ properties: props }),
  });
  if (!res.ok) throw new Error(`Ontology PATCH ${id} failed: ${res.status} ${await res.text()}`);
  return await res.json();
}

async function createTransaction(props) {
  if (DRY_RUN) {
    console.log(`  [DRY-RUN] Would CREATE:`, JSON.stringify(props));
    return { id: 'dry_run', created: true };
  }
  const res = await fetch(`${ONTOLOGY_URL}/entities`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ type: 'Transaction', properties: props }),
  });
  if (!res.ok) throw new Error(`Ontology CREATE failed: ${res.status} ${await res.text()}`);
  return await res.json();
}

// --- Fill grouping ---

/**
 * Group fills that belong to the same order (same coin, side, timestamp).
 * HL splits large orders into multiple fills at the same millisecond.
 */
function groupFills(fills) {
  const groups = [];
  const grouped = new Map();

  for (const f of fills) {
    const key = `${f.coin}|${f.side}|${f.time}`;
    if (!grouped.has(key)) {
      grouped.set(key, {
        coin: f.coin,
        side: f.side,
        time: f.time,
        fills: [],
        totalSize: 0,
        totalPnl: 0,
        totalFee: 0,
        avgPrice: 0,
      });
    }
    const g = grouped.get(key);
    const sz = parseFloat(f.size);
    const px = parseFloat(f.price);
    const pnl = parseFloat(f.pnl) || 0;
    const fee = parseFloat(f.fee) || 0;
    g.fills.push(f);
    g.totalSize += sz;
    g.totalPnl += pnl;
    g.totalFee += fee;
    // weighted avg price
    g.avgPrice = (g.avgPrice * (g.totalSize - sz) + px * sz) / g.totalSize;
  }

  return Array.from(grouped.values());
}

// --- Matching ---

function matchByTimestamp(txn, fillGroup) {
  const txnTime = new Date(txn.properties.timestamp).getTime();
  const fillTime = new Date(fillGroup.time).getTime();
  const timeDiff = Math.abs(txnTime - fillTime);

  const txnCoin = txn.properties.asset_in?.includes('-PERP')
    ? txn.properties.asset_in
    : txn.properties.asset_out;
  const txnSide = txn.properties.type; // buy or sell

  return (
    timeDiff <= MATCH_WINDOW_MS &&
    txnCoin === fillGroup.coin &&
    txnSide === fillGroup.side
  );
}

function matchBySize(txn, fillGroup) {
  const txnSize = txn.properties.amount_in && txn.properties.asset_in?.includes('-PERP')
    ? txn.properties.amount_in
    : txn.properties.amount_out;
  const sizeDiff = Math.abs(parseFloat(txnSize) - fillGroup.totalSize);
  return sizeDiff < 0.001; // small tolerance for rounding
}

// --- Main ---

async function main() {
  console.log('=== Reconcile Transactions ===');
  console.log(`Ontology: ${ONTOLOGY_URL}`);
  console.log(`HL Address: ${HL_ADDRESS}`);
  console.log(`Dry run: ${DRY_RUN}`);
  console.log('');

  // 1. Fetch data
  console.log('Fetching ontology transactions...');
  const txns = await fetchTransactions();
  console.log(`  Found ${txns.length} transactions`);

  console.log('Fetching Hyperliquid fills...');
  const hl = new HLClient({ address: HL_ADDRESS });
  const rawFills = await hl.getFills(200);
  console.log(`  Found ${rawFills.length} fills`);

  const fillGroups = groupFills(rawFills);
  console.log(`  Grouped into ${fillGroups.length} orders`);
  console.log('');

  // 2. Identify txns needing reconciliation
  const needsReconciliation = txns.filter(t => {
    const pnl = t.properties.closed_pnl;
    return pnl === undefined || pnl === null || pnl === 0 || pnl === 0.0;
  });
  const alreadyReconciled = txns.filter(t => {
    const pnl = t.properties.closed_pnl;
    return pnl !== undefined && pnl !== null && pnl !== 0;
  });

  console.log(`Transactions to check: ${needsReconciliation.length}`);
  console.log(`Already reconciled: ${alreadyReconciled.length}`);
  console.log('');

  // Track matched fill groups
  const matchedFillKeys = new Set();
  // Also mark already-reconciled txns' fill groups as matched
  for (const txn of alreadyReconciled) {
    for (const fg of fillGroups) {
      if (matchByTimestamp(txn, fg) && matchBySize(txn, fg)) {
        matchedFillKeys.add(`${fg.coin}|${fg.side}|${fg.time}`);
      }
    }
  }

  let updated = 0;
  let alreadyCorrect = 0;
  let created = 0;

  // 3. Reconcile existing transactions
  console.log('--- Reconciling existing transactions ---');
  for (const txn of needsReconciliation) {
    const txnCoin = txn.properties.asset_in?.includes('-PERP')
      ? txn.properties.asset_in
      : txn.properties.asset_out;

    // Find matching fill group
    let bestMatch = null;
    for (const fg of fillGroups) {
      if (matchByTimestamp(txn, fg) && matchBySize(txn, fg)) {
        bestMatch = fg;
        break;
      }
    }

    if (!bestMatch) {
      // Try looser match (just coin + side + time, ignore size)
      for (const fg of fillGroups) {
        if (matchByTimestamp(txn, fg)) {
          bestMatch = fg;
          break;
        }
      }
    }

    if (bestMatch) {
      matchedFillKeys.add(`${bestMatch.coin}|${bestMatch.side}|${bestMatch.time}`);

      if (Math.abs(bestMatch.totalPnl) > 0.0001) {
        // This was a closing fill — update
        const roundedPnl = Math.round(bestMatch.totalPnl * 100000) / 100000;
        console.log(`  UPDATE ${txn.id}: ${txnCoin} ${txn.properties.type} → closed_pnl=${roundedPnl}`);
        await patchTransaction(txn.id, {
          closed_pnl: roundedPnl,
          status: 'closed',
        });
        updated++;
      } else {
        // Opening fill, pnl=0 is correct
        // Ensure closed_pnl field exists
        if (txn.properties.closed_pnl === undefined || txn.properties.closed_pnl === null) {
          console.log(`  NORMALIZE ${txn.id}: adding closed_pnl=0`);
          await patchTransaction(txn.id, { closed_pnl: 0.0 });
          updated++;
        } else {
          console.log(`  OK ${txn.id}: ${txnCoin} ${txn.properties.type} — opening fill, closed_pnl=0 correct`);
          alreadyCorrect++;
        }
      }
    } else {
      console.log(`  WARN ${txn.id}: no matching HL fill for ${txnCoin} ${txn.properties.type} @ ${txn.properties.timestamp}`);
      alreadyCorrect++;
    }
  }

  console.log('');

  // 4. Create missing transactions for unmatched closing fills
  console.log('--- Creating missing transactions ---');
  for (const fg of fillGroups) {
    const key = `${fg.coin}|${fg.side}|${fg.time}`;
    if (matchedFillKeys.has(key)) continue;

    // Determine asset_in/asset_out based on side
    let props;
    if (fg.side === 'buy') {
      props = {
        type: 'buy',
        asset_in: 'USDC',
        amount_in: Math.round(fg.avgPrice * fg.totalSize * 100) / 100,
        asset_out: fg.coin,
        amount_out: Math.round(fg.totalSize * 10000) / 10000,
        status: Math.abs(fg.totalPnl) > 0.0001 ? 'closed' : 'confirmed',
        timestamp: fg.time,
        closed_pnl: Math.round(fg.totalPnl * 100000) / 100000,
        owner: 'jarvis-agent',
        visibility: 'family',
        account: 'acco_78a3491e',
        executor: 'jarvis-agent',
      };
    } else {
      props = {
        type: 'sell',
        asset_in: fg.coin,
        amount_in: Math.round(fg.totalSize * 10000) / 10000,
        asset_out: 'USDC',
        amount_out: Math.round(fg.avgPrice * fg.totalSize * 100) / 100,
        status: Math.abs(fg.totalPnl) > 0.0001 ? 'closed' : 'confirmed',
        timestamp: fg.time,
        closed_pnl: Math.round(fg.totalPnl * 100000) / 100000,
        owner: 'jarvis-agent',
        visibility: 'family',
        account: 'acco_78a3491e',
        executor: 'jarvis-agent',
      };
    }

    console.log(`  CREATE: ${fg.coin} ${fg.side} ${fg.totalSize} @ ${fg.time} (pnl=${fg.totalPnl.toFixed(5)})`);
    await createTransaction(props);
    created++;
  }

  console.log('');
  console.log('=== RIEPILOGO ===');
  console.log(`Transazioni aggiornate: ${updated}`);
  console.log(`Transazioni create: ${created}`);
  console.log(`Transazioni già corrette: ${alreadyCorrect}`);
  console.log(`Totale ontology (prima): ${txns.length}`);
  console.log(`Totale ontology (dopo): ${txns.length + created}`);
}

async function assignTradeIds() {
  console.log('');
  console.log('=== Assign Trade IDs (post-reconcile) ===');
  // Importa ed esegue la logica di assign-trade-ids inline per evitare fork
  const assignScript = new URL('./assign-trade-ids.mjs', import.meta.url);
  const { randomUUID } = await import('crypto');
  const SIZE_TOLERANCE = 0.0001;

  const reconcileHeaders = {
    'X-Speaker-Id': 'jarvis-agent',
    'Authorization': `Bearer ${ONTOLOGY_TOKEN}`,
    'Content-Type': 'application/json',
  };

  function getCoin(tx) {
    const { asset_in, asset_out } = tx.properties;
    if (asset_in && asset_in !== 'USDC') return asset_in;
    if (asset_out && asset_out !== 'USDC') return asset_out;
    return null;
  }

  function getSize(tx) {
    const { type, amount_in, amount_out } = tx.properties;
    if (type === 'buy') return parseFloat(amount_out) || 0;
    if (type === 'sell') return parseFloat(amount_in) || 0;
    return 0;
  }

  // Fetch aggiornata dopo la riconciliazione
  const res = await fetch(`${ONTOLOGY_URL}/entities?type=Transaction`, { headers: reconcileHeaders });
  if (!res.ok) { console.warn('assign-trade-ids: fetch failed'); return; }
  const txns = await res.json();

  const needsId = txns.filter(t => !t.properties.trade_id);
  console.log(`Transazioni senza trade_id: ${needsId.length}`);
  if (needsId.length === 0) { console.log('Nessuna bonifica necessaria.'); return; }

  needsId.sort((a, b) => {
    return new Date(a.properties.timestamp || 0) - new Date(b.properties.timestamp || 0);
  });

  const buys  = needsId.filter(t => t.properties.type === 'buy');
  const sells = needsId.filter(t => t.properties.type === 'sell');
  const matchedSellIds = new Set();
  const patches = [];

  for (const buy of buys) {
    const buyCoin = getCoin(buy);
    const buySize = getSize(buy);
    const buyTime = new Date(buy.properties.timestamp || 0).getTime();
    const matchingSell = sells.find(sell => {
      if (matchedSellIds.has(sell.id)) return false;
      return (
        getCoin(sell) === buyCoin &&
        Math.abs(getSize(sell) - buySize) <= SIZE_TOLERANCE &&
        new Date(sell.properties.timestamp || 0).getTime() > buyTime
      );
    });
    if (matchingSell) {
      const tradeId = randomUUID();
      matchedSellIds.add(matchingSell.id);
      patches.push({ id: buy.id, tradeId });
      patches.push({ id: matchingSell.id, tradeId });
    } else {
      patches.push({ id: buy.id, tradeId: randomUUID() });
    }
  }
  for (const sell of sells) {
    if (!matchedSellIds.has(sell.id)) {
      patches.push({ id: sell.id, tradeId: randomUUID() });
    }
  }

  let errors = 0;
  for (const patch of patches) {
    if (DRY_RUN) {
      console.log(`  [DRY-RUN] PATCH ${patch.id} → trade_id: ${patch.tradeId}`);
      continue;
    }
    try {
      const patchRes = await fetch(`${ONTOLOGY_URL}/entities/${patch.id}`, {
        method: 'PATCH',
        headers: reconcileHeaders,
        body: JSON.stringify({ properties: { trade_id: patch.tradeId } }),
      });
      if (!patchRes.ok) throw new Error(`${patchRes.status}`);
      console.log(`  ✓ ${patch.id} → trade_id: ${patch.tradeId.slice(0, 8)}…`);
    } catch (e) {
      console.error(`  ✗ ${patch.id}: ${e.message}`);
      errors++;
    }
  }

  console.log(`Trade IDs assegnati: ${patches.length - errors}, errori: ${errors}`);
}

main()
  .then(() => assignTradeIds())
  .then(() => process.exit(0))
  .catch(err => {
    console.error('ERRORE:', err);
    process.exit(1);
  });
