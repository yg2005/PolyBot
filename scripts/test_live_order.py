"""
Test live order placement via polymarket-client (official Polymarket SDK).

Flow:
  1. Init SecureClient — auto-derives deposit wallet and API credentials
  2. Find the nearest active BTC5M market via Gamma API
  3. Place a $1 limit buy YES at $0.01 (intentionally unfillable — sits deep in book)
  4. Wait 5 seconds, cancel it
  5. Report success

Required .env:
    POLYMARKET_PRIVATE_KEY    — 0x-prefixed Ethereum private key
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
from polymarket import AcceptedOrder, SecureClient

from kalbot.feeds.polymarket import BTC5M_SERIES_TICKER, is_btc5m_market

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("test_live_order")

GAMMA_URL = "https://gamma-api.polymarket.com"

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
        raise RuntimeError(
            f"No active {BTC5M_SERIES_TICKER} markets found"
            " — try between :00 and :04 of any 5-min window"
        )
    return min(candidates, key=lambda m: m["end_date"])


def main() -> None:
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    if not private_key:
        raise SystemExit("POLYMARKET_PRIVATE_KEY not set in .env")

    log.info("Initialising SecureClient (derives wallet + credentials) ...")
    with SecureClient.create(private_key=private_key) as client:
        log.info("Wallet: %s", client.wallet)

        log.info("Searching for active BTC5M market ...")
        market = _find_btc5m_market()
        log.info(
            "Market: %s  end=%s  token=%s",
            market["question"], market["end_date"].isoformat(), market["yes_token_id"],
        )

        shares = TEST_SIZE_USDC / TEST_PRICE  # 1.0 / 0.01 = 100.0
        log.info(
            "Placing order: price=%.4f  size=%.2f shares  (~$%.2f USDC)",
            TEST_PRICE, shares, TEST_SIZE_USDC,
        )

        t0 = time.monotonic()
        resp = client.place_limit_order(
            token_id=market["yes_token_id"],
            price=TEST_PRICE,
            size=shares,
            side="BUY",
        )

        if not isinstance(resp, AcceptedOrder):
            raise SystemExit(f"Order rejected: code={resp.code}  msg={resp.message}")

        order_id = resp.order_id
        log.info("Order placed: id=%s  status=%s", order_id, resp.status)

        log.info("Waiting 5 seconds before cancelling ...")
        time.sleep(5)

        cancel_resp = client.cancel_order(order_id=order_id)
        cancelled = order_id in cancel_resp.canceled
        log.info(
            "Cancel response: cancelled=%s  not_cancelled=%s",
            cancel_resp.canceled, cancel_resp.not_canceled,
        )

        elapsed = time.monotonic() - t0
        print(f"\nSUCCESS — place+cancel round-trip in {elapsed:.1f}s")
        print(f"  market   : {market['question']}")
        print(f"  order_id : {order_id}")
        print(f"  cancelled: {cancelled}")
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
