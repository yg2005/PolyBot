from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import Any

import httpx

from .base import BaseFeed, FeedStatus, PriceUpdate

log = logging.getLogger(__name__)

_COINBASE_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"

# ── Option B placeholder ─────────────────────────────────────────────────────
# To use on-chain Chainlink oracle instead of Coinbase REST, set
# feeds.chainlink_source = "onchain_rpc" and implement _connect_onchain()
# calling latestRoundData() on 0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88b
# via public RPC (e.g. https://eth.llamarpc.com).
# ─────────────────────────────────────────────────────────────────────────────

# ── Dead branch: RTDS WebSocket (wss://ws-live-data.polymarket.com) ──────────
# Connects successfully (AWS API Gateway) but streams zero messages.
# Subscribe format research (2026-04-06):
#   {"type":"subscribe","topic":"crypto_prices_chainlink",...} → silent
#   {"action":"subscribe",...} → {"message":"Invalid request body"} (routes
#     to Lambda handler but payload schema is undocumented / private)
# Keeping code below for when/if Polymarket publishes the correct payload.
#
# _SUBSCRIBE_MSG = json.dumps({
#     "action": "subscribe",
#     # TODO: find required payload fields from Polymarket internal spec
# })
#
# async def _connect_ws(self) -> None:
#     import websockets, websockets.exceptions
#     async with websockets.connect(
#         self._ws_url, ping_interval=self._ping_interval_s, ping_timeout=10,
#     ) as ws:
#         self._status = FeedStatus.CONNECTED
#         log.info("ChainlinkFeed WS connected to %s", self._ws_url)
#         await ws.send(_SUBSCRIBE_MSG)
#         async for raw in ws:
#             if not self._running:
#                 break
#             await self._handle_ws_message(raw)
#
# async def _handle_ws_message(self, raw: str | bytes) -> None:
#     import json
#     try:
#         payload = json.loads(raw)
#     except Exception:
#         return
#     if payload.get("type") != "crypto_price_chainlink":
#         return
#     if payload.get("symbol") != "btc/usd":
#         return
#     try:
#         price = float(payload["price"])
#     except (KeyError, ValueError, TypeError):
#         log.warning("ChainlinkFeed bad WS payload: %s", payload)
#         return
#     await self._emit(price)
# ─────────────────────────────────────────────────────────────────────────────


class ChainlinkFeed(BaseFeed):
    _BACKOFF_BASE: float = 3.0

    def __init__(
        self,
        ws_url: str,
        ping_interval_s: int = 5,
        stale_threshold_s: int = 10,
        on_price: Callable[[PriceUpdate], Coroutine[Any, Any, None]] | None = None,
        source: str = "coinbase_rest",
        poll_interval_s: float = 1.0,
    ) -> None:
        super().__init__(stale_threshold_s=stale_threshold_s)
        self._ws_url = ws_url            # kept for future Option B / WS revival
        self._ping_interval_s = ping_interval_s
        self._on_price = on_price
        self._source = source
        self._poll_interval_s = poll_interval_s

    # ------------------------------------------------------------------ #
    # BaseFeed interface                                                   #
    # ------------------------------------------------------------------ #

    async def connect(self) -> None:
        if self._source == "coinbase_rest":
            await self._connect_rest()
        else:
            raise ValueError(f"Unknown chainlink_source: {self._source!r}")

    async def disconnect(self) -> None:
        pass  # REST: no persistent connection to close

    async def on_update(self, update: PriceUpdate) -> None:
        log.info("chainlink price=%.2f source=%s", update.price, update.source)

    # ------------------------------------------------------------------ #
    # REST polling (Option A)                                             #
    # ------------------------------------------------------------------ #

    async def _connect_rest(self) -> None:
        log.info("ChainlinkFeed starting REST polling (source=coinbase_rest, interval=%.1fs)",
                 self._poll_interval_s)
        self._status = FeedStatus.CONNECTED
        backoff = 1.0
        async with httpx.AsyncClient(timeout=4.0) as client:
            while self._running:
                try:
                    resp = await client.get(_COINBASE_URL)
                    resp.raise_for_status()
                    price = float(resp.json()["data"]["amount"])
                    self._network_error = False
                    backoff = 1.0
                    await self._emit(price)
                    await asyncio.sleep(self._poll_interval_s)
                except asyncio.CancelledError:
                    raise
                except socket.gaierror as exc:
                    self._network_error = True
                    self._status = FeedStatus.STALE
                    log.error("ChainlinkFeed DNS failure: %s — retry in %.0fs", exc, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self._BACKOFF_MAX)
                except Exception as exc:
                    self._status = FeedStatus.STALE
                    log.warning("ChainlinkFeed poll error: %s — retry in %.0fs", exc, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self._BACKOFF_MAX)

    # ------------------------------------------------------------------ #
    # Internal                                                            #
    # ------------------------------------------------------------------ #

    async def _emit(self, price: float) -> None:
        now = datetime.now(timezone.utc)
        self._last_update_time = now
        self._status = FeedStatus.CONNECTED
        update = PriceUpdate(price=price, timestamp=now, source="chainlink")
        await self.on_update(update)
        if self._on_price is not None:
            await self._on_price(update)
