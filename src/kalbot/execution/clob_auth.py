"""
Polymarket CLOB API — Level 2 HMAC-SHA256 request signing.

Credentials (all from .env):
    POLYMARKET_API_KEY      — API key
    POLYMARKET_SECRET       — base64url-encoded HMAC secret
                              (also accepted as POLYMARKET_PRIVATE_KEY for legacy)
    POLYMARKET_PASSPHRASE   — optional; send as POLY_PASSPHRASE header (empty if not issued)
    POLYMARKET_WALLET_ADDRESS — optional; send as POLY_ADDRESS header (empty if unknown)

Signing matches py-clob-client/py_clob_client/signing/hmac.py exactly:
    key     = base64url_decode(secret)
    message = timestamp + METHOD + path + body.replace("'", '"')
    sig     = base64url_encode(HMAC-SHA256(key, message))
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime

POLY_ADDRESS = "POLY_ADDRESS"
POLY_SIGNATURE = "POLY_SIGNATURE"
POLY_TIMESTAMP = "POLY_TIMESTAMP"
POLY_API_KEY = "POLY_API_KEY"
POLY_PASSPHRASE = "POLY_PASSPHRASE"


def _pad_base64(value: str) -> str:
    """Add missing base64 padding (matches py-sdk _pad_base64)."""
    return value + "=" * ((-len(value)) % 4)


def _hmac_sig(secret: str, timestamp: int, method: str, path: str, body: str) -> str:
    """Matches py-sdk/py_sdk/signing/hmac.py build_hmac_signature exactly."""
    key = base64.urlsafe_b64decode(_pad_base64(secret))
    message = str(timestamp) + method + path
    if body:
        message += body                    # no quote replacement — matches new SDK
    digest = hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def build_clob_headers(
    *,
    api_key: str,
    api_secret: str,
    method: str,
    path: str,
    body: str = "",
    api_passphrase: str = "",
    wallet_address: str = "",
) -> dict[str, str]:
    """Return Level 2 auth headers for a CLOB API request.

    api_passphrase and wallet_address are optional — sent as empty strings
    if not available, which may or may not be accepted by the server.
    """
    timestamp = int(datetime.now().timestamp())
    # All 5 POLY_* headers are always sent; omitting POLY_ADDRESS or
    # POLY_PASSPHRASE causes generic "Invalid api key" 401s from the server.
    return {
        POLY_ADDRESS: wallet_address,
        POLY_API_KEY: api_key,
        POLY_PASSPHRASE: api_passphrase,
        POLY_SIGNATURE: _hmac_sig(api_secret, timestamp, method, path, body),
        POLY_TIMESTAMP: str(timestamp),
        "Content-Type": "application/json",
    }
