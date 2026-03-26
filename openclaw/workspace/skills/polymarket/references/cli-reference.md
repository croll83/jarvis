# Polymarket CLI — Full Command Reference

> Official Rust CLI: [github.com/Polymarket/polymarket-cli](https://github.com/Polymarket/polymarket-cli)

## Global Flags

| Flag | Description |
|------|-------------|
| `-o json` | JSON output (default: table) |
| `-o table` | Human-readable table output |
| `--private-key 0x...` | Override wallet key (or use env `POLYMARKET_PRIVATE_KEY`) |

## Authentication Resolution Order

1. CLI flag: `--private-key 0xabc...`
2. Environment variable: `POLYMARKET_PRIVATE_KEY=0xabc...`
3. Config file: `~/.config/polymarket/config.json`

## Signature Types

| Type | Description |
|------|-------------|
| `proxy` | Polymarket's legacy Magic/email proxy wallet (EIP-1167 minimal proxy via factory `0xaB45c5A4...`) |
| `gnosis-safe` | Browser/MetaMask wallet — Gnosis Safe 1-of-1 (this is what polymarket.com creates when you sign in with a browser wallet) |
| `eoa` | Direct signing with private key (no derived wallet) |

**Our config uses `gnosis-safe`** — this matches the trading wallet created by the Polymarket website.
The CLI reads `signature_type` from `~/.config/polymarket/config.json` and derives the correct trading wallet address accordingly.

---

## Market Discovery

### `markets list`
List markets with optional filters, sorting, and pagination.

```bash
# Active markets sorted by volume (most common usage)
polymarket -o json markets list --active true --order volume_num --limit 10

# Active markets sorted by liquidity
polymarket -o json markets list --active true --order liquidity_num --limit 10

# With pagination
polymarket -o json markets list --active true --order volume_num --limit 10 --offset 20

# Ascending order
polymarket -o json markets list --active true --order volume_num --ascending --limit 10
```

**Flags:**
- `--active true|false` — filter by active status (true = open, false = closed/resolved)
- `--closed true|false` — inverse of active
- `--order FIELD` — sort field: `volume_num`, `liquidity_num`
- `--ascending` — sort ascending instead of descending (default)
- `--limit N` — max results (default: 25)
- `--offset N` — pagination offset

### `markets search`
Search markets by keyword.

```bash
polymarket -o json markets search "election"
polymarket -o json markets search "Bitcoin price" --limit 5
```

**Flags:** `--limit N`

### `markets get`
Detailed info for a specific market (by slug or numeric ID).

```bash
polymarket -o json markets get "will-trump-win-2024"
polymarket -o json markets get <NUMERIC_ID>
```

**Returns:** title, description, outcomes, end date, volume, liquidity, resolution source.

---

## Events & Tags

### `events list`
Browse events by category.

```bash
polymarket -o json events list --limit 10
polymarket -o json events list --tag politics
polymarket -o json events list --tag crypto
```

**Flags:** `--tag TAG`, `--limit N`, `--offset N`

### `events tags`
List all available event categories/tags.

```bash
polymarket -o json events tags
```

---

## Price Data

### `clob midpoint`
Get current midpoint price for a token (YES or NO outcome).

```bash
polymarket -o json clob midpoint <TOKEN_ID>
```

**Returns:** midpoint price (0.00–1.00)

### `clob midpoints`
Batch midpoint prices for multiple tokens.

```bash
polymarket -o json clob midpoints <TOKEN_1> <TOKEN_2> <TOKEN_3>
```

### `clob book`
Full order book depth.

```bash
polymarket -o json clob book <TOKEN_ID>
```

**Returns:** bids and asks with price and size.

### `clob last-trade`
Last trade info for a token.

```bash
polymarket -o json clob last-trade <TOKEN_ID>
```

### `clob spread`
Bid-ask spread for a token.

```bash
polymarket -o json clob spread <TOKEN_ID>
```

---

## Historical Data

### `data price-history`
Historical price data across multiple intervals.

```bash
polymarket -o json data price-history <TOKEN_ID> --interval 1d
```

**Intervals:** `1m`, `1h`, `6h`, `1d`, `1w`, `max`

### `data positions`
View positions for any wallet address (public data).

```bash
polymarket -o json data positions <WALLET_ADDRESS>
```

**Returns:** open and closed positions with entry price, current value, P&L.

### `data activity`
Trade activity log for a wallet.

```bash
polymarket -o json data activity <WALLET_ADDRESS>
```

---

## Portfolio & Account (Requires Wallet)

### `clob balance`
Check USDC balance and conditional token holdings.

```bash
POLYMARKET_PRIVATE_KEY=0x... polymarket -o json clob balance
```

### `clob orders`
List open orders.

```bash
POLYMARKET_PRIVATE_KEY=0x... polymarket -o json clob orders
```

### `clob trades`
Recent trade fills.

```bash
POLYMARKET_PRIVATE_KEY=0x... polymarket -o json clob trades
```

### `clob rewards`
Check earnings and reward percentages.

```bash
POLYMARKET_PRIVATE_KEY=0x... polymarket -o json clob rewards
```

---

## Trading (Requires Wallet)

### `clob market-order`
Execute immediately at best available price.

```bash
# Buy YES for $10
POLYMARKET_PRIVATE_KEY=0x... polymarket -o json clob market-order \
  --token <TOKEN_ID> --side buy --amount 10

# Sell YES shares worth $5
POLYMARKET_PRIVATE_KEY=0x... polymarket -o json clob market-order \
  --token <TOKEN_ID> --side sell --amount 5
```

**Flags:**
- `--token TOKEN_ID` (required)
- `--side buy|sell` (required)
- `--amount DOLLARS` (required) — dollar amount to spend/receive

**Order types:** FOK (fill-or-kill) or FAK (fill-and-kill).

### `clob limit-order`
Place a resting order at a specific price.

```bash
# Buy 20 shares at $0.65
POLYMARKET_PRIVATE_KEY=0x... polymarket -o json clob limit-order \
  --token <TOKEN_ID> --side buy --price 0.65 --size 20

# Sell 15 shares at $0.80
POLYMARKET_PRIVATE_KEY=0x... polymarket -o json clob limit-order \
  --token <TOKEN_ID> --side sell --price 0.80 --size 15
```

**Flags:**
- `--token TOKEN_ID` (required)
- `--side buy|sell` (required)
- `--price PRICE` (required) — between 0.01 and 0.99
- `--size SHARES` (required) — number of shares

**Order types:** GTC (good-til-cancelled) or GTD (good-til-date).

### `clob cancel`
Cancel a specific order by ID.

```bash
POLYMARKET_PRIVATE_KEY=0x... polymarket -o json clob cancel <ORDER_ID>
```

### `clob cancel-all`
Cancel all open orders.

```bash
POLYMARKET_PRIVATE_KEY=0x... polymarket -o json clob cancel-all
```

### `clob post-orders`
Batch post multiple orders at once.

```bash
POLYMARKET_PRIVATE_KEY=0x... polymarket -o json clob post-orders \
  --orders '[{"token":"...","side":"buy","price":0.65,"size":10}]'
```

---

## On-Chain Operations (Requires Wallet + POL gas)

### `approve set`
Approve USDC and CTF token contracts for trading. **Required once before first trade.**

```bash
POLYMARKET_PRIVATE_KEY=0x... polymarket approve set
```

### `approve status`
Check current approval status.

```bash
POLYMARKET_PRIVATE_KEY=0x... polymarket approve status
```

### `clob redeem`
Redeem winnings after a market resolves.

```bash
POLYMARKET_PRIVATE_KEY=0x... polymarket -o json clob redeem <CONDITION_ID>
```

### CTF Token Operations

```bash
# Split collateral (USDC) into YES + NO outcome tokens
POLYMARKET_PRIVATE_KEY=0x... polymarket ctf split <CONDITION_ID> <AMOUNT>

# Merge YES + NO tokens back into USDC
POLYMARKET_PRIVATE_KEY=0x... polymarket ctf merge <CONDITION_ID> <AMOUNT>
```

---

## Wallet Management

### `wallet create`
Generate a new random wallet.

```bash
polymarket wallet create
```

### `wallet import`
Import an existing private key.

```bash
polymarket wallet import 0xPRIVATE_KEY
```

---

## Utility Commands

### `data leaderboard`
View top traders.

```bash
polymarket -o json data leaderboard --limit 10
```

### `data profile`
Public profile for a wallet address.

```bash
polymarket -o json data profile <WALLET_ADDRESS>
```

### `data comments`
Comments on a market.

```bash
polymarket -o json data comments <CONDITION_ID>
```

### `data holders`
Market holders and open interest.

```bash
polymarket -o json data holders <CONDITION_ID>
```

### `clob health`
API health status.

```bash
polymarket -o json clob health
```

### `info geoblock`
Check geoblock status.

```bash
polymarket -o json info geoblock
```

### `info fees`
Current fee structure.

```bash
polymarket -o json info fees
```

---

## Interactive Shell

Launch an interactive REPL with command history:

```bash
polymarket shell
```

---

## Key Concepts

| Concept | Description |
|---------|-------------|
| **Token ID** | Identifies a specific outcome (YES or NO) within a market |
| **Condition ID** | Identifies the overall market/question |
| **Slug** | Human-readable market URL identifier |
| **Price** | Always $0.00–$1.00, represents implied probability |
| **Shares** | Pay $1.00 if outcome resolves YES, $0.00 if NO |
| **USDC** | Stablecoin on Polygon used for all trades |
| **POL** | Polygon native token, needed for gas (approvals, on-chain ops) |
| **Trading wallet** | Derived contract wallet (Gnosis Safe or Proxy) used for CLOB trading — set via `signature_type` in config |
