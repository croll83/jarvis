#!/usr/bin/env node
/**
 * Search Italo trains via api-biglietti.italotreno.com API.
 *
 * Reads tokens from .env, auto-refreshes if expired, saves new tokens back.
 * POST /api/v1/booking (async 202) → poll GET /api/v1/booking/status/{opId}
 *
 * Usage:
 *   node search-italo.mjs --from MC_ --to RMT --date 2026-03-15 [options]
 *
 * Options:
 *   --from <code>         Origin station code (e.g. MC_, RMT, NAC)
 *   --to <code>           Destination station code
 *   --date <YYYY-MM-DD>   Departure date
 *   --time <HH:MM>        Start of time window (default: 06:00)
 *   --time-end <HH:MM>    End of time window (default: 23:00)
 *   --adults <N>           Number of adults (default: 1)
 *   --children <N>         Number of children (default: 0)
 *   --limit <N>            Max results (default: 20)
 *   --sort <field>         price, departure, duration (default: departure)
 *   --class <class>        economy/smart, comfort, prima/business, executive
 *   --refresh-only         Only refresh token and exit
 */

import { readFileSync, writeFileSync, existsSync, unlinkSync, mkdirSync } from "node:fs";
import { parseArgs, formatDuration, formatDateTime, output } from "./lib.mjs";

// ── Lock (semaphore) — prevent concurrent API calls ─────────────────────────
const LOCK_DIR = "/tmp";
const LOCK_FILE = `${LOCK_DIR}/italo-search.lock`;
const LOCK_TIMEOUT_MS = 60000; // stale lock after 60s

function acquireLock() {
  if (existsSync(LOCK_FILE)) {
    try {
      const lock = JSON.parse(readFileSync(LOCK_FILE, "utf-8"));
      const age = Date.now() - lock.ts;
      if (age < LOCK_TIMEOUT_MS) {
        // Lock is held and fresh — wait for it
        return false;
      }
      // Stale lock — remove it
    } catch { /* corrupt lock file */ }
  }
  writeFileSync(LOCK_FILE, JSON.stringify({ pid: process.pid, ts: Date.now() }), "utf-8");
  return true;
}

function releaseLock() {
  try { unlinkSync(LOCK_FILE); } catch { /* already removed */ }
}

async function waitForLock(timeoutMs = 90000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (acquireLock()) return true;
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error("Italo search lock timeout — another search is stuck. Remove /tmp/italo-search.lock if needed.");
}

const ITALO_API = "https://api-biglietti.italotreno.com";
const ENV_PATH = new URL("../.env", import.meta.url).pathname
  .replace("/opt/jarvis/openclaw/skills/", "/home/jarvis/.openclaw/workspace/skills/");

const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

// ── Station codes ────────────────────────────────────────────────────────────
const STATION_NAMES = {
  MC_: "Milano Centrale", RG_: "Milano Rogoredo",
  RMT: "Roma Termini", RTB: "Roma Tiburtina",
  NAC: "Napoli Centrale", NAF: "Napoli Afragola",
  BC_: "Bologna Centrale", SMN: "Firenze S.M.N.",
  TOP: "Torino P. Nuova", OUE: "Torino P. Susa",
  VEM: "Venezia Mestre", VSL: "Venezia S. Lucia",
  PD_: "Padova", VPN: "Verona P. Nuova",
  BAC: "Bari Centrale", SAL: "Salerno",
  AAV: "Reggio Emilia AV", TSC: "Trieste",
  UDN: "Udine", BSC: "Brescia", BGM: "Bergamo",
};

// productClass → display name
const CLASS_NAMES = {
  S: "Smart", P: "Prima", C: "Club Executive", S1: "Smart XL", S2: "Smart XL Salottino",
};

// class filter → productClass codes
const CLASS_FILTER_MAP = {
  economy: ["S"], smart: ["S"],
  comfort: ["S1", "S2"],
  business: ["P"], prima: ["P"],
  executive: ["C"],
};

// ── Token management ─────────────────────────────────────────────────────────

function readEnv() {
  try {
    const text = readFileSync(ENV_PATH, "utf-8");
    const env = {};
    for (const line of text.split("\n")) {
      const m = line.match(/^(\w+)=(.+)$/);
      if (m) env[m[1]] = m[2];
    }
    return env;
  } catch {
    return {};
  }
}

function writeEnv(sessionToken, refreshToken) {
  writeFileSync(
    ENV_PATH,
    `ITALO_SESSION_TOKEN=${sessionToken}\nITALO_REFRESH_TOKEN=${refreshToken}\n`,
    "utf-8"
  );
}

