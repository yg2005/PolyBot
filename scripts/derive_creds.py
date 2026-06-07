"""
One-time script: recover your Polymarket L2 API credentials from your private key.

Usage:
    python scripts/derive_creds.py

Requires in .env:
    POLYMARKET_PRIVATE_KEY   — Ethereum wallet private key

Prints the three lines to add to .env so future runs skip derivation.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv

load_dotenv()


async def main() -> None:
    from kalbot.execution.clob_auth import derive_api_creds, wallet_address_from_key

    private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    if not private_key:
        print("ERROR: POLYMARKET_PRIVATE_KEY not set in .env", file=sys.stderr)
        sys.exit(1)

    address = wallet_address_from_key(private_key)
    print(f"Wallet address : {address}")
    print("Calling /auth/derive-api-key ...")

    api_key, secret, passphrase = await derive_api_creds(private_key)

    print(f"\nSuccess! Add these to your .env:\n")
    print(f"POLYMARKET_API_KEY={api_key}")
    print(f"POLYMARKET_SECRET={secret}")
    print(f"POLYMARKET_PASSPHRASE={passphrase}")
    print(f"POLYMARKET_WALLET_ADDRESS={address}")


if __name__ == "__main__":
    asyncio.run(main())
