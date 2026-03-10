#!/usr/bin/env node
/**
 * Track a train in real-time via ViaggiaTreno API.
 *
 * Usage:
 *   node track-train.mjs --train 9584                           # Search by train number (today)
 *   node track-train.mjs --train 9584 --date 2026-03-15         # Specific date
 *   node track-train.mjs --from "Milano" --to "Roma"            # Search departures/arrivals
 *   node track-train.mjs --from "Milano" --to "Roma" --time 10:00
 *   node track-train.mjs --station "Milano Centrale"            # Departures board
 *
 * Options:
 *   --train <number>       Train number to track
 *   --date <YYYY-MM-DD>    Date (default: today)
 *   --from <station>       Origin station name (for route search)
 *   --to <station>         Destination station name (for route search)
 *   --time <HH:MM>         Time filter for route search (default: now)
 *   --station <name>       Show departures board for a station
 *   --arrivals             Show arrivals instead of departures (with --station)
 */

import { vtGet, parseArgs, formatTimestamp, output } from "./lib.mjs";

const args = parseArgs();

// ── Helper: resolve station code from name via autocomplete ─────────────────

async function resolveStation(query) {
  const text = await vtGet(`autocompletaStazione/${encodeURIComponent(query)}`);
  if (!text || typeof text !== "string") return null;
  const lines = text.trim().split("\n").filter(Boolean);
  if (lines.length === 0) return null;
  // Find best match
  const q = query.toLowerCase();
  for (const line of lines) {
    const [name, code] = line.split("|");
    if (name.toLowerCase().includes(q)) return { name: name.trim(), code: code.trim() };
  }
  // Fallback to first
  const [name, code] = lines[0].split("|");
  return { name: name.trim(), code: code.trim() };
}

// ── Helper: get region code for station ─────────────────────────────────────

async function getRegion(stationCode) {
  const text = await vtGet(`regione/${stationCode}`);
  return parseInt(text, 10) || 0;
}

// ── Track a specific train by number ────────────────────────────────────────

async function trackTrain(trainNumber, date) {
  // Step 1: Find the train to get origin station and departure timestamp
  const autoText = await vtGet(`cercaNumeroTrenoTrenoAutocomplete/${trainNumber}`);
  if (!autoText || typeof autoText !== "string" || autoText.trim() === "") {
    throw new Error(`Train ${trainNumber} not found`);
  }

  const lines = autoText.trim().split("\n").filter(Boolean);

  // Parse lines: "770 - TRIESTE CENTRALE - 21/07/25|770-S03317-1753048800000"
  let match = null;
  const targetDate = date || new Date().toISOString().slice(0, 10);

  for (const line of lines) {
    const parts = line.split("|");
    if (parts.length < 2) continue;
    const [num, stCode, tsStr] = parts[1].split("-");
    const ts = parseInt(tsStr, 10);
    const d = new Date(ts);
    const dStr = d.toISOString().slice(0, 10);
    if (dStr === targetDate || !date) {
      match = { trainNumber: num, stationCode: stCode, timestamp: ts };
      break;
    }
  }

  if (!match) {
    // Fallback: use first result
    const parts = lines[0].split("|");
    const [num, stCode, tsStr] = parts[1].split("-");
    match = { trainNumber: num, stationCode: stCode, timestamp: parseInt(tsStr, 10) };
  }

  // Step 2: Get real-time tracking data
  const tracking = await vtGet(
    `andamentoTreno/${match.stationCode}/${match.trainNumber}/${match.timestamp}`
  );

  if (!tracking) {
    return {
      error: `No tracking data available for train ${trainNumber} (may be cancelled or not yet in system)`,
    };
  }

  // Parse stops
  const stops = (tracking.fermate || []).map((f) => ({
    station: f.stazione,
    station_code: f.id,
    type: { P: "origin", F: "intermediate", A: "destination" }[f.tipoFermata] || "unknown",
    scheduled_arrival: formatTimestamp(f.arrivo_teorico),
    actual_arrival: formatTimestamp(f.arrivoReale),
    scheduled_departure: formatTimestamp(f.partenza_teorica),
    actual_departure: formatTimestamp(f.partenzaReale),
    delay_arrival_min: f.ritardoArrivo || 0,
    delay_departure_min: f.ritardoPartenza || 0,
    platform_scheduled: f.binarioProgrammatoArrivoDescrizione || f.binarioProgrammatoPartenzaDescrizione,
    platform_actual: f.binarioEffettivoArrivoDescrizione || f.binarioEffettivoPartenzaDescrizione,
    cancelled: f.actualFermataType === 3,
    passed: f.actualFermataType === 1 || !!f.arrivoReale || !!f.partenzaReale,
  }));

  // Determine current status
  let status = "unknown";
  if (tracking.tipoTreno === "ST") status = "cancelled";
  else if (tracking.nonPartito) status = "not_departed";
  else if (tracking.arrivato) status = "arrived";
  else if (tracking.circolante) status = "running";

  const cancelType = {
    PG: null,
    ST: "fully_cancelled",
    SF: "final_stops_cancelled",
    SI: "initial_stops_cancelled",
    DV: "diverted",
    PP: "partially_cancelled",
  }[tracking.tipoTreno];

  return {
    train_number: tracking.numeroTreno,
    train_type: (tracking.compNumeroTreno || "").trim(),
    category: tracking.categoriaDescrizione || tracking.categoria || "",
    status,
    cancellation: cancelType,
    cancellation_detail: tracking.subTitle || null,
    origin: tracking.origine,
    destination: tracking.destinazione,
    scheduled_departure: formatTimestamp(tracking.orarioPartenza),
    scheduled_arrival: formatTimestamp(tracking.orarioArrivo),
    duration: tracking.compDurata,
    delay_min: tracking.ritardo || 0,
    delay_text: (tracking.compRitardo || [])[0] || "",
    last_detection: {
      station: tracking.stazioneUltimoRilevamento !== "--" ? tracking.stazioneUltimoRilevamento : null,
      time: formatTimestamp(tracking.oraUltimoRilevamento),
    },
    delay_reason: tracking.motivoRitardoPrevalente || null,
    stops,
  };
}

