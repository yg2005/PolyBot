from __future__ import annotations

import asyncio
import logging
import socket
from collections import deque
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import Any

import httpx

from .base import BaseFeed, FeedStatus, PriceUpdate

log = logging.getLogger(__name__)

_COINBASE_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
_KRAKEN_URL = "https://api.kraken.com/0/public/Ticker?pair=XXBTZUSD"

# Rolling window: 120 samples @ 2s = 240s ≈ 4 min, enough for 60s trend
_RING_SIZE = 120


class SpotFeed(BaseFeed):
    def __init__(
        self,
        poll_interval_s: float = 2.0,
        on_price: Callable[[PriceUpdate], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        super().__init__(stale_threshold_s=30)
        self._poll_interval_s = poll_interval_s
        self._on_price = on_price
        self._ring: deque[tuple[datetime, float]] = deque(maxlen=_RING_SIZE)
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ #
    # Feed interface                                                       #
    # ------------------------------------------------------------------ #

    async def connect(self) -> None:
        async with httpx.AsyncClient(timeout=5.0) as client:
            self._client = client
            self._status = FeedStatus.CONNECTED
            log.info("SpotFeed polling started")
            backoff = 1.0
            while self._running:
                try:
                    update = await self._poll(client)
                    if update is not None:
                        self._last_update_time = update.timestamp
                        self._status = FeedStatus.CONNECTED
                        self._ring.append((update.timestamp, update.price))
                        await self.on_update(update)
                        if self._on_price is not None:
                            await self._on_price(update)
                        backoff = 1.0
                    await asyncio.sleep(self._poll_interval_s)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._status = FeedStatus.STALE
                    log.warning("SpotFeed poll error: %s — retry in %.0fs", exc, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
        self._client = None

    async def disconnect(self) -> None:
        self._client = None  # AsyncClient closed by context manager in connect()

    async def on_update(self, update: PriceUpdate) -> None:
        log.info("spot price=%.2f source=%s trend_1m=%+.4f",
                 update.price, update.source, self.trend_1m(update.price, update.timestamp))

    # ------------------------------------------------------------------ #
    # Trend calculation                                                    #
    # ------------------------------------------------------------------ #

    def trend_1m(self, price_now: float, now: datetime) -> float:
        """Return (price_now - price_60s_ago) / price_60s_ago, wall-clock anchored."""
        cutoff = now.timestamp() - 60.0
        # Walk newest→oldest, find first sample older than cutoff
        anchor: float | None = None
        for ts, px in reversed(self._ring):
            if ts.timestamp() <= cutoff:
                anchor = px
                break
        if anchor is None or anchor == 0.0:
            return 0.0
        return (price_now - anchor) / anchor

    # ------------------------------------------------------------------ #
    # Internal polling                                                     #
    # ------------------------------------------------------------------ #

    async def _poll(self, client: httpx.AsyncClient) -> PriceUpdate | None:
        try:
            result = await self._fetch_coinbase(client)
            self._network_error = False
            return result
        except socket.gaierror as exc:
            log.error("SpotFeed Coinbase DNS failure: %s", exc)
        except Exception as exc:
            log.warning("SpotFeed Coinbase failed: %s — trying Kraken", exc)

        try:
            result = await self._fetch_kraken(client)
            self._network_error = False
            return result
        except socket.gaierror as exc:
            # Both sources have DNS failure — raise so coordinator sees gaierror
            self._network_error = True
            log.error("SpotFeed Kraken DNS failure: %s", exc)
            raise
        except Exception as exc:
            log.warning("SpotFeed Kraken failed: %s", exc)
            return None

    async def _fetch_coinbase(self, client: httpx.AsyncClient) -> PriceUpdate:
        resp = await client.get(_COINBASE_URL)
        resp.raise_for_status()
        data = resp.json()
        price = float(data["data"]["amount"])
        return PriceUpdate(price=price, timestamp=datetime.now(timezone.utc), source="coinbase")

    async def _fetch_kraken(self, client: httpx.AsyncClient) -> PriceUpdate:
        resp = await client.get(_KRAKEN_URL)
        resp.raise_for_status()
        data = resp.json()
        # Kraken: result.XXBTZUSD.c[0] = last trade price
        price = float(data["result"]["XXBTZUSD"]["c"][0])
        return PriceUpdate(price=price, timestamp=datetime.now(timezone.utc), source="kraken")
