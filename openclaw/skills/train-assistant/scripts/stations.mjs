#!/usr/bin/env node
/**
 * Search train stations via Trenitalia BFF and ViaggiaTreno APIs.
 *
 * Usage:
 *   node stations.mjs --query "Roma"
 *   node stations.mjs --query "Firenze"
 */

import {
  trenitaliaInit,
  trenitaliaGet,
  vtGet,
  parseArgs,
  output,
} from "./lib.mjs";

const args = parseArgs();
const query = args.query;

if (!query) {
  console.error("Usage: node stations.mjs --query <city or station name>");
  process.exit(1);
}

async function searchTrenitaliaStations(q) {
  try {
    await trenitaliaInit();
    const results = await trenitaliaGet("locations/search", { name: q, limit: 10 });
    return (results || []).map((r) => ({
      provider: "trenitalia",
      id: r.id,
      name: r.name,
      display_name: r.displayName,
      multistation: r.multistation || false,
    }));
  } catch (err) {
    return [{ provider: "trenitalia", error: err.message }];
  }
}

async function searchViaggiatrenoStations(q) {
  try {
    const text = await vtGet(`autocompletaStazione/${encodeURIComponent(q)}`);
    if (!text || typeof text !== "string") return [];
    return text
      .trim()
      .split("\n")
      .filter(Boolean)
      .map((line) => {
        const [name, code] = line.split("|");
        return {
          provider: "viaggiatreno",
          code: code?.trim(),
          name: name?.trim(),
        };
      });
  } catch {
    return [];
  }
}

try {
  const allResults = (await Promise.all([
    searchTrenitaliaStations(query),
    searchViaggiatrenoStations(query),
  ])).flat();

  output({
    query,
    count: allResults.length,
    results: allResults,
  });
} catch (err) {
  output({ error: err.message });
  process.exit(1);
}
