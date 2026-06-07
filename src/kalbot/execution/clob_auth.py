"""
Polymarket CLOB API authentication.

L1 (EIP-712): used to derive/recover L2 API credentials from a private key.
L2 (HMAC-SHA256): used for all trading requests (orders, cancels, etc.).

Signing matches py-clob-client exactly:
  signing/eip712.py  — L1 EIP-712 struct signing
  signing/hmac.py    — L2 HMAC-SHA256 request signing
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# L2 header names (Polymarket CLOB convention)
POLY_ADDRESS = "POLY_ADDRESS"
POLY_SIGNATURE = "POLY_SIGNATURE"
POLY_TIMESTAMP = "POLY_TIMESTAMP"
POLY_NONCE = "POLY_NONCE"
POLY_API_KEY = "POLY_API_KEY"
POLY_PASSPHRASE = "POLY_PASSPHRASE"

_POLYGON_CHAIN_ID = 137
_MSG_TO_SIGN = "This message attests that I control the given wallet"


# ------------------------------------------------------------------ #
# L2 — HMAC-SHA256 request signing                                    #
# ------------------------------------------------------------------ #

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


# ------------------------------------------------------------------ #
# L1 — EIP-712 struct signing (for credential derivation)             #
# ------------------------------------------------------------------ #

def wallet_address_from_key(private_key: str) -> str:
    from eth_account import Account
    return Account.from_key(private_key).address


def _sign_clob_auth(
    private_key: str, timestamp: int, nonce: int, chain_id: int
) -> str:
    """Sign the ClobAuth EIP-712 struct. Returns 0x-prefixed hex signature."""
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    address = Account.from_key(private_key).address
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
            "address": address,
            "timestamp": str(timestamp),
            "nonce": nonce,
            "message": _MSG_TO_SIGN,
        },
    }
    msg = encode_typed_data(full_message=structured)
    signed = Account.sign_message(msg, private_key)
    return "0x" + signed.signature.hex()


def build_l1_headers(
    private_key: str,
    chain_id: int = _POLYGON_CHAIN_ID,
    nonce: int = 0,
) -> dict[str, str]:
    """Return Level 1 auth headers (EIP-712 signed)."""
    from eth_account import Account

    timestamp = int(datetime.now().timestamp())
    address = Account.from_key(private_key).address
    signature = _sign_clob_auth(private_key, timestamp, nonce, chain_id)
    return {
        POLY_ADDRESS: address,
        POLY_SIGNATURE: signature,
        POLY_TIMESTAMP: str(timestamp),
        POLY_NONCE: str(nonce),
    }


async def derive_api_creds(
    private_key: str,
    clob_url: str = "https://clob.polymarket.com",
    chain_id: int = _POLYGON_CHAIN_ID,
    nonce: int = 0,
) -> tuple[str, str, str]:
    """Recover the api_key, api_secret, api_passphrase tied to this wallet.

    Uses GET /auth/derive-api-key with L1 (EIP-712) auth.
    Returns (api_key, api_secret, api_passphrase).
    """
    import httpx

    headers = build_l1_headers(private_key, chain_id, nonce)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{clob_url}/auth/derive-api-key", headers=headers)
        resp.raise_for_status()
        data = resp.json()

    api_key = data.get("apiKey") or data.get("api_key", "")
    secret = data.get("secret", "")
    passphrase = data.get("passphrase", "")
    if not api_key or not secret or not passphrase:
        raise RuntimeError(f"Unexpected derive-api-key response: {data}")
    return api_key, secret, passphrase
