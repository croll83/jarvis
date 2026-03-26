---
name: polymarket
description: "Trade and monitor prediction markets on Polymarket. Browse markets, check prices and order books, place limit/market orders, manage positions and portfolio. Use when the user asks about prediction markets, event betting, Polymarket positions, or wants to trade on Polymarket."
metadata:
  {
    "openclaw": {
      "emoji": "🔮",
      "requires": {
        "bins": ["polymarket"]
      },
      "install": [
        {
          "id": "cargo",
          "kind": "shell",
          "command": "git clone -b feat/socks-proxy-support https://github.com/croll83/polymarket-cli.git /tmp/polymarket-cli && . $HOME/.cargo/env && cargo install --path /tmp/polymarket-cli && rm -rf /tmp/polymarket-cli",
          "bins": ["polymarket"],
          "label": "Build from source with SOCKS5 support (requires Rust 1.88+)"
        }
      ]
    }
  }
---

# Polymarket Trading Skill

Full trading and portfolio management for Polymarket prediction markets (Polygon chain).

## Prerequisites

- Binary installed at `~/.cargo/bin/polymarket` (built from source with SOCKS5 proxy support).
- `wireproxy-mullvad` systemd service running (provides SOCKS5 on `127.0.0.1:1080` via Mullvad ES exit). See `resources/SOCKS5-PROXY-SETUP.md` for full setup instructions.
- Proxy configured in `~/.config/polymarket/config.json` — the CLI reads it automatically.

## Building & Updating the CLI

We build from our fork which adds SOCKS5 proxy support and gnosis-safe wallet derivation fix.

**First-time install:**
```bash
sudo apt-get install -y build-essential
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh  # if Rust not installed
source $HOME/.cargo/env

git clone -b feat/socks-proxy-support https://github.com/croll83/polymarket-cli.git /tmp/polymarket-cli
cd /tmp/polymarket-cli && cargo install --path . && rm -rf /tmp/polymarket-cli
```

**Updating to a new version:**
```bash
git clone -b feat/socks-proxy-support https://github.com/croll83/polymarket-cli.git /tmp/polymarket-cli
cd /tmp/polymarket-cli && cargo install --path . --force && rm -rf /tmp/polymarket-cli
```

For full build docs (syncing with upstream, etc.) see `references/build-ubuntu24.md`.

## Authentication

**For read-only operations (browse markets, prices, order books):**
- No wallet or private key needed. Just run the command.

**For trading operations (place/cancel orders, check balances):**
- The wallet private key is available as the `JARVIS_WALLET` environment variable (injected by TPM preboot).
- Pass it to the CLI via `POLYMARKET_PRIVATE_KEY`:

```bash
# Read-only — no key needed:
polymarket -o json markets search "Bitcoin"

# Trading — pass JARVIS_WALLET as POLYMARKET_PRIVATE_KEY:
POLYMARKET_PRIVATE_KEY=$JARVIS_WALLET polymarket -o json clob balance --asset-type collateral
```

## Proxy

The SOCKS5 proxy URL is configured in `~/.config/polymarket/config.json` and applied automatically for most read operations.

**⚠️ IMPORTANT: For CLOB write operations (placing orders, cancelling), you MUST also set `ALL_PROXY` env var.** The config proxy is not applied to POST requests by the Rust HTTP client. Always prefix trading commands with:
```bash
ALL_PROXY="socks5://127.0.0.1:1080" HTTPS_PROXY="socks5://127.0.0.1:1080" POLYMARKET_PRIVATE_KEY=$JARVIS_WALLET polymarket ...
```

If commands fail with connection/geoblock errors, check the proxy service:
```bash
systemctl --user status wireproxy-mullvad
# Restart if needed:
systemctl --user restart wireproxy-mullvad
```

## Output Format

Always use `-o json` for machine-readable output:
```bash
polymarket -o json <command>
```

Parse JSON and format results for chat display. Use `-o table` only if the user explicitly asks for raw output.

## Core Operations

### Market Discovery

**IMPORTANT: always use `--active true` to exclude resolved/closed markets, and `--order` to sort by relevance.**

**List active markets (by volume):**
```bash
polymarket -o json markets list --active true --order volume --limit 10
```