function decodeJwtPayload(jwt) {
  try {
    return JSON.parse(Buffer.from(jwt.split(".")[1], "base64url").toString());
  } catch {
    try { return JSON.parse(Buffer.from(jwt, "base64").toString()); } catch { return {}; }
  }
}

function isTokenExpired(token, marginSec = 60) {
  const payload = decodeJwtPayload(token);
  if (payload.exp == null) return true;
  const expMs = payload.exp > 1e12 ? payload.exp : payload.exp * 1000;
  return Date.now() > expMs - marginSec * 1000;
}

async function refreshTokens(refreshToken) {
  const res = await fetch(`${ITALO_API}/public/api/v1/users/refresh`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "User-Agent": UA },
    body: JSON.stringify({ refreshToken }),
  });
  if (res.ok) {
    const data = await res.json();
    if (data.token && data.refreshToken) {
      return { sessionToken: data.token, refreshToken: data.refreshToken };
    }
    throw new Error(`Unexpected refresh response: ${JSON.stringify(data).slice(0, 200)}`);
  }
  const body = await res.text().catch(() => "");
  throw new Error(`Italo refresh ${res.status}: ${body.slice(0, 300)}`);
}

async function getValidToken() {
  const env = readEnv();
  let { ITALO_SESSION_TOKEN: sessionToken, ITALO_REFRESH_TOKEN: refreshToken } = env;
  if (sessionToken == null || refreshToken == null) {
    throw new Error("No Italo tokens in .env. Run browser token fetch first.");
  }
  if (isTokenExpired(sessionToken)) {
    const fresh = await refreshTokens(refreshToken);
    sessionToken = fresh.sessionToken;
    writeEnv(fresh.sessionToken, fresh.refreshToken);
  }
  return sessionToken;
}

// ── API helpers ──────────────────────────────────────────────────────────────

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

async function italoFetch(method, path, token, body) {
  const opts = {
    method,
    headers: {
      Accept: "application/json",
      Authorization: `Bearer ${token}`,
      "User-Agent": UA,
    },
  };
  if (body) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(`${ITALO_API}${path}`, opts);
  return { status: res.status, data: await res.json().catch(() => null) };
}

// ── Search flow ──────────────────────────────────────────────────────────────

async function searchItalo(token, params) {
  // Clean up any previous booking state
  await italoFetch("DELETE", "/api/v1/working-sessions", token);
  await italoFetch("POST", "/api/v1/working-sessions", token, {});

  const body = {
    departureStation: params.from,
    arrivalStation: params.to,
    departureDate: `${params.date}T${params.time}:00+01:00`,
    adultPassengers: params.adults,
    childPassengers: params.children,
    request: {},
  };

  const postRes = await italoFetch("POST", "/api/v1/booking", token, body);

  if (postRes.status === 200 && postRes.data) return postRes.data;

  if (postRes.status !== 202 || postRes.data == null) {
    throw new Error(`Italo POST /booking ${postRes.status}: ${JSON.stringify(postRes.data).slice(0, 500)}`);
  }

  const { operationId, pollAfter, operationTimeout } = postRes.data;
  const pollMs = (pollAfter || 1500) + 500;
  const deadline = Date.now() + Math.min(operationTimeout || 30000, 30000);

  await sleep(pollMs);

  while (Date.now() < deadline) {
    const pollRes = await italoFetch("GET", `/api/v1/booking/status/${operationId}`, token);
    if (pollRes.status === 200 && pollRes.data) return pollRes.data;
    if (pollRes.status === 500 && pollRes.data?.type?.includes("toomanyconnections")) {
      throw new Error("Session exhausted (too many connections). Need fresh browser tokens — run the token fetch prompt.");
    }
    await sleep(2000);
  }

  throw new Error("Italo poll timeout");
}

// ── Parse response ───────────────────────────────────────────────────────────

