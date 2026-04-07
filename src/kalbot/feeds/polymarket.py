from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)


@dataclass
class MarketInfo:
    market_id: str
    question: str
    condition_id: str
    end_date: datetime
    yes_token_id: str
    no_token_id: str
    maker_base_fee: float = 0.0
    restricted: bool = False


@dataclass
class OrderbookSnapshot:
    market_id: str
    yes_token_id: str
    mid_price: float        # YES mid
    spread: float
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    bid_depth_usd: float
    ask_depth_usd: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class PolymarketClient:
    def __init__(
        self,
        gamma_url: str = "https://gamma-api.polymarket.com",
        clob_url: str = "https://clob.polymarket.com",
        discovery_interval_s: int = 30,
        series_ticker: str = "POLYBTC5M",
    ) -> None:
        self._gamma_url = gamma_url
        self._clob_url = clob_url
        self._discovery_interval_s = discovery_interval_s
        self._series_ticker = series_ticker

        self._active_markets: dict[str, MarketInfo] = {}
        self._running = False
        self._task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._discovery_loop(), name="PolymarketDiscovery")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------ #
    # Public accessors                                                     #
    # ------------------------------------------------------------------ #

    @property
    def active_markets(self) -> dict[str, MarketInfo]:
        return dict(self._active_markets)

    def get_nearest_market(self) -> MarketInfo | None:
        if not self._active_markets:
            return None
        return min(self._active_markets.values(), key=lambda m: m.end_date)

    # ------------------------------------------------------------------ #
    # Discovery loop                                                       #
    # ------------------------------------------------------------------ #

    async def _discovery_loop(self) -> None:
        backoff = 1.0
        async with httpx.AsyncClient(timeout=10.0) as client:
            self._client = client
            while self._running:
                try:
                    markets = await self._discover_markets(client)
                    self._active_markets = {m.market_id: m for m in markets}
                    if markets:
                        log.info(
                            "PolymarketClient discovered %d active BTC5M market(s): %s",
                            len(markets),
                            [m.question for m in markets],
                        )
                    backoff = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("PolymarketClient discovery error: %s — retry in %.0fs", exc, backoff)
                    backoff = min(backoff * 2, 30.0)

                await asyncio.sleep(self._discovery_interval_s)
        self._client = None

    async def _discover_markets(self, client: httpx.AsyncClient) -> list[MarketInfo]:
        now = datetime.now(timezone.utc)
        end_max = now + timedelta(minutes=15)

        params: dict[str, Any] = {
            "active": "true",
            "closed": "false",
            "limit": 500,
            "order": "endDate",
            "ascending": "true",
            "end_date_min": now.isoformat(),
            "end_date_max": end_max.isoformat(),
        }
        resp = await client.get(f"{self._gamma_url}/markets", params=params)
        resp.raise_for_status()
        data = resp.json()

        markets: list[MarketInfo] = []
        items = data if isinstance(data, list) else data.get("markets", [])
        for item in items:
            if not self._is_btc5m_market(item):
                continue
            info = self._parse_market(item)
            if info is not None:
                markets.append(info)
        return markets

    def _is_btc5m_market(self, item: dict) -> bool:
        # Primary: check nested events[].series[].ticker — most reliable
        for event in item.get("events", []):
            for s in event.get("series", []):
                if s.get("ticker") == self._series_ticker:
                    return True

        # Fallback: top-level series/ticker fields
        series: str = item.get("series_ticker") or item.get("seriesTicker") or ""
        ticker: str = item.get("ticker") or ""
        if series == self._series_ticker or ticker == self._series_ticker:
            return True

        return False

    def _parse_market(self, item: dict) -> MarketInfo | None:
        try:
            raw = item.get("clobTokenIds") or item.get("clob_token_ids") or []
            token_ids: list[str] = json.loads(raw) if isinstance(raw, str) else raw
            if len(token_ids) < 2:
                return None
            end_raw = item.get("endDate") or item.get("end_date_utc") or item.get("end_date") or ""
            end_date = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
            return MarketInfo(
                market_id=str(item.get("id") or item.get("conditionId") or ""),
                question=item.get("question") or item.get("title") or "",
                condition_id=str(item.get("conditionId") or ""),
                end_date=end_date,
                yes_token_id=str(token_ids[0]),
                no_token_id=str(token_ids[1]),
                maker_base_fee=float(item.get("makerBaseFee") or 0.0),
                restricted=bool(item.get("restricted", False)),
            )
        except Exception as exc:
            log.warning("PolymarketClient failed to parse market: %s — %s", exc, item)
            return None

    # ------------------------------------------------------------------ #
    # Orderbook                                                            #
    # ------------------------------------------------------------------ #

    async def fetch_orderbook(self, market: MarketInfo) -> OrderbookSnapshot | None:
        if self._client is None:
            return None
        try:
            return await self._fetch_orderbook(self._client, market)
        except Exception as exc:
            log.warning("PolymarketClient orderbook fetch failed for %s: %s", market.market_id, exc)
            return None

    async def _fetch_orderbook(
        self, client: httpx.AsyncClient, market: MarketInfo
    ) -> OrderbookSnapshot:
        yes_id = market.yes_token_id

        mid_resp, spread_resp, buy_resp, book_resp = await asyncio.gather(
            client.get(f"{self._clob_url}/midpoint", params={"token_id": yes_id}),
            client.get(f"{self._clob_url}/spread", params={"token_id": yes_id}),
            client.get(f"{self._clob_url}/price", params={"token_id": yes_id, "side": "buy"}),
            client.get(f"{self._clob_url}/book", params={"token_id": yes_id}),
        )

        mid = float(mid_resp.json().get("mid", 0.0))
        spread = float(spread_resp.json().get("spread", 0.0))
        yes_ask = float(buy_resp.json().get("price", 0.0))
        yes_bid = round(mid - spread / 2, 4)
        no_bid = round(1.0 - yes_ask, 4)
        no_ask = round(1.0 - yes_bid, 4)

        book = book_resp.json()
        bid_depth = sum(float(lvl.get("size", 0)) * float(lvl.get("price", 0))
                        for lvl in book.get("bids", []))
        ask_depth = sum(float(lvl.get("size", 0)) * float(lvl.get("price", 0))
                        for lvl in book.get("asks", []))

        snap = OrderbookSnapshot(
            market_id=market.market_id,
            yes_token_id=yes_id,
            mid_price=mid,
            spread=spread,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            bid_depth_usd=bid_depth,
            ask_depth_usd=ask_depth,
        )
        log.info(
            "orderbook market=%s yes_bid=%.4f yes_ask=%.4f spread=%.4f",
            market.question[:40],
            yes_bid,
            yes_ask,
            spread,
        )
        return snap