**List active markets (by liquidity):**
```bash
polymarket -o json markets list --active true --order liquidity --limit 10
```

**Sort ascending (lowest first):**
```bash
polymarket -o json markets list --active true --order volume --ascending --limit 10
```

**Available `--order` fields:** `volume`, `liquidity` (default: descending; add `--ascending` to reverse).

**Search markets by keyword:**
```bash
polymarket -o json markets search "Trump"
polymarket -o json markets search "Bitcoin" --limit 5
```
Note: search already filters for active/relevant markets.

**Get detailed market info (by slug or numeric ID):**
```bash
polymarket -o json markets get "will-trump-win-2024"
polymarket -o json markets get <NUMERIC_ID>
```

**Browse by category/tag:**
```bash
polymarket -o json events tags
polymarket -o json events list --tag politics --limit 10
```

### Price Data & Order Books

**Get current midpoint price:**
```bash
polymarket -o json clob midpoint <TOKEN_ID>
```

**Get full order book:**
```bash
polymarket -o json clob book <TOKEN_ID>
```

**Get price history:**
```bash
polymarket -o json data price-history <TOKEN_ID> --interval 1d
# intervals: 1m, 1h, 6h, 1d, 1w, max
```

### Portfolio Monitoring

**Check balance (USDC on Polygon):**
```bash
POLYMARKET_PRIVATE_KEY=$JARVIS_WALLET polymarket -o json clob balance --asset-type collateral
```

**View open positions:**
```bash
polymarket -o json data positions <WALLET_ADDRESS>
```

**View open orders:**
```bash
POLYMARKET_PRIVATE_KEY=$JARVIS_WALLET polymarket -o json clob orders
```

**View trade history:**
```bash
POLYMARKET_PRIVATE_KEY=$JARVIS_WALLET polymarket -o json clob trades
```

### Trading Operations

**Market order (fill-or-kill):**
```bash
# Buy YES outcome for $10
POLYMARKET_PRIVATE_KEY=$JARVIS_WALLET polymarket -o json clob market-order \
  --token <TOKEN_ID> --side buy --amount 10

# Sell position
POLYMARKET_PRIVATE_KEY=$JARVIS_WALLET polymarket -o json clob market-order \
  --token <TOKEN_ID> --side sell --amount 10
```

**Limit order (good-til-cancelled):**
```bash
# Buy 20 shares at $0.65
POLYMARKET_PRIVATE_KEY=$JARVIS_WALLET polymarket -o json clob limit-order \
  --token <TOKEN_ID> --side buy --price 0.65 --size 20

# Sell 15 shares at $0.80
POLYMARKET_PRIVATE_KEY=$JARVIS_WALLET polymarket -o json clob limit-order \
  --token <TOKEN_ID> --side sell --price 0.80 --size 15
```

**Cancel orders:**
```bash
# Cancel specific order
POLYMARKET_PRIVATE_KEY=$JARVIS_WALLET polymarket -o json clob cancel <ORDER_ID>

# Cancel all open orders
POLYMARKET_PRIVATE_KEY=$JARVIS_WALLET polymarket -o json clob cancel-all
```

### On-Chain Operations (no proxy needed — RPC is not geoblocked)

**Approve contracts (first-time setup):**
```bash
POLYMARKET_PRIVATE_KEY=$JARVIS_WALLET polymarket approve set
```

**Redeem winnings after market resolution:**

First, determine if the market is a NegRisk market by checking the CLOB API:
```bash
polymarket -o json clob market <CONDITION_ID>
```
Look for `"neg_risk": true` in the response.

**Standard markets (neg_risk is false or absent):**
```bash
POLYMARKET_PRIVATE_KEY=$JARVIS_WALLET polymarket -o json ctf redeem --condition <CONDITION_ID>
```

**NegRisk markets (neg_risk is true):**
NegRisk markets MUST be redeemed via the NegRisk adapter. Using `ctf redeem` on a NegRisk market will succeed on-chain but produce payout=0.
```bash
# First check position size — look at your token balance for this market
polymarket -o json data positions <TRADING_WALLET_ADDRESS>

# Then redeem: amounts are comma-separated per outcome in USDC (YES amount, NO amount)
# Example: you hold 10 USDC worth of YES tokens → amounts "10,0"
POLYMARKET_PRIVATE_KEY=$JARVIS_WALLET polymarket -o json ctf redeem-neg-risk \
  --condition <CONDITION_ID> --amounts "<YES_AMOUNT>,<NO_AMOUNT>"
```

