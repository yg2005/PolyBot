"""
Test live order placement: place a $0.01 limit buy YES on current BTC5M market,
wait 5 seconds, cancel it. Verifies Polymarket CLOB credentials work end-to-end.

Usage:
    python scripts/test_live_order.py

Requires .env with POLYMARKET_API_KEY and POLYMARKET_PRIVATE_KEY.
"""
from __future__ import annotations

import asyncio
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

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("test_live_order")

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
SERIES_TICKER = "btc-up-or-down-5m"

TEST_PRICE = 0.01   # intentionally unfillable
TEST_SIZE = 1.0     # $1 notional


def _get_creds() -> tuple[str, str]:
    api_key = os.getenv("POLYMARKET_API_KEY", "")
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    if not api_key or not private_key:
        raise RuntimeError(
            "POLYMARKET_API_KEY and POLYMARKET_PRIVATE_KEY must be set in .env"
        )
    return api_key, private_key


def _is_btc5m_market(item: dict) -> bool:
    """Same logic as PolymarketClient._is_btc5m_market."""
    for event in item.get("events", []):
        for s in event.get("series", []):
            if s.get("ticker") == SERIES_TICKER:
                return True
    series: str = item.get("series_ticker") or item.get("seriesTicker") or ""
    ticker: str = item.get("ticker") or ""
    return series == SERIES_TICKER or ticker == SERIES_TICKER


def _parse_market(item: dict) -> dict | None:
    """Extract fields needed for order placement."""
    try:
        raw = item.get("clobTokenIds") or item.get("clob_token_ids") or []
        token_ids: list[str] = json.loads(raw) if isinstance(raw, str) else raw
        if len(token_ids) < 2:
            return None
        end_raw = (
            item.get("endDate")
            or item.get("end_date_utc")
            or item.get("end_date")
            or ""
        )
        end_date = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        return {
            "question": item.get("question") or item.get("title") or "",
            "condition_id": str(item.get("conditionId") or ""),
            "end_date": end_date,
            "yes_token_id": str(token_ids[0]),
        }
    except Exception as exc:
        log.warning("Failed to parse market item: %s — %s", exc, item)
        return None


async def _find_current_market(client: httpx.AsyncClient) -> dict:
    """Return the nearest active BTC5M market using the same query as the bot."""
    now = datetime.now(timezone.utc)
    end_max = now + timedelta(minutes=15)

    resp = await client.get(
        f"{GAMMA_URL}/markets",
        params={
            "active": "true",
            "closed": "false",
            "limit": 500,
            "order": "endDate",
            "ascending": "true",
            "end_date_min": now.isoformat(),
            "end_date_max": end_max.isoformat(),
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    data = resp.json()
    items = data if isinstance(data, list) else data.get("markets", [])

    candidates: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if not _is_btc5m_market(item):
            continue
        parsed = _parse_market(item)
        if parsed:
            candidates.append(parsed)

    if not candidates:
        raise RuntimeError(
            f"No active BTC5M markets found (series_ticker={SERIES_TICKER}). "
            "Is the market currently between windows?"
        )

    # nearest by end_date
    market = min(candidates, key=lambda m: m["end_date"])
    log.info(
        "Found market: %s  end=%s  yes_token=%s",
        market["question"],
        market["end_date"].isoformat(),
        market["yes_token_id"],
    )
    return market


async def _place_order(
    client: httpx.AsyncClient, api_key: str, yes_token_id: str
) -> str:
    """Place a GTC limit buy YES at TEST_PRICE. Returns order_id."""
    payload = {
        "order": {
            "tokenID": yes_token_id,
            "price": str(TEST_PRICE),
            "side": "BUY",
            "size": str(TEST_SIZE),
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
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected order response type {type(data)}: {data!r}")
    order_id = data.get("orderID") or data.get("order_id") or data.get("id")
    if not order_id:
        raise RuntimeError(f"No order_id in response: {data}")
    log.info("Order placed: order_id=%s  price=%.2f  size=%.2f", order_id, TEST_PRICE, TEST_SIZE)
    return str(order_id)


async def _cancel_order(
    client: httpx.AsyncClient, api_key: str, order_id: str
) -> None:
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

        log.info(
            "Placing test order: YES token=%s price=%.2f size=%.2f",
            market["yes_token_id"], TEST_PRICE, TEST_SIZE,
        )
        t0 = time.monotonic()
        order_id = await _place_order(client, api_key, market["yes_token_id"])

        log.info("Waiting 5 seconds before cancelling...")
        await asyncio.sleep(5)

        await _cancel_order(client, api_key, order_id)
        elapsed = time.monotonic() - t0

    print(f"\nSUCCESS — place+cancel round-trip in {elapsed:.1f}s")
    print(f"  market   : {market['question']}")
    print(f"  order_id : {order_id}")
    print(f"  price    : {TEST_PRICE}")
    print(f"  size_usd : {TEST_SIZE}")


def main() -> None:
    try:
        asyncio.run(run())
    except Exception as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
