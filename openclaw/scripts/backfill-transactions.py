#!/usr/bin/env python3
"""
Backfill ontology Transaction entities from Hyperliquid fill history.
All trades attributed to Scalping strategy.
Run on the LXC where ontology API is at localhost:8100.
"""

import json
import os
import uuid
import urllib.request
from datetime import datetime, timezone

HL_API = "https://api.hyperliquid.xyz/info"
WALLET = "0x2bA250B38673Ff0Ac9c8d931B1AF17Ee6f66871b"
ONTOLOGY_URL = os.environ.get("ONTOLOGY_URL", "http://127.0.0.1:8100")
ONTOLOGY_TOKEN = os.environ["ONTOLOGY_API_TOKEN"]  # required — never hardcode
ONTOLOGY_SPEAKER = os.environ.get("ONTOLOGY_SPEAKER", "jarvis-agent")
SCALPING_ID = "stra_65b1c509"


def hl_fetch(req_type):
    body = {"type": req_type, "user": WALLET}
    data = json.dumps(body).encode()
    req = urllib.request.Request(HL_API, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def ontology_req(method, path, body=None):
    url = f"{ONTOLOGY_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json",
        "X-Speaker-Id": ONTOLOGY_SPEAKER,
        "Authorization": f"Bearer {ONTOLOGY_TOKEN}",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  API error on {method} {path}: {e}")
        return None


def get_entity_id(entity_type, query):
    result = ontology_req("POST", f"/entities/query?type={entity_type}", query)
    return result[0]["id"] if result else None


def main():
    # Step 0: Delete existing transactions
    print("Cleaning existing transactions...")
    existing = ontology_req("GET", "/entities?type=Transaction")
    if existing:
        for tx in existing:
            ontology_req("DELETE", f"/entities/{tx['id']}")
        print(f"  Deleted {len(existing)} existing transactions")
    else:
        print("  No existing transactions to delete")

    # Step 1: Fetch fills
    print("\nFetching HL fills...")
    fills = hl_fetch("userFills")
    fills.sort(key=lambda f: f["time"])
    print(f"Total fills: {len(fills)}")

    account_id = get_entity_id("Account", {"service": "jarvis wallet"})
    agent_id = get_entity_id("Person", {"name": "Agent Trading"})
    print(f"Account: {account_id}, Agent: {agent_id}")

    # Step 2: Group fills by oid → orders
    oid_map = {}
    for f in fills:
        oid = f["oid"]
        if oid not in oid_map:
            oid_map[oid] = []
        oid_map[oid].append(f)

    orders = []
    for oid, group in oid_map.items():
        coin = group[0]["coin"]
        direction = group[0]["dir"]
        total_sz = sum(float(f["sz"]) for f in group)
        total_notional = sum(float(f["px"]) * float(f["sz"]) for f in group)
        avg_px = total_notional / total_sz if total_sz > 0 else 0
        total_pnl = sum(float(f["closedPnl"]) for f in group)
        ts = group[0]["time"]

        is_open = direction.startswith("Open")
        is_buy = ("Long" in direction) == is_open

        orders.append({
            "oid": oid,
            "coin": coin,
            "dir": direction,
            "is_open": is_open,
            "is_buy": is_buy,
            "size": round(total_sz, 8),
            "price": round(avg_px, 6),
            "notional": round(total_notional, 6),
            "pnl": round(total_pnl, 6),
            "time_ms": ts,
            "timestamp": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(),
        })

    orders.sort(key=lambda o: o["time_ms"])
    print(f"Unique orders: {len(orders)}")

    # Step 3: Track position lifecycles per coin
    coin_positions = {}
    coin_lifecycles = {}
    completed_trades = []

    for order in orders:
        coin = order["coin"]
        if coin not in coin_positions:
            coin_positions[coin] = 0.0
            coin_lifecycles[coin] = None

        lc = coin_lifecycles[coin]

        if order["is_open"]:
            if lc is None:
                lc = {"opens": [], "closes": [], "coin": coin}
                coin_lifecycles[coin] = lc

            lc["opens"].append(order)
            delta = order["size"] if order["is_buy"] else -order["size"]
            coin_positions[coin] += delta

        else:
            if lc is None:
                lc = {"opens": [], "closes": [], "coin": coin}
                coin_lifecycles[coin] = lc

            lc["closes"].append(order)
            delta = order["size"] if order["is_buy"] else -order["size"]
            coin_positions[coin] += delta

            if abs(coin_positions[coin]) < 0.0001:
                coin_positions[coin] = 0.0
                completed_trades.append(lc)
                coin_lifecycles[coin] = None

    # Still-open positions → append as open trades
    for coin, lc in coin_lifecycles.items():
        if lc is not None:
            completed_trades.append(lc)

    print(f"\nPosition lifecycles: {len(completed_trades)}")
    for t in completed_trades:
        opens = t["opens"]
        closes = t["closes"]
        coin = t["coin"]
        status = "CLOSED" if closes else "OPEN"
        total_pnl = sum(c["pnl"] for c in closes)
        open_sz = sum(o["size"] for o in opens)
        ts_str = datetime.fromtimestamp(opens[0]["time_ms"] / 1000, tz=timezone.utc).strftime("%m/%d %H:%M UTC") if opens else "?"
        print(f"  {coin:5s} | Scalping | {status:6s} | sz={open_sz:>10.6f} | opens={len(opens)} closes={len(closes)} | PnL={total_pnl:+.6f} | {ts_str}")

    # Step 4: Create Transaction entities — all Scalping
    print("\n--- Creating ontology entities ---")
    created = 0
    errors = 0

    for trade in completed_trades:
        trade_id = str(uuid.uuid4())
        coin = trade["coin"]
        opens = trade["opens"]
        closes = trade["closes"]

        def create_tx(tx_type, sz, notional, px, ts, pnl=None, status="confirmed"):
            nonlocal created, errors
            entity = {
                "type": "Transaction",
                "properties": {
                    "type": tx_type,
                    "account": account_id or "unknown",
                    "asset_in": "USDC" if tx_type == "buy" else coin,
                    "amount_in": round(notional, 2) if tx_type == "buy" else round(sz, 8),
                    "asset_out": coin if tx_type == "buy" else "USDC",
                    "amount_out": round(sz, 8) if tx_type == "buy" else round(notional, 2),
                    "status": status,
                    "timestamp": ts,
                    "executor": ONTOLOGY_SPEAKER,
                    "visibility": "family",
                    "trade_id": trade_id,
                },
            }
            if pnl is not None:
                entity["properties"]["closed_pnl"] = round(pnl, 6)

            result = ontology_req("POST", "/entities", entity)
            if result and result.get("id"):
                tx_id = result["id"]
                created += 1
                # Relations: originated_from Scalping, affects_account, executed_by
                ontology_req("POST", "/relations", {"from_id": tx_id, "rel_type": "originated_from", "to_id": SCALPING_ID})
                if account_id:
                    ontology_req("POST", "/relations", {"from_id": tx_id, "rel_type": "affects_account", "to_id": account_id})
                if agent_id:
                    ontology_req("POST", "/relations", {"from_id": tx_id, "rel_type": "executed_by", "to_id": agent_id})
                label = "CLOSE" if pnl is not None else "OPEN"
                pnl_str = f" PnL={pnl:+.4f}" if pnl is not None else ""
                print(f"  {label:5s} {coin:5s} {tx_type:4s} {sz:>10.6f} @ {px:>10.2f}{pnl_str} | {tx_id}")
            else:
                errors += 1
                print(f"  ERROR {coin} {tx_type}")

        # Open transaction (aggregate of all opening fills)
        if opens:
            open_sz = sum(o["size"] for o in opens)
            open_notional = sum(o["notional"] for o in opens)
            open_px = open_notional / open_sz if open_sz > 0 else 0
            open_ts = opens[0]["timestamp"]
            open_type = "buy" if opens[0]["is_buy"] else "sell"
            create_tx(open_type, open_sz, open_notional, open_px, open_ts)

        # Close transaction (aggregate of all closing fills)
        if closes:
            close_sz = sum(c["size"] for c in closes)
            close_notional = sum(c["notional"] for c in closes)
            close_px = close_notional / close_sz if close_sz > 0 else 0
            close_ts = closes[-1]["timestamp"]
            close_type = "buy" if closes[0]["is_buy"] else "sell"
            total_pnl = sum(c["pnl"] for c in closes)
            create_tx(close_type, close_sz, close_notional, close_px, close_ts, pnl=total_pnl, status="closed")

    print(f"\nDone! Created: {created}, Errors: {errors}")


if __name__ == "__main__":
    main()
