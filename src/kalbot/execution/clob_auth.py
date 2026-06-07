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


def _hmac_sig(secret: str, timestamp: int, method: str, path: str, body: str) -> str:
    """Matches py-clob-client build_hmac_signature byte-for-byte."""
    key = base64.urlsafe_b64decode(secret)
    # Single-quote → double-quote: required for Go/TS/Py hash parity
    message = str(timestamp) + method + path + body.replace("'", '"')
    digest = hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8")


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
    headers: dict[str, str] = {
        POLY_SIGNATURE: _hmac_sig(api_secret, timestamp, method, path, body),
        POLY_TIMESTAMP: str(timestamp),
        POLY_API_KEY: api_key,
        "Content-Type": "application/json",
    }
    # Include optional headers only when values are present
    if wallet_address:
        headers[POLY_ADDRESS] = wallet_address
    if api_passphrase:
        headers[POLY_PASSPHRASE] = api_passphrase
    return headers
