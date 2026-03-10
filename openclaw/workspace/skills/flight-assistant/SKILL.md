---
name: flight-assistant
version: 2.0.0
description: Flight search and booking via Duffel API with Balance auto-payment or card+OTP async flow
requires:
  env:
    - DUFFEL_API_TOKEN
  binary:
    - node
invocable: true
---

# Flight Assistant Skill

Search, compare, and book flights using the Duffel API. Supports multiple airlines, one-way/return searches, and two payment methods: Balance (fully automatic) or Card (with async 3DS/OTP via browser).

## Architecture

The agent uses CLI tools via `exec`. Each tool is standalone and outputs structured JSON.

### Tools

| Tool | Purpose | Usage |
|------|---------|-------|
| `search-flights.mjs` | Search flights | `node search-flights.mjs --from FCO --to JFK --date 2026-04-01 [--return 2026-04-10]` |
| `check-flight.mjs` | Verify offer details/price + baggage options | `node check-flight.mjs --offer <offer_id>` |
| `save-booking.mjs` | Book flight (instant or hold) | `node save-booking.mjs --offer <offer_id> --passengers '<json>' [--services '<json>']` |
| `add-baggage.mjs` | List/add baggage to existing order | `node add-baggage.mjs --order <order_id> [--services '<json>']` |
| `confirm-payment.mjs` | Pay for held order | `node confirm-payment.mjs --order <order_id>` |
| `locations.mjs` | Resolve city/airport codes | `node locations.mjs --query "Rome"` |
| `lastminute-search.mjs` | Price comparison via lastminute.com | `node lastminute-search.mjs --from "Roma" --to "New York" --date 2026-04-01` |

### Scripts Location

```
cd ~/.openclaw/workspace/skills/flight-assistant/scripts && node <tool>.mjs <command>
```

### Environment Variables

All tools read from environment:
- `DUFFEL_API_TOKEN` — API token from duffel.com (required)
- `DUFFEL_PAYMENT_TYPE` — Payment method: `"balance"` (default, auto) or `"card"`
- `DUFFEL_EMAIL` — Default contact email for bookings
- `DUFFEL_PHONE` — Default contact phone for bookings

## Booking Workflow

### Baggage

Use `check-flight.mjs` before booking to see available baggage options (`baggage_services` in output).
Pass selected service IDs at booking time via `--services` in `save-booking.mjs` (preferred — single payment).
Alternatively, add baggage post-booking with `add-baggage.mjs --order <id> --services '<json>'`.

```json
// Example --services format
[{"id": "ase_xxx", "quantity": 1}]
```

### Passengers — Infants

When booking with infant_without_seat passengers, add `infant_passenger_id` to the accompanying adult:

```json
{
  "id": "pas_adult_id",
  "given_name": "Marco",
  ...
  "infant_passenger_id": "pas_infant_id"
}
```

---

### 1. Search Flights
```
node search-flights.mjs --from FCO --to JFK --date 2026-04-01 --return 2026-04-10
```
Returns a list of flights with prices, airlines, times, and `offer_id` for each result.
Each offer has an `expires_at` — book before it expires.

### 2. User Selects a Flight
Present results in a readable table. User picks one by number.

### 3. Check Offer Details (optional)
```
node check-flight.mjs --offer <offer_id>
```
Retrieves full offer details: price breakdown, conditions (refundable/changeable), available services (bags, seats).

### 4. Book the Flight

#### Instant Booking (Balance payment, fully automatic):
```
node save-booking.mjs --offer <offer_id> --passengers '[{
  "id": "<passenger_id from offer>",
  "given_name": "Marco",
  "family_name": "Monaco",
  "born_on": "1990-01-15",
  "email": "jarvis.monaco@gmail.com",
  "phone_number": "+39...",
  "title": "mr",
  "gender": "m"
}]'
```
This searches, books, and pays in one step using Duffel Balance. Returns `booking_reference` (PNR).

#### Hold Booking (pay later):
```
node save-booking.mjs --offer <offer_id> --passengers '<json>' --type hold
```
Reserves the flight without payment. Pay before the deadline using confirm-payment.

### 5. Confirm Payment (only for held orders)

#### Balance Payment (fully automatic):
```
node confirm-payment.mjs --order <order_id>
```
Deducts from pre-funded Duffel Balance. No human interaction needed.

