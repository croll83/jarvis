#!/usr/bin/env node
/**
 * Search flights via Duffel Offer Requests API.
 *
 * Usage:
 *   node search-flights.mjs --from FCO --to JFK --date 2026-04-01 [options]
 *
 * Options:
 *   --return <date>         Return date (YYYY-MM-DD)
 *   --adults <N>            Number of adults (default: 1)
 *   --children <N>          Number of children with ages (e.g. "8,10")
 *   --infants <N>           Number of infants with ages (e.g. "1")
 *   --cabins <class>        economy, premium_economy, business, first (default: economy)
 *   --max-connections <N>   Max connections per slice (default: 1, 0=direct)
 *   --currency <code>       Currency (default: EUR)
 *   --limit <N>             Max results to display (default: 10)
 *   --sort <field>          price, duration (default: price)
 *   --json                  Output raw API response
 */

import { duffelPost, parseArgs, formatDuration, formatDateTime, output } from "./lib.mjs";

const args = parseArgs();

if (!args.from || !args.to || !args.date) {
  console.error("Usage: node search-flights.mjs --from <IATA> --to <IATA> --date <YYYY-MM-DD> [options]");
  process.exit(1);
}

// Build passengers array
const passengers = [];
const adultCount = parseInt(args.adults || "1", 10);
for (let i = 0; i < adultCount; i++) {
  passengers.push({ type: "adult" });
}

if (args.children) {
  const ages = args.children.split(",").map((a) => parseInt(a.trim(), 10));
  for (const age of ages) {
    passengers.push({ age });
  }
}

if (args.infants) {
  const ages = args.infants.split(",").map((a) => parseInt(a.trim(), 10));
  for (const age of ages) {
    passengers.push({ age });
  }
}

// Build slices
const slices = [
  {
    origin: args.from,
    destination: args.to,
    departure_date: args.date,
  },
];

// Add return slice
if (args.return) {
  slices.push({
    origin: args.to,
    destination: args.from,
    departure_date: args.return,
  });
}

const body = {
  data: {
    slices,
    passengers,
    cabin_class: args.cabins || "economy",
    max_connections: args["max-connections"] !== undefined ? parseInt(args["max-connections"], 10) : 1,
  },
};

try {
  const response = await duffelPost("/air/offer_requests?return_offers=true", body);

  if (args.json) {
    output(response);
    process.exit(0);
  }

  const offers = response.data?.offers || [];
  const limit = parseInt(args.limit || "10", 10);

  // Sort offers
  let sorted = [...offers];
  if (args.sort === "duration") {
    sorted.sort((a, b) => {
      const durA = a.slices.reduce((sum, s) => sum + (parseDurationSeconds(s.duration) || 0), 0);
      const durB = b.slices.reduce((sum, s) => sum + (parseDurationSeconds(s.duration) || 0), 0);
      return durA - durB;
    });
  } else {
    sorted.sort((a, b) => parseFloat(a.total_amount) - parseFloat(b.total_amount));
  }

  const limited = sorted.slice(0, limit);

  const results = limited.map((offer, idx) => {
    const outbound = offer.slices[0];
    const inbound = offer.slices[1]; // undefined for one-way

    const segments = outbound?.segments || [];
    const firstSeg = segments[0];
    const lastSeg = segments[segments.length - 1];

    const airlines = [...new Set(segments.map((s) => s.operating_carrier?.name || s.marketing_carrier?.name || ""))];

    const result = {
      index: idx + 1,
      price: `${offer.total_amount} ${offer.total_currency}`,
      price_raw: parseFloat(offer.total_amount),
      currency: offer.total_currency,
      airlines: airlines.join(", "),
      cabin_class: offer.slices[0]?.segments?.[0]?.passengers?.[0]?.cabin_class || args.cabins || "economy",
      departure: {
        airport: firstSeg?.origin?.iata_code,
        city: firstSeg?.origin?.city_name || firstSeg?.origin?.name,
        time: formatDateTime(firstSeg?.departing_at),
      },
      arrival: {
        airport: lastSeg?.destination?.iata_code,
        city: lastSeg?.destination?.city_name || lastSeg?.destination?.name,
        time: formatDateTime(lastSeg?.arriving_at),
      },
      duration: formatDuration(outbound?.duration),
      stops: segments.length - 1,
      stop_airports:
        segments.length > 1
          ? segments.slice(0, -1).map((s) => s.destination?.iata_code).join(" -> ")
          : "direct",
      offer_id: offer.id,
      expires_at: offer.expires_at,
      conditions: {
        refundable: offer.conditions?.refund_before_departure?.allowed || false,
        changeable: offer.conditions?.change_before_departure?.allowed || false,
      },
    };

    // Return leg
    if (inbound) {
      const retSegs = inbound.segments || [];
      const retFirst = retSegs[0];
      const retLast = retSegs[retSegs.length - 1];
      result.return_departure = {
        airport: retFirst?.origin?.iata_code,
        city: retFirst?.origin?.city_name || retFirst?.origin?.name,
        time: formatDateTime(retFirst?.departing_at),
      };
      result.return_arrival = {
        airport: retLast?.destination?.iata_code,
        city: retLast?.destination?.city_name || retLast?.destination?.name,
        time: formatDateTime(retLast?.arriving_at),
      };
      result.return_duration = formatDuration(inbound.duration);
      result.return_stops = retSegs.length - 1;
    }

    return result;
  });

  output({
    offer_request_id: response.data?.id,
    total_offers: offers.length,
    currency: results[0]?.currency || args.currency || "EUR",
    showing: results.length,
    results,
  });
} catch (err) {
  output({ error: err.message });
  process.exit(1);
}

function parseDurationSeconds(isoDuration) {
  if (!isoDuration) return 0;
  const match = String(isoDuration).match(/PT(?:(\d+)H)?(?:(\d+)M)?/);
  if (!match) return 0;
  return (parseInt(match[1] || "0", 10) * 60 + parseInt(match[2] || "0", 10)) * 60;
}
