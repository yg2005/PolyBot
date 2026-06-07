"""
Polymarket CLOB API authentication.

L1 (EIP-712): sign with Ethereum private key → GET /auth/derive-api-key
              → returns (api_key, api_secret, api_passphrase)

L2 (HMAC-SHA256): sign every trading request with the derived credentials.

Signing matches py-sdk (github.com/Polymarket/py-sdk):
  L1 struct: ClobAuth with address type "address" (not "string")
  L2 message: timestamp + METHOD + path + body  (no quote replacement)
  L2 secret:  base64url-decode with padding fix before use
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime

POLY_ADDRESS    = "POLY_ADDRESS"
POLY_SIGNATURE  = "POLY_SIGNATURE"
POLY_TIMESTAMP  = "POLY_TIMESTAMP"
POLY_NONCE      = "POLY_NONCE"
POLY_API_KEY    = "POLY_API_KEY"
POLY_PASSPHRASE = "POLY_PASSPHRASE"

_POLYGON_CHAIN_ID = 137
_CLOB_AUTH_MSG    = "This message attests that I control the given wallet"


# ------------------------------------------------------------------ #
# L2 — HMAC-SHA256 request signing                                    #
# ------------------------------------------------------------------ #

def _pad_b64(value: str) -> str:
    return value + "=" * ((-len(value)) % 4)


def _hmac_sig(secret: str, timestamp: int, method: str, path: str, body: str) -> str:
    """Matches py-sdk build_hmac_signature exactly."""
    key = base64.urlsafe_b64decode(_pad_b64(secret))
    message = str(timestamp) + method + path
    if body:
        message += body
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
    """All 5 POLY_* headers always sent — omitting any causes generic 401s."""
    timestamp = int(datetime.now().timestamp())
    return {
        POLY_ADDRESS:    wallet_address,
        POLY_API_KEY:    api_key,
        POLY_PASSPHRASE: api_passphrase,
        POLY_SIGNATURE:  _hmac_sig(api_secret, timestamp, method, path, body),
        POLY_TIMESTAMP:  str(timestamp),
        "Content-Type":  "application/json",
    }


# ------------------------------------------------------------------ #
# L1 — EIP-712 signing for credential derivation                     #
# ------------------------------------------------------------------ #

def wallet_address_from_key(private_key: str) -> str:
    from eth_account import Account
    return Account.from_key(private_key).address


def _eip712_typed_data(address: str, timestamp: int, nonce: int, chain_id: int) -> dict:
    """Build the ClobAuth EIP-712 struct matching py-sdk exactly.

    Critical: address field type is "address" (not "string" as in old py-clob-client).
    """
    return {
        "domain": {
            "name":    "ClobAuthDomain",
            "version": "1",
            "chainId": chain_id,
        },
        "types": {
            "EIP712Domain": [
                {"name": "name",    "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "ClobAuth": [
                {"name": "address",   "type": "address"},   # "address" not "string"
                {"name": "timestamp", "type": "string"},
                {"name": "nonce",     "type": "uint256"},
                {"name": "message",   "type": "string"},
            ],
        },
        "primaryType": "ClobAuth",
        "message": {
            "address":   address,
            "timestamp": str(timestamp),
            "nonce":     nonce,
            "message":   _CLOB_AUTH_MSG,
        },
    }


def build_l1_headers(
    private_key: str,
    chain_id: int = _POLYGON_CHAIN_ID,
    nonce: int = 0,
) -> dict[str, str]:
    """Level 1 auth headers signed with the Ethereum private key."""
    from eth_account import Account

    account   = Account.from_key(private_key)
    timestamp = int(datetime.now().timestamp())
    data      = _eip712_typed_data(account.address, timestamp, nonce, chain_id)
    signed    = account.sign_typed_data(full_message=data)
    return {
        POLY_ADDRESS:   account.address,
        POLY_SIGNATURE: "0x" + signed.signature.hex(),
        POLY_TIMESTAMP: str(timestamp),
        POLY_NONCE:     str(nonce),
    }


async def derive_api_creds(
    private_key: str,
    clob_url: str = "https://clob.polymarket.com",
    chain_id: int = _POLYGON_CHAIN_ID,
    nonce: int = 0,
) -> tuple[str, str, str]:
    """Derive api_key + api_secret + api_passphrase from an Ethereum private key.

    Calls GET /auth/derive-api-key with Level 1 (EIP-712) auth headers.
    Returns (api_key, api_secret, api_passphrase).
    """
    import httpx

    headers = build_l1_headers(private_key, chain_id, nonce)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{clob_url}/auth/derive-api-key", headers=headers)
        resp.raise_for_status()
        data = resp.json()

    api_key    = data.get("apiKey")    or data.get("api_key", "")
    secret     = data.get("secret",    "")
    passphrase = data.get("passphrase","")
    if not api_key or not secret or not passphrase:
        raise RuntimeError(f"Unexpected /auth/derive-api-key response: {data}")
    return api_key, secret, passphrase