## Safety Guidelines

**Before executing any trade:**
1. Confirm trade parameters with the user (market, outcome, amount/size, price)
2. Show current market price for context
3. Show account balance to confirm sufficient funds
4. For market orders: state the estimated cost
5. For limit orders: compare limit price vs current market price — warn if >10% away

**Position sizing:**
- Warn if trade exceeds 25% of available balance
- Always show the max loss scenario (amount paid = max loss for YES, since Polymarket is binary)

**Important reminders for chat:**
- Polymarket is binary outcomes (YES/NO). Price = implied probability. $0.65 = 65% implied probability.
- Shares pay $1.00 if the outcome resolves YES, $0.00 if NO.
- Max loss = amount paid. Max profit = (number of shares × $1.00) - amount paid.

## Error Handling

**Common errors:**
- "Insufficient balance" → need more USDC, deposit via bridge
- "Insufficient allowance" → run `polymarket approve set`
- "Order too small" → minimum order size applies
- "Geoblocked" / connection errors → check `systemctl --user status wireproxy-mullvad`, restart if needed
- "error sending request" → proxy may be down, try `systemctl --user restart wireproxy-mullvad`

**When errors occur:**
- Show the error message to the user
- Suggest a fix
- Never retry trades automatically

## Workflow Examples

**"What are the top prediction markets right now?"**
1. `polymarket -o json markets list --active true --order volume --limit 10`
2. Format: title, current YES price (= probability), volume, end date

**"What's the probability of X happening?"**
1. `polymarket -o json markets search "X"`
2. Find matching market
3. `polymarket -o json clob midpoint <TOKEN_ID>`
4. Format: "Market: [title]. Current price: $0.XX (XX% implied probability)"

**"Buy $20 of YES on that market"**
1. Get current midpoint price
2. Check balance (with key)
3. Confirm with user: "Buy $20 of YES on [market]? Current price: $0.XX. You'll get ~XX shares. Max loss: $20, max profit: $XX if YES resolves."
4. Execute market order (with key)
5. Report fill result

**"Show my Polymarket portfolio"**
1. Get balance (with key)
2. Get positions
3. Format: total USDC balance, each position (market, side, size, avg entry, current price, unrealized P&L)

## Depositing Funds

The CLI does not have a deposit command. To add funds to the CLOB:

1. Get the deposit address: `polymarket -o json wallet show` → use the `trading_wallet` address from the JSON output
2. Send USDC.e (on Polygon) to that trading wallet address
3. Polymarket automatically credits the CLOB balance within ~1 minute
4. Verify: `POLYMARKET_PRIVATE_KEY=$JARVIS_WALLET polymarket -o json clob balance --asset-type collateral`

## Infrastructure

- **Proxy service:** `wireproxy-mullvad.service` — WireGuard userspace tunnel via Mullvad ES (Spain) exit nodes
- **SOCKS5:** `127.0.0.1:1080` — configured in `~/.config/polymarket/config.json`, applied automatically
- **Failover:** if a Mullvad server drops, systemd restarts the launcher which picks a new server automatically
- **Active server:** `cat /tmp/wireproxy-active-server.txt` to see which Mullvad node is in use
- **Full setup docs:** `resources/SOCKS5-PROXY-SETUP.md` — install wireproxy, configure Mullvad, systemd service

## Notes

- Polymarket uses USDC.e on Polygon for trading. The CLOB accepts both USDC.e and native USDC on Polygon, but USDC.e is the primary token. Use `polymarket bridge` to bridge from Ethereum if needed.
- Token IDs identify specific outcomes (YES or NO) within a market
- Condition IDs identify the overall market (used for redemption)
- All prices are between $0.00 and $1.00 (probability)
- Markets resolve to either $1.00 (YES) or $0.00 (NO)
- For full CLI reference, see `references/cli-reference.md`
