"""
Test live order placement: place a $0.01 limit buy YES on current BTC5M market,
wait 5 seconds, cancel it. Verifies Polymarket CLOB credentials work end-to-end.

Usage:
    python scripts/test_live_order.py

Required .env vars:
    POLYMARKET_API_KEY
    POLYMARKET_SECRET        (base64url-encoded HMAC secret)
    POLYMARKET_PASSPHRASE
    POLYMARKET_WALLET_ADDRESS  (0x... Ethereum address)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
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

# ------------------------------------------------------------------ #
# Auth                                                                #
# ------------------------------------------------------------------ #

def _hmac_sig(secret: str, timestamp: int, method: str, path: str, body: str) -> str:
    """Matches py-clob-client build_hmac_signature exactly."""
    key = base64.urlsafe_b64decode(secret)
    message = str(timestamp) + method + path + body.replace("'", '"')
    digest = hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8")


def _auth_headers(method: str, path: str, body: str = "") -> dict[str, str]:
    creds = _get_creds()
    timestamp = int(datetime.now().timestamp())
    return {
        "POLY_ADDRESS": creds["wallet_address"],
        "POLY_SIGNATURE": _hmac_sig(creds["secret"], timestamp, method, path, body),
        "POLY_TIMESTAMP": str(timestamp),
        "POLY_API_KEY": creds["api_key"],
        "POLY_PASSPHRASE": creds["passphrase"],
        "Content-Type": "application/json",
    }


def _get_creds() -> dict[str, str]:
    missing = []
    api_key = os.getenv("POLYMARKET_API_KEY", "")
    secret = os.getenv("POLYMARKET_SECRET", "")
    passphrase = os.getenv("POLYMARKET_PASSPHRASE", "")
    wallet = os.getenv("POLYMARKET_WALLET_ADDRESS", "")
    if not api_key:      missing.append("POLYMARKET_API_KEY")
    if not secret:       missing.append("POLYMARKET_SECRET")
    if not passphrase:   missing.append("POLYMARKET_PASSPHRASE")
    if not wallet:       missing.append("POLYMARKET_WALLET_ADDRESS")
    if missing:
        raise RuntimeError(f"Missing .env vars: {', '.join(missing)}")
    return {"api_key": api_key, "secret": secret, "passphrase": passphrase, "wallet_address": wallet}

# ------------------------------------------------------------------ #
# Market discovery (mirrors PolymarketClient._is_btc5m_market)       #
# ------------------------------------------------------------------ #

def _is_btc5m_market(item: dict) -> bool:
    for event in item.get("events", []):
        for s in event.get("series", []):
            if s.get("ticker") == SERIES_TICKER:
                return True
    series: str = item.get("series_ticker") or item.get("seriesTicker") or ""
    ticker: str = item.get("ticker") or ""
    return series == SERIES_TICKER or ticker == SERIES_TICKER


def _parse_market(item: dict) -> dict | None:
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
            "end_date": end_date,
            "yes_token_id": str(token_ids[0]),
        }
    except Exception as exc:
        log.warning("Failed to parse market: %s — %s", exc, item)
        return None


async def _find_current_market(client: httpx.AsyncClient) -> dict:
    now = datetime.now(timezone.utc)
    resp = await client.get(
        f"{GAMMA_URL}/markets",
        params={
            "active": "true",
            "closed": "false",
            "limit": 500,
            "order": "endDate",
            "ascending": "true",
            "end_date_min": now.isoformat(),
            "end_date_max": (now + timedelta(minutes=15)).isoformat(),
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    data = resp.json()
    items = data if isinstance(data, list) else data.get("markets", [])

    candidates = [
        m for item in items
        if isinstance(item, dict) and _is_btc5m_market(item)
        for m in [_parse_market(item)] if m
    ]
    if not candidates:
        raise RuntimeError(
            f"No active BTC5M markets (series_ticker={SERIES_TICKER}). Between windows?"
        )
    market = min(candidates, key=lambda m: m["end_date"])
    log.info(
        "Market: %s  end=%s  token=%s",
        market["question"], market["end_date"].isoformat(), market["yes_token_id"],
    )
    return market

# ------------------------------------------------------------------ #
# Order placement / cancellation                                      #
# ------------------------------------------------------------------ #

async def _place_order(client: httpx.AsyncClient, yes_token_id: str) -> str:
    payload = {
        "orderType": "GTC",
        "tokenID": yes_token_id,
        "side": "BUY",
        "price": str(TEST_PRICE),
        "size": str(TEST_SIZE),
        "feeRateBps": "0",
    }
    body = json.dumps(payload)
    resp = await client.post(
        f"{CLOB_URL}/order",
        content=body,
        headers=_auth_headers("POST", "/order", body),
        timeout=15.0,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected response type {type(data)}: {data!r}")
    order_id = data.get("orderID") or data.get("order_id") or data.get("id")
    if not order_id:
        raise RuntimeError(f"No order_id in response: {data}")
    log.info("Order placed: id=%s  price=%.2f  size=%.2f", order_id, TEST_PRICE, TEST_SIZE)
    return str(order_id)


async def _cancel_order(client: httpx.AsyncClient, order_id: str) -> None:
    path = f"/order/{order_id}"
    resp = await client.delete(
        f"{CLOB_URL}{path}",
        headers=_auth_headers("DELETE", path),
        timeout=15.0,
    )
    resp.raise_for_status()
    log.info("Order cancelled: id=%s", order_id)

# ------------------------------------------------------------------ #
# Main                                                                #
# ------------------------------------------------------------------ #

async def run() -> None:
    _get_creds()  # validate env vars before hitting network

    async with httpx.AsyncClient() as client:
        market = await _find_current_market(client)

        log.info(
            "Placing test order: token=%s price=%.2f size=%.2f",
            market["yes_token_id"], TEST_PRICE, TEST_SIZE,
        )
        t0 = time.monotonic()
        order_id = await _place_order(client, market["yes_token_id"])

        log.info("Waiting 5 seconds before cancelling...")
        await asyncio.sleep(5)

        await _cancel_order(client, order_id)
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