#### Card Payment (async OTP flow):
When `DUFFEL_PAYMENT_TYPE=card`, 3D Secure authentication may be required:

1. **Agent** creates a 3DS session via Duffel dashboard or card form component
2. **Agent** opens the **browser with openclaw profile** to the Duffel card form URL
3. **Agent** tells user via Telegram:
   > "Pagamento in attesa di verifica 3DS. Inserisci il codice OTP ricevuto via SMS."
4. **User** sends OTP code via Telegram chat
5. **Agent** enters OTP in the browser and submits
6. **Agent** calls:
   ```
   node confirm-payment.mjs --order <order_id> --card --3ds-session <session_id>
   ```
7. **Agent** sends booking confirmation recap via Telegram

## Payment Methods Explained

| Method | Config | Flow | OTP Needed |
|--------|--------|------|------------|
| **Balance** | `DUFFEL_PAYMENT_TYPE=balance` | Pre-fund account -> API auto-deduct | No |
| **Card** | `DUFFEL_PAYMENT_TYPE=card` | Card form -> 3DS browser -> OTP async | Yes |

**Recommended: Balance** — pre-fund the Duffel account via bank transfer, then all bookings are 100% automatic via API. No OTP, no browser, no human interaction.

## Search Features

### Cabin Classes
- `economy` (default)
- `premium_economy`
- `business`
- `first`

### Flight Types
- One-way: only `--date`
- Return: `--date` + `--return`

### Passengers
- `--adults N` (default: 1)
- `--children "age1,age2"` (ages 2-11)
- `--infants "age1"` (ages 0-1)

### Sorting
- `--sort price` (default)
- `--sort duration`

### Filters
- `--max-connections N` — Max connections per slice (0 = direct only, default: 1)

## Price Comparison

For price comparison, use both Duffel and lastminute.com MCP:
```bash
cd ~/.openclaw/workspace/skills/flight-assistant/scripts
node search-flights.mjs --from FCO --to JFK --date 2026-04-01
node lastminute-search.mjs --from "Rome" --to "New York" --date 2026-04-01
```
Present both results to the user to choose the best deal. Booking is only available via Duffel.

## Credential Retrieval

### API Token (TPM)

The `DUFFEL_API_TOKEN` env var is resolved automatically by the gateway from the TPM vault:

```
openclaw.json → skills.entries.flight-assistant.env.DUFFEL_API_TOKEN = "$DUFFEL_API_TOKEN"
                 ↓
              TPM vault resolves $DUFFEL_API_TOKEN → actual token injected into script env
```

No plaintext tokens in `.env` files. The TPM key name is `DUFFEL_API_TOKEN`.

### Account Credentials (Ontology)

The Duffel account username and password are stored in the ontology graph. The agent can query them at runtime:

```bash
# Get account info (username)
curl -s http://127.0.0.1:8100/api/entities?type=Account&service=duffel.com \
  -H "Authorization: Bearer $ONTOLOGY_API_TOKEN" | jq '.[0]'
# → { "username": "jarvis.monaco@gmail.com", ... }

# Get credential secret_ref (TPM key name for password)
curl -s http://127.0.0.1:8100/api/entities/<credential_id> \
  -H "Authorization: Bearer $ONTOLOGY_API_TOKEN" | jq '.secret_ref'
# → "DUFFEL_PASSWORD"

# Resolve password from TPM
~/.openclaw/secrets/scripts/tpm-secret-resolver.sh DUFFEL_PASSWORD
```

This pattern lets the agent retrieve Duffel login credentials for browser-based actions (e.g., dashboard access, card payment 3DS setup) without hardcoding anything.

### Entity Structure

```
Person (Jarvis-agent)
  ├── owns → Account (duffel.com, jarvis.monaco@gmail.com)
  │            └── has_credential → Credential (DUFFEL_PASSWORD in TPM)
  └── (future) owns → Credential (credit card PAN:EXP:CVV in TPM)
```

## Troubleshooting

- **Offer expired** — Offers have limited validity (`expires_at`). Search again if expired.
- **Rate limiting** — If 429 received, wait and retry.
- **3DS timeout** — User has ~5 minutes to enter OTP before the 3DS session expires.
- **Payment deadline** — Held orders have a `payment_required_by` deadline. Pay before it passes.
- **Balance insufficient** — Top up Duffel Balance via bank transfer in the dashboard.
