/**
 * Shared utilities for flight-assistant scripts (Duffel API).
 */

const BASE_URL = "https://api.duffel.com";
const API_TOKEN = process.env.DUFFEL_API_TOKEN;
const API_VERSION = "v2";

if (!API_TOKEN) {
  console.error("ERROR: DUFFEL_API_TOKEN environment variable is required");
  process.exit(1);
}

export { BASE_URL, API_TOKEN };

/**
 * Standard Duffel headers.
 */
function headers() {
  return {
    Authorization: `Bearer ${API_TOKEN}`,
    "Duffel-Version": API_VERSION,
    "Content-Type": "application/json",
    Accept: "application/json",
  };
}

/**
 * Make an authenticated GET request to Duffel API.
 */
export async function duffelGet(path, params = {}) {
  const url = new URL(path, BASE_URL);
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") {
      url.searchParams.set(k, String(v));
    }
  }

  const res = await fetch(url.toString(), { headers: headers() });

  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`Duffel API ${res.status}: ${res.statusText}\n${body}`);
  }

  return res.json();
}

/**
 * Make an authenticated POST request to Duffel API.
 */
export async function duffelPost(path, body) {
  const url = new URL(path, BASE_URL);

  const res = await fetch(url.toString(), {
    method: "POST",
    headers: headers(),
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Duffel API ${res.status}: ${res.statusText}\n${text}`);
  }

  return res.json();
}

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
 * Format ISO duration (PT2H30M) or minutes to a human-readable string.
 */
export function formatDuration(input) {
  if (typeof input === "number") {
    const h = Math.floor(input / 60);
    const m = input % 60;
    return `${h}h${m > 0 ? ` ${m}m` : ""}`;
  }
  // Parse ISO 8601 duration
  const match = String(input).match(/PT(?:(\d+)H)?(?:(\d+)M)?/);
  if (match) {
    const h = parseInt(match[1] || "0", 10);
    const m = parseInt(match[2] || "0", 10);
    return `${h}h${m > 0 ? ` ${m}m` : ""}`;
  }
  return String(input);
}

/**
 * Format an ISO datetime string to "DD/MM HH:MM" for display.
 */
export function formatDateTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const dd = String(d.getDate()).padStart(2, "0");
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const min = String(d.getMinutes()).padStart(2, "0");
  return `${dd}/${mm} ${hh}:${min}`;
}

/**
 * Output JSON to stdout.
 */
export function output(data) {
  console.log(JSON.stringify(data, null, 2));
}
