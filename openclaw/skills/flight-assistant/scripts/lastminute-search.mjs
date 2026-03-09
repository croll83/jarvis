#!/usr/bin/env node
/**
 * Search flights via lastminute.com MCP server for price comparison.
 *
 * This script calls the lastminute.com MCP endpoint to get flight results
 * as a secondary source for price comparison against Kiwi/Tequila.
 *
 * Usage:
 *   node lastminute-search.mjs --from "Rome" --to "New York" --date 2026-04-01 [options]
 *
 * Options:
 *   --return <date>    Return date (YYYY-MM-DD)
 *   --adults <N>       Number of adults (default: 1)
 *   --cabins <class>   economy, premium_economy, business, first (default: economy)
 *   --currency <code>  Currency (default: EUR)
 *
 * Note: This uses the MCP JSON-RPC protocol over HTTP.
 * The lastminute.com MCP server may have different availability than Tequila.
 */

import { parseArgs, output } from "./lib.mjs";

const MCP_ENDPOINT = "https://mcp.lastminute.com/mcp";

const args = parseArgs();

if (!args.from || !args.to || !args.date) {
  console.error("Usage: node lastminute-search.mjs --from <city> --to <city> --date <YYYY-MM-DD>");
  process.exit(1);
}

// Convert date to DD/MM/YYYY
function toDisplayDate(d) {
  if (!d) return undefined;
  if (/^\d{2}\/\d{2}\/\d{4}$/.test(d)) return d;
  const [y, m, day] = d.split("-");
  return `${day}/${m}/${y}`;
}

// Build MCP JSON-RPC request
const mcpRequest = {
  jsonrpc: "2.0",
  id: 1,
  method: "tools/call",
  params: {
    name: "search-flight",
    arguments: {
      flyFrom: args.from,
      flyTo: args.to,
      departureDate: toDisplayDate(args.date),
      returnDate: args.return ? toDisplayDate(args.return) : undefined,
      passengers: {
        adults: parseInt(args.adults || "1", 10),
        children: parseInt(args.children || "0", 10),
        infants: parseInt(args.infants || "0", 10),
      },
      cabinClass: args.cabins === "business" ? "C" : args.cabins === "first" ? "F" : args.cabins === "premium_economy" ? "W" : "M",
      curr: args.currency || "EUR",
      sort: "price",
      locale: "en",
    },
  },
};

try {
  const res = await fetch(MCP_ENDPOINT, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify(mcpRequest),
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`lastminute.com MCP ${res.status}: ${text}`);
  }

  const data = await res.json();

  if (data.error) {
    throw new Error(`MCP error: ${JSON.stringify(data.error)}`);
  }

  // MCP response contains the result in data.result
  output({
    source: "lastminute.com",
    result: data.result,
  });
} catch (err) {
  output({
    source: "lastminute.com",
    error: err.message,
    note: "lastminute.com MCP search failed. Use Tequila/Kiwi results as primary source.",
  });
  process.exit(1);
}
