"""
Test live order placement: place a $0.01 limit buy YES on current BTC5M market,
wait 5 seconds, cancel it. Verifies Polymarket CLOB credentials work end-to-end.

Usage:
    python scripts/test_live_order.py          # normal run
    python scripts/test_live_order.py --debug  # print signing details

Auth flow:
  1. Load POLYMARKET_PRIVATE_KEY (Ethereum hex key) from .env
  2. EIP-712 sign → GET /auth/derive-api-key → (api_key, secret, passphrase)
  3. Build EIP-712 signed Order struct (POLY_PROXY, maker=proxy wallet)
  4. HMAC-SHA256 sign the order POST and cancel DELETE

Required .env:
    POLYMARKET_PRIVATE_KEY    — 0x-prefixed hex Ethereum private key
    POLYMARKET_PROXY_WALLET   — proxy/deposit wallet address (from Polymarket UI)
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

DEBUG = "--debug" in sys.argv
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import httpx
from dotenv import load_dotenv
from eth_account import Account
from kalbot.execution.clob_auth import build_signed_order

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("test_live_order")

CLOB_URL       = "https://clob.polymarket.com"
GAMMA_URL      = "https://gamma-api.polymarket.com"
SERIES_TICKER  = "btc-up-or-down-5m"
CHAIN_ID       = 137

TEST_PRICE = 0.01   # intentionally unfillable — sits deep in the book
TEST_SIZE  = 1.0    # $1 notional

_CLOB_AUTH_MSG = "This message attests that I control the given wallet"


# ------------------------------------------------------------------ #
# L1 — EIP-712 credential derivation                                 #
# ------------------------------------------------------------------ #

def _eip712_typed_data(address: str, timestamp: int, nonce: int) -> dict:
    return {
        "domain": {"name": "ClobAuthDomain", "version": "1", "chainId": CHAIN_ID},
        "types": {
            "EIP712Domain": [
                {"name": "name",    "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "ClobAuth": [
                {"name": "address",   "type": "address"},  # "address", not "string"
                {"name": "timestamp", "type": "string"},
                {"name": "nonce",     "type": "uint256"},
                {"name": "message",   "type": "string"},
            ],
        },
        "primaryType": "ClobAuth",
        "message": {
            "address":   address,
            "timestamp": str(timestamp),
            "nonce":     0,
            "message":   _CLOB_AUTH_MSG,
        },
    }


async def _derive_creds(client: httpx.AsyncClient, private_key: str) -> dict:
    account   = Account.from_key(private_key)
    timestamp = int(time.time())
    data      = _eip712_typed_data(account.address, timestamp, 0)
    signed    = account.sign_typed_data(full_message=data)

    headers = {
        "POLY_ADDRESS":   account.address,
        "POLY_SIGNATURE": "0x" + signed.signature.hex(),
        "POLY_TIMESTAMP": str(timestamp),
        "POLY_NONCE":     "0",
    }
    log.info("Deriving credentials for wallet %s ...", account.address)
    resp = await client.get(f"{CLOB_URL}/auth/derive-api-key", headers=headers, timeout=10.0)
    if not resp.is_success:
        raise RuntimeError(
            f"GET /auth/derive-api-key failed {resp.status_code}: {resp.text[:300]}"
        )
    body = resp.json()
    api_key    = body.get("apiKey")     or body.get("api_key", "")
    secret     = body.get("secret",    "")
    passphrase = body.get("passphrase","")
    if not api_key or not secret or not passphrase:
        raise RuntimeError(f"Unexpected /auth/derive-api-key response: {body}")
    log.info("Credentials derived: api_key=%s...", api_key[:8])
    return {
        "api_key":    api_key,
        "secret":     secret,
        "passphrase": passphrase,
        "wallet":     account.address,
    }


# ------------------------------------------------------------------ #
# L2 — HMAC-SHA256 request signing                                   #
# ------------------------------------------------------------------ #

def _pad_b64(v: str) -> str:
    return v + "=" * ((-len(v)) % 4)


def _hmac_sig(secret: str, timestamp: int, method: str, path: str, body: str) -> str:
    key     = base64.urlsafe_b64decode(_pad_b64(secret))
    message = str(timestamp) + method + path + (body if body else "")
    digest  = hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def _auth_headers(creds: dict, method: str, path: str, body: str = "") -> dict[str, str]:
    timestamp = int(time.time())
    sig = _hmac_sig(creds["secret"], timestamp, method, path, body)
    if DEBUG:
        print(f"\n[DEBUG] {method} {path}")
        print(f"  signing string : {timestamp!r}{method!r}{path!r}"
              f"{(body[:60]+'…' if len(body) > 60 else body)!r}")
        print(f"  POLY_ADDRESS   : {creds['wallet']}")
        print(f"  POLY_API_KEY   : {creds['api_key'][:8]}...")
        print(f"  POLY_PASSPHRASE: {creds['passphrase'][:8]}...")
        print(f"  POLY_SIGNATURE : {sig[:16]}...")
        print(f"  POLY_TIMESTAMP : {timestamp}")
    return {
        "POLY_ADDRESS":   creds["wallet"],
        "POLY_API_KEY":   creds["api_key"],
        "POLY_PASSPHRASE": creds["passphrase"],
        "POLY_SIGNATURE": sig,
        "POLY_TIMESTAMP": str(timestamp),
        "Content-Type":   "application/json",
    }


# ------------------------------------------------------------------ #
# Market discovery                                                    #
# ------------------------------------------------------------------ #

def _is_btc5m(item: dict) -> bool:
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
        ids: list[str] = json.loads(raw) if isinstance(raw, str) else raw
        if len(ids) < 2:
            return None
        end_raw = item.get("endDate") or item.get("end_date_utc") or item.get("end_date") or ""
        return {
            "question":     item.get("question") or item.get("title") or "",
            "end_date":     datetime.fromisoformat(end_raw.replace("Z", "+00:00")),
            "yes_token_id": str(ids[0]),
        }
    except Exception as exc:
        log.warning("Failed to parse market: %s", exc)
        return None


async def _find_market(client: httpx.AsyncClient) -> dict:
    now  = datetime.now(timezone.utc)
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
    data  = resp.json()
    items = data if isinstance(data, list) else data.get("markets", [])
    candidates = [
        m for item in items
        if isinstance(item, dict) and _is_btc5m(item)
        for m in [_parse_market(item)] if m
    ]
    if not candidates:
        raise RuntimeError("No active BTC5M markets — between windows?")
    market = min(candidates, key=lambda m: m["end_date"])
    log.info("Market: %s  end=%s", market["question"], market["end_date"].isoformat())
    return market


# ------------------------------------------------------------------ #
# Order placement / cancellation                                      #
# ------------------------------------------------------------------ #

async def _place_order(
    client: httpx.AsyncClient,
    creds: dict,
    token_id: str,
    private_key: str,
    proxy_wallet: str,
) -> str:
    signed_order = build_signed_order(
        private_key=private_key,
        proxy_wallet=proxy_wallet,
        token_id=token_id,
        side="BUY",
        price=TEST_PRICE,
        size_usdc=TEST_SIZE,
        fee_rate_bps=0,
    )
    if DEBUG:
        print(f"\n[DEBUG] signed_order={json.dumps(signed_order, indent=2)}")

    payload = {
        "order":     signed_order,
        "owner":     creds["api_key"],
        "orderType": "GTC",
        "postOnly":  False,
    }
    body = json.dumps(payload)
    resp = await client.post(
        f"{CLOB_URL}/order",
        content=body,
        headers=_auth_headers(creds, "POST", "/order", body),
        timeout=15.0,
    )
    if not resp.is_success:
        raise RuntimeError(f"POST /order failed {resp.status_code}: {resp.text[:300]}")
    data     = resp.json()
    order_id = data.get("orderID") or data.get("order_id") or data.get("id")
    if not order_id:
        raise RuntimeError(f"No order_id in response: {data}")
    log.info("Order placed: id=%s  price=%.2f  size=%.2f", order_id, TEST_PRICE, TEST_SIZE)
    return str(order_id)


async def _cancel_order(client: httpx.AsyncClient, creds: dict, order_id: str) -> None:
    path = f"/order/{order_id}"
    resp = await client.delete(
        f"{CLOB_URL}{path}",
        headers=_auth_headers(creds, "DELETE", path),
        timeout=15.0,
    )
    if not resp.is_success:
        raise RuntimeError(f"DELETE {path} failed {resp.status_code}: {resp.text[:300]}")
    log.info("Order cancelled: id=%s", order_id)


# ------------------------------------------------------------------ #
# Main                                                                #
# ------------------------------------------------------------------ #

async def run() -> None:
    private_key  = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    proxy_wallet = os.getenv("POLYMARKET_PROXY_WALLET", "")
    if not private_key:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY not set in .env")
    if not proxy_wallet:
        raise RuntimeError(
            "POLYMARKET_PROXY_WALLET not set in .env. "
            "Complete the Polymarket deposit flow and copy your proxy wallet address."
        )

    async with httpx.AsyncClient() as client:
        creds  = await _derive_creds(client, private_key)
        market = await _find_market(client)
        log.info(
            "Placing test order: token=%s  price=%.2f  size=%.2f",
            market["yes_token_id"], TEST_PRICE, TEST_SIZE,
        )
        t0       = time.monotonic()
        order_id = await _place_order(
            client, creds, market["yes_token_id"], private_key, proxy_wallet
        )
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
