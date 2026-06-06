"""
Test live order placement: place a $0.01 limit buy YES on current BTC5M market,
wait 5 seconds, cancel it. Verifies Polymarket CLOB credentials work end-to-end.

Usage:
    python scripts/test_live_order.py

Requires .env with POLYMARKET_API_KEY and POLYMARKET_PRIVATE_KEY.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import logging
from pathlib import Path

# Allow running from repo root without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import httpx
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("test_live_order")

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
SERIES_TICKER = "btc-up-or-down-5m"

# Intentionally unfilllable price — will never match
TEST_PRICE = 0.01
TEST_SIZE_USD = 1.0


def _get_creds() -> tuple[str, str]:
    api_key = os.getenv("POLYMARKET_API_KEY", "")
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    if not api_key or not private_key:
        raise RuntimeError(
            "POLYMARKET_API_KEY and POLYMARKET_PRIVATE_KEY must be set in .env"
        )
    return api_key, private_key


async def _find_current_market(client: httpx.AsyncClient) -> dict:
    """Return the nearest active BTC5M market from Gamma API."""
    resp = await client.get(
        f"{GAMMA_URL}/markets",
        params={"series_ticker": SERIES_TICKER, "active": "true", "limit": "10"},
        timeout=10.0,
    )
    resp.raise_for_status()
    markets = resp.json()
    if not markets:
        raise RuntimeError(f"No active markets found for series_ticker={SERIES_TICKER}")
    # Take first (nearest) market
    market = markets[0] if isinstance(markets, list) else markets.get("markets", [])[0]
    log.info("Market: %s  condition_id=%s", market.get("question", "?"), market.get("conditionId", "?"))
    return market


async def _place_order(
    client: httpx.AsyncClient, api_key: str, condition_id: str, token_id: str
) -> str:
    """Place a GTC limit buy YES at $0.01. Returns order_id."""
    payload = {
        "order": {
            "tokenID": token_id,
            "price": str(TEST_PRICE),
            "side": "BUY",
            "size": str(TEST_SIZE_USD),
            "type": "LIMIT",
            "timeInForce": "GTC",
        },
        "owner": api_key,
        "orderType": "LIMIT",
    }
    resp = await client.post(
        f"{CLOB_URL}/order",
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=15.0,
    )
    resp.raise_for_status()
    data = resp.json()
    order_id = data.get("orderID") or data.get("order_id") or data.get("id")
    if not order_id:
        raise RuntimeError(f"No order_id in response: {data}")
    log.info("Order placed: order_id=%s price=%.2f size=%.2f", order_id, TEST_PRICE, TEST_SIZE_USD)
    return order_id


async def _cancel_order(client: httpx.AsyncClient, api_key: str, order_id: str) -> None:
    resp = await client.delete(
        f"{CLOB_URL}/order/{order_id}",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=15.0,
    )
    resp.raise_for_status()
    log.info("Order cancelled: order_id=%s", order_id)


async def run() -> None:
    api_key, _private_key = _get_creds()

    async with httpx.AsyncClient() as client:
        market = await _find_current_market(client)
        condition_id = market.get("conditionId") or market.get("condition_id", "")

        # YES token is tokens[0] by Polymarket convention
        tokens = market.get("tokens") or market.get("clobTokenIds") or []
        if not tokens:
            raise RuntimeError("Could not find token IDs in market response")
        yes_token_id = tokens[0] if isinstance(tokens, list) else tokens.get("yes", "")

        log.info(
            "Placing test order: YES token=%s price=%.2f size=%.2f",
            yes_token_id, TEST_PRICE, TEST_SIZE_USD,
        )
        t0 = time.monotonic()
        order_id = await _place_order(client, api_key, condition_id, yes_token_id)

        log.info("Waiting 5 seconds before cancelling...")
        await asyncio.sleep(5)

        await _cancel_order(client, api_key, order_id)
        elapsed = time.monotonic() - t0

    print(f"\nSUCCESS — place+cancel round-trip completed in {elapsed:.1f}s")
    print(f"  market   : {market.get('question', '?')}")
    print(f"  order_id : {order_id}")
    print(f"  price    : {TEST_PRICE}")
    print(f"  size_usd : {TEST_SIZE_USD}")


def main() -> None:
    try:
        asyncio.run(run())
    except Exception as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
