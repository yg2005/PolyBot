"""
Test live order placement via py-clob-client (official Polymarket SDK).

Flow:
  1. Init ClobClient with POLY_PROXY signing (proxy wallet as maker)
  2. Derive API credentials via GET /auth/derive-api-key
  3. Find the nearest active BTC5M market via Gamma API
  4. Place a $1 limit buy YES at $0.01 (intentionally unfillable — sits deep in book)
  5. Wait 5 seconds, cancel it
  6. Report success

Required .env:
    POLYMARKET_PRIVATE_KEY    — 0x-prefixed Ethereum private key
    POLYMARKET_PROXY_WALLET   — proxy/deposit wallet address (from Polymarket UI)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import httpx
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs
from py_order_utils.model import POLY_PROXY

from kalbot.feeds.polymarket import BTC5M_SERIES_TICKER, is_btc5m_market

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("test_live_order")

CLOB_URL      = "https://clob.polymarket.com"
GAMMA_URL     = "https://gamma-api.polymarket.com"
CHAIN_ID      = 137

TEST_PRICE     = 0.01  # intentionally unfillable — sits deep in the book
TEST_SIZE_USDC = 1.0   # $1 notional → 100 shares at $0.01


def _find_btc5m_market() -> dict:
    """Return the nearest active BTC5M market from Gamma API."""
    now = datetime.now(timezone.utc)
    with httpx.Client(timeout=10.0) as http:
        resp = http.get(
            f"{GAMMA_URL}/markets",
            params={
                "active": "true", "closed": "false", "limit": 500,
                "order": "endDate", "ascending": "true",
                "end_date_min": now.isoformat(),
                "end_date_max": (now + timedelta(minutes=15)).isoformat(),
            },
        )
        resp.raise_for_status()

    items = resp.json()
    if not isinstance(items, list):
        items = items.get("markets", [])

    candidates = []
    for item in items:
        if not is_btc5m_market(item):
            continue
        raw = item.get("clobTokenIds") or item.get("clob_token_ids") or "[]"
        ids = json.loads(raw) if isinstance(raw, str) else raw
        if len(ids) < 2:
            continue
        end_raw = (
            item.get("endDate") or item.get("end_date_utc") or item.get("end_date") or ""
        )
        try:
            end = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        candidates.append({
            "question":     item.get("question") or "",
            "end_date":     end,
            "yes_token_id": str(ids[0]),
        })

    if not candidates:
        raise RuntimeError(f"No active {BTC5M_SERIES_TICKER} markets found — try between :00 and :04 of any 5-min window")
    return min(candidates, key=lambda m: m["end_date"])


def main() -> None:
    private_key  = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    proxy_wallet = os.getenv("POLYMARKET_PROXY_WALLET", "")
    if not private_key:
        raise SystemExit("POLYMARKET_PRIVATE_KEY not set in .env")
    if not proxy_wallet:
        raise SystemExit(
            "POLYMARKET_PROXY_WALLET not set in .env — "
            "complete the Polymarket deposit flow and copy your proxy wallet address."
        )

    # Level 1: key only (no creds yet)
    client = ClobClient(
        host=CLOB_URL,
        key=private_key,
        chain_id=CHAIN_ID,
        signature_type=POLY_PROXY,
        funder=proxy_wallet,
    )
    log.info("EOA address: %s", client.get_address())
    log.info("Proxy (funder): %s", proxy_wallet)

    # Derive API credentials → Level 2
    log.info("Deriving API credentials ...")
    creds = client.derive_api_key()
    if creds is None:
        raise SystemExit("derive_api_key() returned None — check private key and network")
    log.info("api_key: %s...", creds.api_key[:8])
    client.set_api_creds(creds)

    # Find market
    log.info("Searching for active BTC5M market ...")
    market = _find_btc5m_market()
    log.info(
        "Market: %s  end=%s  token=%s",
        market["question"], market["end_date"].isoformat(), market["yes_token_id"],
    )

    # size is in ConditionalTokens (shares), not USDC
    # BUY at price p: spend size*p USDC, receive size shares
    shares = TEST_SIZE_USDC / TEST_PRICE   # 1.0 / 0.01 = 100.0
    order_args = OrderArgs(
        token_id=market["yes_token_id"],
        price=TEST_PRICE,
        size=shares,
        side="BUY",
    )
    log.info(
        "Placing order: price=%.4f  size=%.2f shares  (~$%.2f USDC)",
        TEST_PRICE, shares, TEST_SIZE_USDC,
    )

    t0 = time.monotonic()
    place_resp = client.create_and_post_order(order_args)
    print(f"\n[RESP] POST /order:\n{json.dumps(place_resp, indent=2)}")

    order_id = (
        (place_resp or {}).get("orderID")
        or (place_resp or {}).get("order_id")
        or (place_resp or {}).get("id")
    )
    if not order_id:
        raise SystemExit(f"No order_id in response: {place_resp}")
    log.info("Order placed: id=%s", order_id)

    log.info("Waiting 5 seconds before cancelling ...")
    time.sleep(5)

    cancel_resp = client.cancel(order_id)
    print(f"\n[RESP] DELETE /order:\n{json.dumps(cancel_resp, indent=2)}")
    log.info("Order cancelled: id=%s", order_id)

    elapsed = time.monotonic() - t0
    print(f"\nSUCCESS — place+cancel round-trip in {elapsed:.1f}s")
    print(f"  market   : {market['question']}")
    print(f"  order_id : {order_id}")
    print(f"  price    : {TEST_PRICE}")
    print(f"  shares   : {shares}")
    print(f"  usdc     : ${TEST_SIZE_USDC}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        import traceback
        print(f"\nFAILED: {exc}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
