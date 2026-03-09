#!/usr/bin/env node
/**
 * Search for airports/cities via Duffel Places API.
 *
 * Usage:
 *   node locations.mjs --query "Rome"
 *   node locations.mjs --query "FCO"
 */

import { duffelGet, parseArgs, output } from "./lib.mjs";

const args = parseArgs();
const query = args.query;

if (!query) {
  console.error("Usage: node locations.mjs --query <city or airport name>");
  process.exit(1);
}

try {
  const data = await duffelGet("/places/suggestions", { query });

  const locations = (data.data || []).map((place) => ({
    id: place.id,
    name: place.name,
    iata_code: place.iata_code,
    type: place.type, // airport, city
    city_name: place.city_name || place.city?.name,
    country: place.iata_country_code,
    latitude: place.latitude,
    longitude: place.longitude,
  }));

  output({ results: locations, count: locations.length });
} catch (err) {
  output({ error: err.message });
  process.exit(1);
}