// ── Station departures/arrivals board ───────────────────────────────────────

async function stationBoard(stationName, showArrivals = false) {
  const station = await resolveStation(stationName);
  if (!station) throw new Error(`Station "${stationName}" not found`);

  // ViaggiaTreno wants a specific date format for partenze/arrivi
  const now = new Date();
  const dateStr = now
    .toLocaleString("en-US", {
      weekday: "short",
      month: "short",
      day: "2-digit",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
      timeZone: "Europe/Rome",
    })
    .replace(/,/g, "");
  // Format: "Mon Mar 09 2026 14:30:00 GMT+0100"
  const vtDate = `${dateStr} GMT+0100`;

  const endpoint = showArrivals ? "arrivi" : "partenze";
  const data = await vtGet(`${endpoint}/${station.code}/${encodeURIComponent(vtDate)}`);

  if (!data || !Array.isArray(data)) {
    return { station: station.name, station_code: station.code, trains: [] };
  }

  const trains = data.map((t) => ({
    train_number: t.numeroTreno,
    train_type: (t.compNumeroTreno || "").trim(),
    category: (t.categoriaDescrizione || "").trim(),
    origin: showArrivals ? t.origine : station.name,
    destination: showArrivals ? station.name : t.destinazione,
    time: t.compOrarioPartenza || t.compOrarioArrivo,
    delay_min: t.ritardo || 0,
    delay_text: (t.compRitardo || [])[0] || "",
    platform: t.binarioEffettivoPartenzaDescrizione || t.binarioProgrammatoPartenzaDescrizione || "",
    cancelled: t.provvedimento === 1,
    running: t.circolante,
    in_station: t.inStazione,
    train_product: { "100": "Frecciarossa", "101": "Frecciargento", "102": "Frecciabianca" }[
      t.tipoProdotto
    ] || "",
  }));

  return {
    station: station.name,
    station_code: station.code,
    type: showArrivals ? "arrivals" : "departures",
    count: trains.length,
    trains,
  };
}

// ── Route search (departures from A, filter by destination B) ───────────────

async function searchRoute(fromName, toName, time) {
  const fromStation = await resolveStation(fromName);
  if (!fromStation) throw new Error(`Station "${fromName}" not found`);

  const board = await stationBoard(fromName);
  const toQuery = toName.toLowerCase();

  // Filter trains going to destination
  const matching = board.trains.filter(
    (t) => t.destination && t.destination.toLowerCase().includes(toQuery)
  );

  if (matching.length === 0) {
    return {
      message: `No direct trains found from ${fromStation.name} to ${toName}. Try search-trains.mjs for connections.`,
      departures_checked: board.trains.length,
    };
  }

  // Optionally filter by time
  if (time) {
    const [hh, mm] = time.split(":").map(Number);
    const timeMin = hh * 60 + mm;
    const filtered = matching.filter((t) => {
      if (!t.time) return true;
      const [th, tm] = t.time.split(":").map(Number);
      return th * 60 + tm >= timeMin;
    });
    return { from: fromStation.name, to: toName, trains: filtered };
  }

  return { from: fromStation.name, to: toName, trains: matching };
}

// ── Main ────────────────────────────────────────────────────────────────────

try {
  if (args.train) {
    const result = await trackTrain(args.train, args.date);
    output(result);
  } else if (args.station) {
    const result = await stationBoard(args.station, !!args.arrivals);
    output(result);
  } else if (args.from && args.to) {
    const result = await searchRoute(args.from, args.to, args.time);
    output(result);
  } else {
    console.error(
      "Usage:\n" +
        "  node track-train.mjs --train <number> [--date YYYY-MM-DD]\n" +
        "  node track-train.mjs --station <name> [--arrivals]\n" +
        "  node track-train.mjs --from <station> --to <station> [--time HH:MM]"
    );
    process.exit(1);
  }
} catch (err) {
  output({ error: err.message });
  process.exit(1);
}
