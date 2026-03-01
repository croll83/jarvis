#!/usr/bin/env node
/**
 * assign-trade-ids.mjs
 * Bonifica: assegna trade_id a tutte le Transaction senza, accoppiando buy/sell.
 *
 * Logica:
 *  - Per ogni buy senza trade_id: cerca sell successiva (stessa coin, stessa size)
 *    → se trovata: UUID condiviso per entrambe
 *    → se non trovata: UUID proprio (posizione ancora aperta)
 *  - Per le sell non accoppiate: UUID proprio
 *
 * Usage: node scripts/assign-trade-ids.mjs [--dry-run]
 */

import { randomUUID } from 'crypto';

const ONTOLOGY_URL = process.env.ONTOLOGY_URL || 'http://127.0.0.1:8100';
const ONTOLOGY_TOKEN = process.env.ONTOLOGY_API_TOKEN;
const DRY_RUN = process.argv.includes('--dry-run');
const SIZE_TOLERANCE = 0.0001;

if (!ONTOLOGY_TOKEN) {
  console.error('Missing ONTOLOGY_API_TOKEN env var');
  process.exit(1);
}

const headers = {
  'X-Speaker-Id': 'jarvis-agent',
  'Authorization': `Bearer ${ONTOLOGY_TOKEN}`,
  'Content-Type': 'application/json',
};

async function fetchTransactions() {
  const res = await fetch(`${ONTOLOGY_URL}/entities?type=Transaction`, { headers });
  if (!res.ok) throw new Error(`Fetch failed: ${res.status} ${await res.text()}`);
  return res.json();
}

async function patchTradeId(id, tradeId) {
  if (DRY_RUN) {
    console.log(`  [DRY-RUN] PATCH ${id} → trade_id: ${tradeId}`);
    return true;
  }
  const res = await fetch(`${ONTOLOGY_URL}/entities/${id}`, {
    method: 'PATCH',
    headers,
    body: JSON.stringify({ properties: { trade_id: tradeId } }),
  });
  if (!res.ok) {
    const errText = await res.text();
    throw new Error(`PATCH ${id} failed: ${res.status} ${errText}`);
  }
  return true;
}

function getCoin(tx) {
  // Il coin è l'asset perpetual (contiene '-PERP' o è la cripto stessa)
  const { asset_in, asset_out } = tx.properties;
  if (asset_in && asset_in !== 'USDC') return asset_in;
  if (asset_out && asset_out !== 'USDC') return asset_out;
  return null;
}

function getSize(tx) {
  const { type, amount_in, amount_out } = tx.properties;
  // Per buy: size = amount_out (coin ottenuto)
  // Per sell: size = amount_in (coin ceduto)
  if (type === 'buy') return parseFloat(amount_out) || 0;
  if (type === 'sell') return parseFloat(amount_in) || 0;
  return 0;
}

async function main() {
  console.log('=== Assign Trade IDs ===');
  console.log(`Ontology: ${ONTOLOGY_URL}`);
  console.log(`Dry run: ${DRY_RUN}`);
  console.log('');

  const txns = await fetchTransactions();
  console.log(`Trovate ${txns.length} transazioni totali`);

  // Filtra solo quelle senza trade_id
  const needsId = txns.filter(t => !t.properties.trade_id);
  console.log(`Senza trade_id: ${needsId.length}`);
  console.log('');

  if (needsId.length === 0) {
    console.log('Nessuna transazione da bonificare. Uscita.');
    return { matched: 0, singles: 0, errors: 0 };
  }

  // Ordina per timestamp ASC
  needsId.sort((a, b) => {
    const ta = new Date(a.properties.timestamp || 0).getTime();
    const tb = new Date(b.properties.timestamp || 0).getTime();
    return ta - tb;
  });

  const buys  = needsId.filter(t => t.properties.type === 'buy');
  const sells = needsId.filter(t => t.properties.type === 'sell');

  // Set di sell già accoppiate (per id)
  const matchedSellIds = new Set();

  // Patches da applicare: array di { id, tradeId }
  const patches = [];

  let matchedPairs = 0;
  let singleBuys = 0;
  let singleSells = 0;
  let errors = 0;

  // 1. Accopia ogni buy con la prima sell successiva compatibile
  console.log('--- Accoppiamento buy → sell ---');
  for (const buy of buys) {
    const buyCoin = getCoin(buy);
    const buySize = getSize(buy);
    const buyTime = new Date(buy.properties.timestamp || 0).getTime();

    // Cerca sell successiva con stessa coin e stessa size
    const matchingSell = sells.find(sell => {
      if (matchedSellIds.has(sell.id)) return false;
      const sellCoin = getCoin(sell);
      const sellSize = getSize(sell);
      const sellTime = new Date(sell.properties.timestamp || 0).getTime();
      return (
        sellCoin === buyCoin &&
        Math.abs(sellSize - buySize) <= SIZE_TOLERANCE &&
        sellTime > buyTime
      );
    });

    if (matchingSell) {
      const tradeId = randomUUID();
      matchedSellIds.add(matchingSell.id);
      patches.push({ id: buy.id, tradeId });
      patches.push({ id: matchingSell.id, tradeId });
      console.log(`  PAIR [${tradeId.slice(0, 8)}…] ${buy.id} (buy ${buyCoin} ${buySize}) ↔ ${matchingSell.id} (sell ${getCoin(matchingSell)} ${getSize(matchingSell)})`);
      matchedPairs++;
    } else {
      // Posizione aperta o senza chiusura tracciata
      const tradeId = randomUUID();
      patches.push({ id: buy.id, tradeId });
      console.log(`  SOLO [${tradeId.slice(0, 8)}…] ${buy.id} (buy ${buyCoin} ${buySize}) — nessuna sell corrispondente`);
      singleBuys++;
    }
  }

  // 2. Sell non accoppiate
  console.log('');
  console.log('--- Sell non accoppiate ---');
  for (const sell of sells) {
    if (matchedSellIds.has(sell.id)) continue;
    const tradeId = randomUUID();
    patches.push({ id: sell.id, tradeId });
    console.log(`  SOLO [${tradeId.slice(0, 8)}…] ${sell.id} (sell ${getCoin(sell)} ${getSize(sell)}) — nessun buy corrispondente`);
    singleSells++;
  }

  // 3. Applica tutti i PATCH
  console.log('');
  console.log(`--- Applicazione ${patches.length} PATCH ---`);
  for (const patch of patches) {
    try {
      await patchTradeId(patch.id, patch.tradeId);
      if (!DRY_RUN) console.log(`  ✓ ${patch.id} → ${patch.tradeId.slice(0, 8)}…`);
    } catch (e) {
      console.error(`  ✗ ${patch.id}: ${e.message}`);
      errors++;
    }
  }

  console.log('');
  console.log('=== RIEPILOGO ===');
  console.log(`Coppie buy/sell accoppiate: ${matchedPairs}`);
  console.log(`Buy singoli (posizione aperta): ${singleBuys}`);
  console.log(`Sell singole (chiusura senza apertura tracciata): ${singleSells}`);
  console.log(`Totale transazioni con trade_id assegnato: ${patches.length}`);
  console.log(`Errori PATCH: ${errors}`);

  return { matched: matchedPairs, singles: singleBuys + singleSells, errors };
}

main().catch(err => {
  console.error('ERRORE:', err);
  process.exit(1);
});
