"""
Test live order placement: place a $0.01 limit buy YES on current BTC5M market,
wait 5 seconds, cancel it. Verifies Polymarket CLOB credentials work end-to-end.

Usage:
    python scripts/test_live_order.py

Required .env:
    POLYMARKET_PRIVATE_KEY   — Ethereum wallet private key (hex, with or without 0x)

Optional (derived automatically from POLYMARKET_PRIVATE_KEY if absent):
    POLYMARKET_API_KEY       — if you want to verify a specific key
    POLYMARKET_SECRET        — base64url HMAC secret
    POLYMARKET_PASSPHRASE

Auth flow:
  1. EIP-712 L1 sign  →  GET /auth/derive-api-key  →  (api_key, secret, passphrase)
  2. HMAC-SHA256 L2 sign the order POST and cancel DELETE
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

CLOB_URL = "https://clob.polymarket.com"
GAMMA_URL = "https://gamma-api.polymarket.com"
SERIES_TICKER = "btc-up-or-down-5m"
POLYGON_CHAIN_ID = 137

TEST_PRICE = 0.01   # intentionally unfillable
TEST_SIZE = 1.0     # $1 notional

# ------------------------------------------------------------------ #
# L1 — EIP-712 signing (for credential derivation)                   #
# ------------------------------------------------------------------ #

_MSG_TO_SIGN = "This message attests that I control the given wallet"


def _eip712_sign(private_key: str, timestamp: int, nonce: int, chain_id: int) -> tuple[str, str]:
    """Sign ClobAuth EIP-712 struct. Returns (address, 0x-signature)."""
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    account = Account.from_key(private_key)
    structured = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "ClobAuth": [
                {"name": "address", "type": "string"},
                {"name": "timestamp", "type": "string"},
                {"name": "nonce", "type": "uint256"},
                {"name": "message", "type": "string"},
            ],
        },
        "domain": {"name": "ClobAuthDomain", "version": "1", "chainId": chain_id},
        "primaryType": "ClobAuth",
        "message": {
            "address": account.address,
            "timestamp": str(timestamp),
            "nonce": nonce,
            "message": _MSG_TO_SIGN,
        },
    }
    msg = encode_typed_data(full_message=structured)
    signed = account.sign_message(msg)
    return account.address, "0x" + signed.signature.hex()


def _l1_headers(private_key: str, nonce: int = 0) -> dict[str, str]:
    timestamp = int(datetime.now().timestamp())
    address, sig = _eip712_sign(private_key, timestamp, nonce, POLYGON_CHAIN_ID)
    return {
        "POLY_ADDRESS": address,
        "POLY_SIGNATURE": sig,
        "POLY_TIMESTAMP": str(timestamp),
        "POLY_NONCE": str(nonce),
    }


# ------------------------------------------------------------------ #
# L2 — HMAC-SHA256 request signing                                    #
# ------------------------------------------------------------------ #

def _hmac_sig(secret: str, timestamp: int, method: str, path: str, body: str) -> str:
    key = base64.urlsafe_b64decode(secret)
    message = str(timestamp) + method + path + body.replace("'", '"')
    digest = hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8")


def _l2_headers(
    api_key: str, secret: str, passphrase: str, wallet: str,
    method: str, path: str, body: str = "",
) -> dict[str, str]:
    timestamp = int(datetime.now().timestamp())
    return {
        "POLY_ADDRESS": wallet,
        "POLY_SIGNATURE": _hmac_sig(secret, timestamp, method, path, body),
        "POLY_TIMESTAMP": str(timestamp),
        "POLY_API_KEY": api_key,
        "POLY_PASSPHRASE": passphrase,
        "Content-Type": "application/json",
    }


# ------------------------------------------------------------------ #
# Credential loading / derivation                                     #
# ------------------------------------------------------------------ #

async def _load_creds(client: httpx.AsyncClient) -> dict[str, str]:
    """Return L2 creds dict, deriving from private key if needed."""
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    if not private_key:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY must be set in .env")

    api_key = os.getenv("POLYMARKET_API_KEY", "")
    secret = os.getenv("POLYMARKET_SECRET", "")
    passphrase = os.getenv("POLYMARKET_PASSPHRASE", "")

    if not secret or not passphrase:
        log.info("POLYMARKET_SECRET not set — deriving L2 credentials from private key ...")
        headers = _l1_headers(private_key)
        resp = await client.get(f"{CLOB_URL}/auth/derive-api-key", headers=headers, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        api_key = data.get("apiKey") or data.get("api_key", api_key)
        secret = data.get("secret", "")
        passphrase = data.get("passphrase", "")
        if not secret or not passphrase:
            raise RuntimeError(f"Unexpected derive-api-key response: {data}")
        log.info("Derived api_key: %s...", api_key[:8])
        log.info(
            "Add to .env to skip derivation next time:\n"
            "  POLYMARKET_API_KEY=%s\n  POLYMARKET_SECRET=%s\n  POLYMARKET_PASSPHRASE=%s",
            api_key, secret, passphrase,
        )

    from eth_account import Account
    wallet = os.getenv("POLYMARKET_WALLET_ADDRESS") or Account.from_key(private_key).address

    return {"api_key": api_key, "secret": secret, "passphrase": passphrase, "wallet": wallet}


# ------------------------------------------------------------------ #
# Market discovery                                                    #
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
            item.get("endDate") or item.get("end_date_utc") or item.get("end_date") or ""
        )
        return {
            "question": item.get("question") or item.get("title") or "",
            "end_date": datetime.fromisoformat(end_raw.replace("Z", "+00:00")),
            "yes_token_id": str(token_ids[0]),
        }
    except Exception as exc:
        log.warning("Failed to parse market: %s", exc)
        return None


async def _find_current_market(client: httpx.AsyncClient) -> dict:
    now = datetime.now(timezone.utc)
    resp = await client.get(
        f"{GAMMA_URL}/markets",
        params={
            "active": "true", "closed": "false", "limit": 500,
            "order": "endDate", "ascending": "true",
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
        raise RuntimeError(f"No active BTC5M markets. Between windows?")
    market = min(candidates, key=lambda m: m["end_date"])
    log.info("Market: %s  end=%s", market["question"], market["end_date"].isoformat())
    return market


# ------------------------------------------------------------------ #
# Order placement / cancellation                                      #
# ------------------------------------------------------------------ #

async def _place_order(client: httpx.AsyncClient, creds: dict, yes_token_id: str) -> str:
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
        headers=_l2_headers(
            creds["api_key"], creds["secret"], creds["passphrase"], creds["wallet"],
            "POST", "/order", body,
        ),
        timeout=15.0,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected response type: {data!r}")
    order_id = data.get("orderID") or data.get("order_id") or data.get("id")
    if not order_id:
        raise RuntimeError(f"No order_id in response: {data}")
    log.info("Order placed: id=%s  price=%.2f  size=%.2f", order_id, TEST_PRICE, TEST_SIZE)
    return str(order_id)


async def _cancel_order(client: httpx.AsyncClient, creds: dict, order_id: str) -> None:
    path = f"/order/{order_id}"
    resp = await client.delete(
        f"{CLOB_URL}{path}",
        headers=_l2_headers(
            creds["api_key"], creds["secret"], creds["passphrase"], creds["wallet"],
            "DELETE", path,
        ),
        timeout=15.0,
    )
    resp.raise_for_status()
    log.info("Order cancelled: id=%s", order_id)


# ------------------------------------------------------------------ #
# Main                                                                #
# ------------------------------------------------------------------ #

async def run() -> None:
    async with httpx.AsyncClient() as client:
        creds = await _load_creds(client)
        market = await _find_current_market(client)

        log.info(
            "Placing test order: token=%s  price=%.2f  size=%.2f",
            market["yes_token_id"], TEST_PRICE, TEST_SIZE,
        )
        t0 = time.monotonic()
        order_id = await _place_order(client, creds, market["yes_token_id"])

        log.info("Waiting 5 seconds before cancelling...")
        await asyncio.sleep(5)

        await _cancel_order(client, creds, order_id)
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
