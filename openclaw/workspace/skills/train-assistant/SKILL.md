---
name: train-assistant
version: 3.0.0
description: Search Italian trains (Trenitalia + Italo via API), track trains in real-time via ViaggiaTreno
requires:
  binary:
    - node
invocable: true
---

# Train Assistant Skill

Search Italian train tickets across Trenitalia and Italo via API, with real-time tracking via ViaggiaTreno.

## Architecture

- **Trenitalia**: CLI script via `exec` — fast, API-based (lefrecce.it BFF), no auth needed
- **Italo**: CLI script via `exec` — API-based (api-biglietti.italotreno.com), requires JWT tokens from browser session
- **ViaggiaTreno**: CLI script via `exec` — real-time tracking, no auth needed

### Scripts Location

```
cd ~/.openclaw/workspace/skills/train-assistant/scripts && node <tool>.mjs <args>
```

### Token Management (Italo)

Italo requires a JWT session token obtained from the browser. Tokens are stored in:

```
~/.openclaw/workspace/skills/train-assistant/.env
```

- **Session token**: expires every ~60 minutes, auto-refreshed by the script
- **Refresh token**: expires after ~24 hours, needs fresh browser extraction
- A **cronjob refreshes tokens every 40 minutes** to keep the session alive
- If both tokens expire, re-extract from browser (see **Session Recovery** below)

### Session Recovery (Italo SID Refresh)

**When to use this:** API returns `"Session exhausted (too many connections)"` or `"Need fresh browser tokens"`.

This error occurs when:
- Too many API calls on the same session ID (SID) burn out the server-side working session
- The cron refresh **does NOT help** — it refreshes the JWT tokens but keeps the same SID
- Only a brand-new SID from the browser will fix it

**Steps to get a fresh SID:**

