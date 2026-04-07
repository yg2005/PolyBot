from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# Populated by calling code (KalBot) via update_* methods.
# Dashboard reads this singleton; no DB required for live view.


@dataclass
class FeedHealth:
    chainlink_ok: bool = False
    chainlink_last_update: datetime | None = None
    chainlink_stale: bool = False
    spot_ok: bool = False
    spot_last_update: datetime | None = None
    spot_source: str = "none"
    polymarket_ok: bool = False
    polymarket_markets: int = 0


@dataclass
class WindowState:
    market_id: str = ""
    question: str = ""
    elapsed_seconds: int = 0
    remaining_seconds: int = 0
    strike_price: float = 0.0
    open_price: float = 0.0
    current_price: float = 0.0
    displacement_pct: float = 0.0
    direction: int = 0
    direction_consistency: float = 0.0
    yes_bid: float = 0.0
    yes_ask: float = 0.0
    mid_price: float = 0.0
    spread: float = 0.0
    signal: str = "PASS"
    model_prob: float | None = None
    traded: bool = False


@dataclass
class PositionState:
    window_id: str
    side: str
    entry_price: float
    size_usd: float
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class BotMetrics:
    # Live state
    feed_health: FeedHealth = field(default_factory=FeedHealth)
    window: WindowState = field(default_factory=WindowState)
    active_positions: list[PositionState] = field(default_factory=list)

    # Session stats
    today_pnl: float = 0.0
    today_trades: int = 0
    today_wins: int = 0
    today_losses: int = 0
    session_windows: int = 0  # windows seen since bot start
    settled_windows: int = 0  # settled windows in DB (for ML progress)

    # Model state
    model_active: bool = False
    model_id: str | None = None
    model_auc: float | None = None
    scorer_mode: str = "rules"

    # Circuit breaker
    circuit_breaker_active: bool = False
    circuit_breaker_reason: str = ""

    # Data collection
    windows_collected: int = 0  # settled windows total
    windows_target: int = 500

    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class MetricsCollector:
    """In-memory metrics store. Updated by KalBot; read by the dashboard."""

    def __init__(self) -> None:
        self._m = BotMetrics()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Feed health                                                          #
    # ------------------------------------------------------------------ #

    async def update_chainlink(
        self, ok: bool, stale: bool, last_update: datetime | None
    ) -> None:
        async with self._lock:
            self._m.feed_health.chainlink_ok = ok
            self._m.feed_health.chainlink_stale = stale
            self._m.feed_health.chainlink_last_update = last_update
            self._m.updated_at = datetime.now(timezone.utc)

    async def update_spot(
        self, ok: bool, source: str, last_update: datetime | None
    ) -> None:
        async with self._lock:
            self._m.feed_health.spot_ok = ok
            self._m.feed_health.spot_source = source
            self._m.feed_health.spot_last_update = last_update
            self._m.updated_at = datetime.now(timezone.utc)

    async def update_polymarket(self, ok: bool, market_count: int) -> None:
        async with self._lock:
            self._m.feed_health.polymarket_ok = ok
            self._m.feed_health.polymarket_markets = market_count
            self._m.updated_at = datetime.now(timezone.utc)

    # ------------------------------------------------------------------ #
    # Window state                                                         #
    # ------------------------------------------------------------------ #

    async def update_window(self, **kwargs: Any) -> None:
        async with self._lock:
            for k, v in kwargs.items():
                if hasattr(self._m.window, k):
                    setattr(self._m.window, k, v)
            self._m.updated_at = datetime.now(timezone.utc)

    async def increment_session_windows(self) -> None:
        async with self._lock:
            self._m.session_windows += 1
            self._m.updated_at = datetime.now(timezone.utc)

    # ------------------------------------------------------------------ #
    # Position tracking                                                    #
    # ------------------------------------------------------------------ #

    async def open_position(
        self, window_id: str, side: str, entry_price: float, size_usd: float
    ) -> None:
        async with self._lock:
            self._m.active_positions.append(
                PositionState(
                    window_id=window_id,
                    side=side,
                    entry_price=entry_price,
                    size_usd=size_usd,
                )
            )
            self._m.today_trades += 1
            self._m.updated_at = datetime.now(timezone.utc)

    async def close_position(self, window_id: str, pnl: float) -> None:
        async with self._lock:
            self._m.active_positions = [
                p for p in self._m.active_positions if p.window_id != window_id
            ]
            self._m.today_pnl += pnl
            if pnl > 0:
                self._m.today_wins += 1
            elif pnl < 0:
                self._m.today_losses += 1
            self._m.updated_at = datetime.now(timezone.utc)

    # ------------------------------------------------------------------ #
    # Circuit breaker                                                      #
    # ------------------------------------------------------------------ #

    async def update_circuit_breaker(self, active: bool, reason: str = "") -> None:
        async with self._lock:
            self._m.circuit_breaker_active = active
            self._m.circuit_breaker_reason = reason
            self._m.updated_at = datetime.now(timezone.utc)

    # ------------------------------------------------------------------ #
    # Model / data collection                                              #
    # ------------------------------------------------------------------ #

    async def update_model(
        self,
        active: bool,
        model_id: str | None,
        auc: float | None,
        scorer_mode: str,
    ) -> None:
        async with self._lock:
            self._m.model_active = active
            self._m.model_id = model_id
            self._m.model_auc = auc
            self._m.scorer_mode = scorer_mode
            self._m.updated_at = datetime.now(timezone.utc)

    async def update_windows_collected(self, count: int) -> None:
        async with self._lock:
            self._m.windows_collected = count
            self._m.updated_at = datetime.now(timezone.utc)

    # ------------------------------------------------------------------ #
    # Read                                                                 #
    # ------------------------------------------------------------------ #

    def snapshot(self) -> BotMetrics:
        """Return current metrics. Caller should not mutate."""
        return self._m
