"""
Polymarket CLOB API — Level 2 authentication (HMAC-SHA256).

Mirrors py-clob-client/py_clob_client/signing/hmac.py and
py_clob_client/headers/headers.py exactly so signatures are
byte-for-byte identical.
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
    """Compute HMAC-SHA256 matching py-clob-client's build_hmac_signature."""
    key = base64.urlsafe_b64decode(secret)
    # Body single-quote → double-quote: required for Go/TS/Py parity
    message = str(timestamp) + method + path + body.replace("'", '"')
    digest = hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8")


def build_clob_headers(
    *,
    api_key: str,
    api_secret: str,
    api_passphrase: str,
    wallet_address: str,
    method: str,
    path: str,
    body: str = "",
) -> dict[str, str]:
    """Return Level 2 auth headers for a CLOB API request."""
    timestamp = int(datetime.now().timestamp())
    return {
        POLY_ADDRESS: wallet_address,
        POLY_SIGNATURE: _hmac_sig(api_secret, timestamp, method, path, body),
        POLY_TIMESTAMP: str(timestamp),
        POLY_API_KEY: api_key,
        POLY_PASSPHRASE: api_passphrase,
        "Content-Type": "application/json",
    }