function parseResults(data, params) {
  const trips = data.trips || [];
  if (trips.length === 0) return [];

  const trip = trips[0]; // forward direction
  const solutions = trip.travelSolutions || [];

  return solutions.map((ts) => {
    const journeys = ts.journeys || [];
    const firstJourney = journeys[0] || {};
    const segments = firstJourney.segments || [];
    const firstSeg = segments[0] || {};
    const lastSeg = segments[segments.length - 1] || firstSeg;

    const dep = firstSeg.std || "";
    const arr = lastSeg.sta || "";
    const trainNumber = firstSeg.trainNumber || "";
    const equipmentType = firstSeg.equipmentType || "";

    // Collect fares from all segments (usually just one for direct trains)
    const fareMap = {}; // productClass → cheapest price
    for (const seg of segments) {
      for (const fare of (seg.fares || [])) {
        const pc = fare.productClass;
        const paxFare = (fare.paxFares || [])[0];
        const price = paxFare?.singlePaxDiscountPrice ?? paxFare?.singlePaxFarePrice;
        if (price == null) continue;
        const className = CLASS_NAMES[pc] || pc;
        const key = `${pc}|${fare.offerType}`;
        if (fareMap[key] == null || price < fareMap[key].price) {
          fareMap[key] = {
            class: className,
            product_class: pc,
            offer_type: fare.offerType,
            price,
            currency: "€",
            available: fare.availableCount,
          };
        }
      }
    }

    const fares = Object.values(fareMap).sort((a, b) => a.price - b.price);
    const cheapest = fares[0] || null;

    const depDate = dep ? new Date(dep) : null;
    const arrDate = arr ? new Date(arr) : null;
    const durationMin = depDate && arrDate ? Math.round((arrDate - depDate) / 60000) : 0;

    return {
      provider: "italo",
      train_number: trainNumber,
      equipment: equipmentType,
      departure_time: dep,
      arrival_time: arr,
      departure_display: formatDateTime(dep),
      arrival_display: formatDateTime(arr),
      origin: STATION_NAMES[params.from] || params.from,
      destination: STATION_NAMES[params.to] || params.to,
      duration: formatDuration(durationMin),
      duration_min: durationMin,
      changes: ts.numberOfChanges || 0,
      price: cheapest?.price ?? null,
      currency: "€",
      fares,
      booking_url: `https://biglietti.italotreno.com/en/booking/lista-treni-andata?osc=${params.from}&dsc=${params.to}&od=${params.date}&adt=${params.adults}&chd=${params.children}`,
    };
  });
}

// ── Main ─────────────────────────────────────────────────────────────────────

const args = parseArgs();

if (args["refresh-only"]) {
  try {
    const env = readEnv();
    const fresh = await refreshTokens(env.ITALO_REFRESH_TOKEN);
    writeEnv(fresh.sessionToken, fresh.refreshToken);
    output({ ok: true, message: "Tokens refreshed successfully" });
  } catch (err) {
    output({ error: err.message });
    process.exit(1);
  }
  process.exit(0);
}

if (args.from == null || args.to == null || args.date == null) {
  console.error("Usage: node search-italo.mjs --from <station_code> --to <station_code> --date <YYYY-MM-DD>");
  console.error("\nStation codes: " + Object.keys(STATION_NAMES).join(", "));
  process.exit(1);
}

const params = {
  from: args.from,
  to: args.to,
  date: args.date,
  time: args.time || "06:00",
  timeEnd: args["time-end"] || "23:00",
  adults: parseInt(args.adults || "1", 10),
  children: parseInt(args.children || "0", 10),
  limit: parseInt(args.limit || "20", 10),
  sort: args.sort || "departure",
  classFilter: args.class ? args.class.toLowerCase() : null,
};

try {
  await waitForLock();
} catch (err) {
  output({ error: err.message });
  process.exit(1);
}

try {
  const token = await getValidToken();
  const data = await searchItalo(token, params);
  let results = parseResults(data, params);

  // Filter by time window
  const timeEnd = new Date(`${params.date}T${params.timeEnd}:00`);
  results = results.filter((r) => r.departure_time == null || new Date(r.departure_time) <= timeEnd);

  // Filter by class
  if (params.classFilter) {
    const allowed = CLASS_FILTER_MAP[params.classFilter] || [params.classFilter.toUpperCase()];
    results = results
      .filter((r) => r.fares.some((f) => allowed.includes(f.product_class)))
      .map((r) => {
        const matching = r.fares.filter((f) => allowed.includes(f.product_class));
        const cheapest = matching.reduce((a, b) => (a.price <= b.price ? a : b));
        return { ...r, price: cheapest.price, fares: matching };
      });
  }

  // Sort
  if (params.sort === "price") {
    results.sort((a, b) => (a.price ?? 9999) - (b.price ?? 9999));
  } else if (params.sort === "duration") {
    results.sort((a, b) => a.duration_min - b.duration_min);
  } else {
    results.sort((a, b) => new Date(a.departure_time || 0) - new Date(b.departure_time || 0));
  }

  const limited = results.slice(0, params.limit);
  const indexed = limited.map((r, i) => ({ index: i + 1, ...r }));

  output({
    query: {
      from: `${params.from} (${STATION_NAMES[params.from] || "?"})`,
      to: `${params.to} (${STATION_NAMES[params.to] || "?"})`,
      date: params.date,
      time: params.time,
      time_end: params.timeEnd,
      adults: params.adults,
      children: params.children,
    },
    total_results: results.length,
    showing: indexed.length,
    results: indexed,
  });
} catch (err) {
  releaseLock();
  output({ error: err.message });
  process.exit(1);
}
releaseLock();