1. **Clear all Italo cookies from the browser** (so the browser can't reuse the old cached session)
   ```javascript
   // In browser DevTools console:
   document.cookie.split(';').forEach(c => {
     const name = c.trim().split('=')[0];
     ['/', '/it', '/en'].forEach(path => {
       ['.italotreno.com', 'biglietti.italotreno.com'].forEach(domain => {
         document.cookie = name + '=;expires=Thu, 01 Jan 1970 00:00:00 GMT;path=' + path + ';domain=' + domain;
       });
     });
   });
   ```

2. **Navigate to the Italo booking page** (creates new session server-side)
   ```
   https://biglietti.italotreno.com/en/booking/ricerca-treni
   ```

3. **Wait 5 seconds** for the page to fully load and SID to be issued

4. **Extract the new tokens** from cookies:
   ```javascript
   // In browser DevTools console:
   const cookies = document.cookie.split(';').reduce((acc, c) => {
     const [k, ...v] = c.trim().split('=');
     acc[k] = v.join('=');
     return acc;
   }, {});
   console.log(JSON.stringify({
     session: cookies.BIGSessionToken,
     refresh: cookies.BIGRefreshToken
   }));
   ```

5. **Copy the output**, decode the session token to verify the SID is **new**:
   ```javascript
   // Decode the SID from the session token:
   JSON.parse(atob(sessionToken.split('.')[1])).sid
   ```
   Should be a **different UUID** than the old one.

6. **Update `.env` file** with the new tokens:
   ```
   ITALO_SESSION_TOKEN=<new_session_token>
   ITALO_REFRESH_TOKEN=<new_refresh_token>
   ```

7. **Run the search again** — should work with fresh session

**⚡ AUTOMATION (usa questo, non chiedere a Marco):**
Quando il token Italo è scaduto (errore 401, 500 "invalid-token", o 502 Bad Gateway sul refresh), usa il **browser tool di OpenClaw** per estrarre automaticamente i nuovi token:

```
1. browser(action="open", profile="openclaw", url="https://biglietti.italotreno.com/en/booking/ricerca-treni")
2. browser(action="act", kind="evaluate") → cancella i cookie vecchi con lo snippet JS sopra
3. browser(action="navigate") → ricarica la pagina
4. sleep 6 secondi
5. browser(action="act", kind="evaluate") → estrai BIGSessionToken e BIGRefreshToken dai cookie
6. Scrivi i nuovi token in ~/.openclaw/workspace/skills/train-assistant/.env
7. Riprova la ricerca
```

Fallo in autonomia senza chiedere conferma a Marco. Il processo completo richiede ~15 secondi.

## Workflow

### 1. Search Trenitalia (API — fast, no auth)

```bash
# One-way search
node search-trains.mjs --from "Milano" --to "Roma" --date 2026-03-15

# With time window
node search-trains.mjs --from "Milano" --to "Roma" --date 2026-03-15 --time 08:00 --time-end 14:00

# Round-trip
node search-trains.mjs --from "Milano" --to "Roma" --date 2026-03-15 --return 2026-03-17

# Multiple passengers
node search-trains.mjs --from "Firenze" --to "Napoli" --date 2026-04-01 --adults 2 --children 1

# Filter by class
node search-trains.mjs --from "Milano" --to "Roma" --date 2026-03-15 --class business

# Sort by price
node search-trains.mjs --from "Milano" --to "Roma" --date 2026-03-15 --sort price
```

**Options:**
- `--from <city>` — Origin city/station name (required)
- `--to <city>` — Destination city/station name (required)
- `--date <YYYY-MM-DD>` — Departure date (required)
- `--time <HH:MM>` — Start of time window (default: 06:00)
- `--time-end <HH:MM>` — End of time window (default: 23:00)
- `--return <YYYY-MM-DD>` — Return date for round-trip
- `--return-time <HH:MM>` — Return time (default: 06:00)
- `--adults <N>` — Number of adults (default: 1)
- `--children <N>` — Number of children (default: 0)
- `--class <class>` — economy, standard, business, executive
- `--limit <N>` — Max results (default: 20)
- `--sort <field>` — price, departure, duration (default: departure)

### 2. Search Italo (API — requires JWT)

> **⚠️ CRITICAL: Mai lanciare più di una ricerca Italo alla volta.**
> L'API Italo usa working-session server-side con connessioni limitate. Chiamate concorrenti o ravvicinate bruciano la sessione e servono nuovi token dal browser.
> Lo script ha un semaforo integrato (lockfile) che serializza le chiamate automaticamente: se una ricerca è in corso, le successive aspettano. Non serve gestirlo manualmente.
> Il polling è interno allo script (POST 202 → poll GET fino a risultato). Non chiamare lo script più volte pensando che il primo non abbia funzionato — aspetta che finisca (timeout 30s).

```bash
# One-way search
node search-italo.mjs --from MC_ --to RMT --date 2026-03-15

# With time window
node search-italo.mjs --from MC_ --to RMT --date 2026-03-15 --time 08:00 --time-end 14:00

# Filter by class
node search-italo.mjs --from MC_ --to RMT --date 2026-03-15 --class smart

# Sort by price
node search-italo.mjs --from MC_ --to RMT --date 2026-03-15 --sort price

# Refresh token only (no search)
node search-italo.mjs --refresh-only
```

**Options:**
- `--from <code>` — Origin station code (required, see table below)
- `--to <code>` — Destination station code (required)
- `--date <YYYY-MM-DD>` — Departure date (required)
- `--time <HH:MM>` — Start of time window (default: 06:00)
- `--time-end <HH:MM>` — End of time window (default: 23:00)
- `--adults <N>` — Number of adults (default: 1)
- `--children <N>` — Number of children (default: 0)
- `--class <class>` — economy/smart, comfort, prima/business, executive
- `--limit <N>` — Max results (default: 20)
- `--sort <field>` — price, departure, duration (default: departure)
- `--refresh-only` — Refresh token and exit

**Italo station codes:**

| Code | Station |
|------|---------|
| MC_ | Milano Centrale |
| RG_ | Milano Rogoredo |
| RMT | Roma Termini |
| RTB | Roma Tiburtina |
| NAC | Napoli Centrale |
| NAF | Napoli Afragola |
| BC_ | Bologna Centrale |
| SMN | Firenze S.M.N. |
| TOP | Torino P. Nuova |
| OUE | Torino P. Susa |
| VEM | Venezia Mestre |
| VSL | Venezia S. Lucia |
| PD_ | Padova |
| VPN | Verona P. Nuova |
| BAC | Bari Centrale |
| SAL | Salerno |
| AAV | Reggio Emilia AV |
| TSC | Trieste |
| UDN | Udine |
| BSC | Brescia |
| BGM | Bergamo |

### 3. Search Both Providers

When user asks to search trains, run **both**:
1. `search-trains.mjs` for Trenitalia (no auth, always works)
2. `search-italo.mjs` for Italo (needs valid JWT)
3. Present combined results sorted by user preference

### 4. Track a Train (Real-Time via ViaggiaTreno)

```bash
# Track by train number (today)
node track-train.mjs --train 9584

# Track on specific date
node track-train.mjs --train 9584 --date 2026-03-15

# Station departures board
node track-train.mjs --station "Milano Centrale"

# Station arrivals board
node track-train.mjs --station "Roma Termini" --arrivals

# Route search (direct trains from A to B)
node track-train.mjs --from "Milano" --to "Roma" --time 10:00
```

### 5. Station Lookup

```bash
node stations.mjs --query "Roma"
```

## Booking

This skill is **search-only**. Each result includes a `booking_url`:
- **Trenitalia**: Pre-filled URL with origin, destination, date and time
- **Italo**: Pre-filled URL with station codes and date

## Class Mapping

| Filter | Trenitalia | Italo |
|--------|-----------|-------|
| economy | 2a Classe / Economy | Smart |
| standard | 1a Classe / Standard | Smart XL |
| business | Business / Premium | Prima |
| executive | Executive | Club Executive |

## Train Types

### Trenitalia
- **FR** (Frecciarossa) — Fastest, Rome-Milan in ~3h
- **FA** (Frecciargento) — High-speed tilting
- **FB** (Frecciabianca) — High-speed conventional
- **IC** (Intercity) — Long-distance, more stops

### Italo
- **AGV** — Alstom AGV, flagship high-speed
- **EVO** — Pendolino EVO, newer fleet
- Classes: Smart < Smart XL < Prima < Club Executive

## Troubleshooting

- **Trenitalia 403** — Akamai cookies expired. Script auto-retries on init.
- **Italo "Session exhausted"** — Too many API calls on same session. Need fresh browser tokens.
- **Italo 401** — Session/refresh token expired. Run `--refresh-only` or re-extract from browser.
- **ViaggiaTreno 204** — Train cancelled or not in system.
- **Empty results** — Widen time window or check date is in the future.
- **All times are in Europe/Rome timezone.**
