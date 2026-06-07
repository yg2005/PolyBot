"""
Test live order placement: place a $0.01 limit buy YES on current BTC5M market,
wait 5 seconds, cancel it. Verifies Polymarket CLOB credentials work end-to-end.

Usage:
    python scripts/test_live_order.py          # normal run
    python scripts/test_live_order.py --debug  # print signing details + headers

Required .env:
    POLYMARKET_API_KEY    — API key
    POLYMARKET_SECRET     — base64url HMAC secret (or POLYMARKET_PRIVATE_KEY fallback)

Optional .env (sent as empty string if absent, not omitted):
    POLYMARKET_PASSPHRASE     — POLY_PASSPHRASE header value
    POLYMARKET_WALLET_ADDRESS — POLY_ADDRESS header value

NOTE: "Invalid api key" usually means the key was not created via the CLOB
credential derivation flow (GET /auth/derive-api-key with an EIP-712 wallet
signature). Website dashboard keys are a different type and will be rejected
by the CLOB trading API. You need your Ethereum wallet private key to derive
real CLOB credentials — see scripts/derive_creds.py once eth-account is added.
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

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("test_live_order")

CLOB_URL = "https://clob.polymarket.com"
GAMMA_URL = "https://gamma-api.polymarket.com"
SERIES_TICKER = "btc-up-or-down-5m"

TEST_PRICE = 0.01
TEST_SIZE = 1.0


# ------------------------------------------------------------------ #
# HMAC-SHA256 L2 signing — matches current py-sdk exactly            #
# ------------------------------------------------------------------ #

def _pad_base64(value: str) -> str:
    """Add missing base64 padding characters."""
    padding = (-len(value)) % 4
    return value + "=" * padding


def _hmac_sig(secret: str, timestamp: int, method: str, path: str, body: str) -> str:
    """
    Matches py-sdk/py_sdk/signing/hmac.py build_hmac_signature:
      message = timestamp + method + path + body   (no quote replacement)
      key     = base64url_decode(padded_secret)
      sig     = base64url_encode(HMAC-SHA256(key, message))
    """
    key = base64.urlsafe_b64decode(_pad_base64(secret))
    message = str(timestamp) + method + path
    if body:
        message += body                        # no .replace("'", '"')
    digest = hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def _auth_headers(creds: dict, method: str, path: str, body: str = "") -> dict[str, str]:
    """
    All 5 POLY_* headers always sent (empty string when value unknown).
    Omitting POLY_ADDRESS or POLY_PASSPHRASE causes generic 'Invalid api key' errors.
    """
    timestamp = int(datetime.now().timestamp())
    sig = _hmac_sig(creds["secret"], timestamp, method, path, body)

    if DEBUG:
        print(f"\n[DEBUG] Signing string : {timestamp!r}{method!r}{path!r}"
              f"{(body[:80] + '…') if len(body) > 80 else body!r}")
        print(f"[DEBUG] Headers (redacted):")
        print(f"  POLY_ADDRESS    : {creds['wallet']!r}")
        print(f"  POLY_API_KEY    : {creds['api_key'][:8]}...")
        print(f"  POLY_PASSPHRASE : {creds['passphrase']!r}")
        print(f"  POLY_SIGNATURE  : {sig[:16]}...  (base64url HMAC-SHA256)")
        print(f"  POLY_TIMESTAMP  : {timestamp}")

    return {
        "POLY_ADDRESS": creds["wallet"],          # empty string if unknown
        "POLY_API_KEY": creds["api_key"],
        "POLY_PASSPHRASE": creds["passphrase"],   # empty string if not issued
        "POLY_SIGNATURE": sig,
        "POLY_TIMESTAMP": str(timestamp),
        "Content-Type": "application/json",
    }


# ------------------------------------------------------------------ #
# Credential loading                                                  #
# ------------------------------------------------------------------ #

def _load_creds() -> dict:
    api_key = os.getenv("POLYMARKET_API_KEY", "")
    secret = os.getenv("POLYMARKET_SECRET") or os.getenv("POLYMARKET_PRIVATE_KEY", "")

    missing = []
    if not api_key:  missing.append("POLYMARKET_API_KEY")
    if not secret:   missing.append("POLYMARKET_SECRET (or POLYMARKET_PRIVATE_KEY)")
    if missing:
        raise RuntimeError(f"Missing .env vars: {', '.join(missing)}")

    passphrase = os.getenv("POLYMARKET_PASSPHRASE", "")
    wallet = os.getenv("POLYMARKET_WALLET_ADDRESS", "")

    log.info("Credentials:")
    log.info("  POLYMARKET_API_KEY       : %s...", api_key[:8])
    log.info("  POLYMARKET_SECRET        : %s... (len=%d)", secret[:8], len(secret))
    log.info("  POLYMARKET_PASSPHRASE    : %s", repr(passphrase) if passphrase else "''" + "  ← not set, sending empty string")
    log.info("  POLYMARKET_WALLET_ADDRESS: %s", wallet or "''" + "  ← not set, sending empty string")

    if api_key.startswith("019") or (len(api_key) > 20 and "-" not in api_key):
        log.warning(
            "API key looks like a dashboard/website key. "
            "The CLOB trading API requires credentials derived from your Ethereum "
            "wallet via GET /auth/derive-api-key (EIP-712 L1 auth). "
            "A website key will return 'Invalid api key'."
        )

    return {"api_key": api_key, "secret": secret, "passphrase": passphrase, "wallet": wallet}


# ------------------------------------------------------------------ #
# Verify signing at GET /time (no auth needed — just checks our      #
# clock matches the server, which affects HMAC freshness)            #
# ------------------------------------------------------------------ #

async def _check_server_time(client: httpx.AsyncClient) -> None:
    try:
        resp = await client.get(f"{CLOB_URL}/time", timeout=5.0)
        server_ts = resp.json().get("time", 0)
        our_ts = int(time.time())
        skew = abs(server_ts - our_ts)
        log.info("Server time: %d  Our time: %d  Skew: %ds", server_ts, our_ts, skew)
        if skew > 30:
            log.warning("Clock skew %ds > 30s — HMAC freshness window may be exceeded", skew)
    except Exception as exc:
        log.warning("Could not check server time: %s", exc)


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
        raise RuntimeError("No active BTC5M markets. Between windows?")
    market = min(candidates, key=lambda m: m["end_date"])
    log.info("Market: %s  end=%s", market["question"], market["end_date"].isoformat())
    return market


# ------------------------------------------------------------------ #
# Dry-run: hit a lightweight authenticated endpoint first             #
# ------------------------------------------------------------------ #

async def _dry_run_auth(client: httpx.AsyncClient, creds: dict) -> None:
    """GET /auth/api-keys — authenticated, read-only. Confirms key+sig work."""
    path = "/auth/api-keys"
    resp = await client.get(
        f"{CLOB_URL}{path}",
        headers=_auth_headers(creds, "GET", path),
        timeout=10.0,
    )
    if resp.status_code == 401:
        raise RuntimeError(
            f"Auth dry-run failed 401: {resp.text[:300]}\n\n"
            "Most likely cause: the API key was not created via the CLOB credential\n"
            "derivation flow. Website dashboard keys are NOT valid for CLOB trading.\n"
            "You need your Ethereum wallet private key. Add it to .env as\n"
            "POLYMARKET_PRIVATE_KEY and run: python scripts/test_live_order.py\n"
            "(once derive_creds support is re-added via eth-account)."
        )
    if resp.status_code == 200:
        log.info("Auth dry-run OK — key recognised. Keys: %s", resp.text[:120])
    else:
        log.warning("Auth dry-run status %d: %s", resp.status_code, resp.text[:120])


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
        headers=_auth_headers(creds, "POST", "/order", body),
        timeout=15.0,
    )
    if not resp.is_success:
        raise RuntimeError(f"POST /order failed {resp.status_code}: {resp.text[:300]}")
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
    creds = _load_creds()
    async with httpx.AsyncClient() as client:
        await _check_server_time(client)
        await _dry_run_auth(client, creds)        # fail fast with clear message
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
