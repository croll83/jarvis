/**
 * Shared utilities for train-assistant scripts.
 * Supports Trenitalia BFF API and ViaggiaTreno tracking API.
 * Italo search is handled via browser scraping (see SKILL.md).
 */

// ── Trenitalia BFF API ──────────────────────────────────────────────────────
const TRENITALIA_BFF = "https://www.lefrecce.it/Channels.Website.BFF.WEB/website";
const TRENITALIA_WEB = "https://www.lefrecce.it/Channels.Website.WEB/";

// ── ViaggiaTreno API ────────────────────────────────────────────────────────
const VT_BASE = "http://www.viaggiatreno.it/infomobilita/resteasy/viaggiatreno";

export { TRENITALIA_BFF, TRENITALIA_WEB, VT_BASE };

// ── Browser-like headers ─────────────────────────────────────────────────
const BROWSER_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

// ── Italian timezone ─────────────────────────────────────────────────────
const TZ = "Europe/Rome";

// ── Cookie jar (simple in-memory) ───────────────────────────────────────────
let _trenitaliaCookies = "";

/**
 * Acquire Akamai CDN cookies from the Trenitalia main page.
 * Must be called once before using trenitaliaPost.
 */
export async function trenitaliaInit() {
  const res = await fetch(TRENITALIA_WEB, {
    method: "GET",
    headers: { "User-Agent": BROWSER_UA },
    redirect: "manual",
  });
  const setCookies = res.headers.getSetCookie?.() || [];
  _trenitaliaCookies = setCookies
    .map((c) => c.split(";")[0])
    .join("; ");
  await res.text().catch(() => {});
}

/**
 * GET request to Trenitalia BFF API.
 */
export async function trenitaliaGet(path, params = {}) {
  const url = new URL(path, TRENITALIA_BFF + "/");
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
  }
  const res = await fetch(url.toString(), {
    headers: {
      "User-Agent": BROWSER_UA,
      Accept: "application/json, text/plain, */*",
      Cookie: _trenitaliaCookies,
    },
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`Trenitalia ${res.status}: ${body.slice(0, 300)}`);
  }
  return res.json();
}

/**
 * POST request to Trenitalia BFF API.
 */
export async function trenitaliaPost(path, body) {
  const url = `${TRENITALIA_BFF}/${path}`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "User-Agent": BROWSER_UA,
      "Content-Type": "application/json",
      Accept: "application/json, text/plain, */*",
      Origin: "https://www.lefrecce.it",
      Referer: TRENITALIA_WEB,
      Cookie: _trenitaliaCookies,
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Trenitalia ${res.status}: ${text.slice(0, 300)}`);
  }
  return res.json();
}

// ── ViaggiaTreno helpers ────────────────────────────────────────────────────

/**
 * GET request to ViaggiaTreno API.
 */
export async function vtGet(path) {
  const url = `${VT_BASE}/${path}`;
  const res = await fetch(url, {
    headers: { "User-Agent": BROWSER_UA },
  });
  if (res.status === 204) return null;
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`ViaggiaTreno ${res.status}: ${text.slice(0, 300)}`);
  }
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("json")) return res.json();
  return res.text();
}

// ── Shared utilities ────────────────────────────────────────────────────────

/**
 * Parse CLI arguments into a key-value object.
 * Supports: --key value, --flag (boolean true)
 */
export function parseArgs(argv = process.argv.slice(2)) {
  const args = {};
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg.startsWith("--")) {
      const key = arg.slice(2);
      const next = argv[i + 1];
      if (next && !next.startsWith("--")) {
        args[key] = next;
        i++;
      } else {
        args[key] = true;
      }
    }
  }
  return args;
}

/**
 * Format a duration in minutes to "Xh Ym".
 */
export function formatDuration(input) {
  if (typeof input === "number") {
    const h = Math.floor(input / 60);
    const m = input % 60;
    return `${h}h${m > 0 ? ` ${m}m` : ""}`;
  }
  if (typeof input === "string") {
    const match = input.match(/(\d+)h\s*(\d+)/);
    if (match) return `${match[1]}h ${match[2]}m`;
    return input;
  }
  return String(input);
}

/**
 * Format ISO datetime to "DD/MM HH:MM" in Italian timezone.
 */
export function formatDateTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const parts = new Intl.DateTimeFormat("it-IT", {
    timeZone: TZ,
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(d);
  const get = (type) => parts.find((p) => p.type === type)?.value || "";
  return `${get("day")}/${get("month")} ${get("hour")}:${get("minute")}`;
}

/**
 * Format a unix-ms timestamp to "DD/MM HH:MM" in Italian timezone.
 */
export function formatTimestamp(ms) {
  if (!ms) return "";
  return formatDateTime(new Date(ms).toISOString());
}

/**
 * Output JSON to stdout.
 */
export function output(data) {
  console.log(JSON.stringify(data, null, 2));
}

/**
 * Build a Trenitalia booking URL with pre-filled search parameters.
 */
export function buildTrenitaliaBookingUrl(originId, destId, date, time) {
  const t = (time || "06:00").replace(":", "");
  return `https://www.lefrecce.it/Channels.Website.WEB/#/train-search/${originId}/${destId}/${date}/${t}`;
}
