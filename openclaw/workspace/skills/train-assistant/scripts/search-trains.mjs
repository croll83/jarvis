#!/usr/bin/env node
/**
 * Search Trenitalia trains with prices via BFF API.
 *
 * Usage:
 *   node search-trains.mjs --from "Milano" --to "Roma" --date 2026-03-15 [options]
 *
 * Options:
 *   --from <city>          Origin city/station name
 *   --to <city>            Destination city/station name
 *   --date <YYYY-MM-DD>    Departure date
 *   --time <HH:MM>         Departure time (default: 06:00)
 *   --time-end <HH:MM>     End of time window (default: 23:00)
 *   --return <YYYY-MM-DD>  Return date (optional, for round-trip)
 *   --return-time <HH:MM>  Return departure time (default: 06:00)
 *   --adults <N>           Number of adults (default: 1)
 *   --children <N>         Number of children (default: 0)
 *   --class <class>        Preferred class filter: economy, standard, business, executive (shows all by default)
 *   --limit <N>            Max results to display (default: 20)
 *   --sort <field>         price, departure, duration (default: departure)
 */

import {
  trenitaliaInit,
  trenitaliaGet,
  trenitaliaPost,
  parseArgs,
  formatDuration,
  formatDateTime,
  buildTrenitaliaBookingUrl,
  output,
} from "./lib.mjs";

const args = parseArgs();

if (!args.from || !args.to || !args.date) {
  console.error("Usage: node search-trains.mjs --from <city> --to <city> --date <YYYY-MM-DD> [options]");
  process.exit(1);
}

const adults = parseInt(args.adults || "1", 10);
const children = parseInt(args.children || "0", 10);
const depTime = args.time || "06:00";
const depTimeEnd = args["time-end"] || "23:00";
const limit = parseInt(args.limit || "20", 10);
const sortBy = args.sort || "departure";
const classFilter = args.class ? args.class.toLowerCase() : null;

// ── Resolve stations ────────────────────────────────────────────────────────

async function resolveTrenitaliaStation(query) {
  const results = await trenitaliaGet("locations/search", { name: query, limit: 5 });
  if (!results || results.length === 0) return null;
  const exact = results.find(
    (r) => !r.multistation && r.name.toLowerCase().includes(query.toLowerCase())
  );
  return exact || results[0];
}

// ── Main ────────────────────────────────────────────────────────────────────

try {
  await trenitaliaInit();

  const [originSt, destSt] = await Promise.all([
    resolveTrenitaliaStation(args.from),
    resolveTrenitaliaStation(args.to),
  ]);

  if (!originSt || !destSt) {
    output({ error: `Station not found (from: ${!!originSt}, to: ${!!destSt})` });
    process.exit(1);
  }

  const depDatetime = `${args.date}T${depTime}:00.000+01:00`;

  const body = {
    departureLocationId: originSt.id,
    arrivalLocationId: destSt.id,
    departureTime: depDatetime,
    returnDepartureTime: args.return ? `${args.return}T${args["return-time"] || "06:00"}:00.000+01:00` : null,
    adults,
    children,
    criteria: {
      frecceOnly: false,
      regionalOnly: false,
      noChanges: false,
      order: "DEPARTURE_DATE",
      limit: 40,
      offset: 0,
    },
    advancedSearchRequest: {
      bestFare: false,
    },
  };

  const data = await trenitaliaPost("ticket/solutions", body);
  const solutions = data.solutions || [];

  let results = solutions
    .filter((s) => s.solution?.status === "SALEABLE")
    .map((s) => {
      const sol = s.solution;
      const grids = s.grids || [];
      const trains = (sol.trains || []).map((t) => ({
        number: t.name || t.description,
        category: t.acronym || t.trainCategory,
        name: t.denomination || t.trainCategory,
      }));

      const fares = [];
      for (const grid of grids) {
        for (const svc of grid.services || []) {
          for (const offer of svc.offers || []) {
            if (offer.status !== "SALEABLE") continue;
            fares.push({
              class: svc.groupName || svc.name,
              offer_name: offer.name,
              price: offer.price?.amount,
              currency: offer.price?.currency || "€",
              refundable: offer.canRefund || false,
              changeable: offer.canChange || false,
              available: offer.availableAmount,
            });
          }
        }
      }

      const cheapest = fares.length > 0
        ? fares.reduce((a, b) => (a.price <= b.price ? a : b))
        : null;

      const depDate = new Date(sol.departureTime);
      const arrDate = new Date(sol.arrivalTime);
      const durationMin = Math.round((arrDate - depDate) / 60000);

      return {
        provider: "trenitalia",
        departure_time: sol.departureTime,
        arrival_time: sol.arrivalTime,
        departure_display: formatDateTime(sol.departureTime),
        arrival_display: formatDateTime(sol.arrivalTime),
        origin: sol.origin,
        destination: sol.destination,
        duration: formatDuration(durationMin),
        duration_min: durationMin,
        trains,
        train_display: trains.map((t) => `${t.category} ${t.number}`).join(" + "),
        changes: (sol.nodes || []).length - 1,
        price: cheapest?.price || null,
        currency: cheapest?.currency || "€",
        fares,
        solution_id: sol.id,
        booking_url: buildTrenitaliaBookingUrl(originSt.id, destSt.id, args.date, depTime),
      };
    });

  // Filter by time window
  const timeEndDate = new Date(`${args.date}T${depTimeEnd}:00+01:00`);
  results = results.filter((r) => new Date(r.departure_time) <= timeEndDate);

  // Filter by class — also update displayed price to match filtered class
  if (classFilter) {
    const classMap = {
      economy: ["smart", "2nd class", "2a classe"],
      standard: ["comfort", "standard", "1st class", "1a classe"],
      business: ["prima", "business"],
      executive: ["club", "executive"],
    };
    const allowed = classMap[classFilter] || [classFilter];
    const matchesFare = (f) =>
      allowed.some((a) => f.class.toLowerCase().includes(a) || f.offer_name.toLowerCase().includes(a));

    results = results
      .filter((r) => r.fares.some(matchesFare))
      .map((r) => {
        const matchingFares = r.fares.filter(matchesFare);
        const cheapest = matchingFares.reduce((a, b) => (a.price <= b.price ? a : b));
        return { ...r, price: cheapest.price, fares: matchingFares };
      });
  }

  // Sort
  if (sortBy === "price") {
    results.sort((a, b) => (a.price || 9999) - (b.price || 9999));
  } else if (sortBy === "duration") {
    results.sort((a, b) => a.duration_min - b.duration_min);
  } else {
    results.sort((a, b) => new Date(a.departure_time) - new Date(b.departure_time));
  }

  const limited = results.slice(0, limit);
  const indexed = limited.map((r, i) => ({ index: i + 1, ...r }));

  output({
    query: {
      from: args.from,
      to: args.to,
      date: args.date,
      time: depTime,
      time_end: depTimeEnd,
      adults,
      children,
      return_date: args.return || null,
    },
    total_results: results.length,
    showing: indexed.length,
    results: indexed,
  });
} catch (err) {
  output({ error: err.message });
  process.exit(1);
}
