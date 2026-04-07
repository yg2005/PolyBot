from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger(__name__)

ELAPSED_HARD_LIMIT = 330  # seconds — critical bug prevention


@dataclass
class WindowFeatures:
    btc_move_since_open: float      # raw $ delta
    btc_move_pct: float             # (current - open) / open * 100
    displacement_pct: float         # same as btc_move_pct
    abs_displacement_pct: float
    direction: int                  # 1, -1, or 0
    direction_consistency: float    # last 20 samples
    cross_count: int
    time_above_pct: float           # sample-based
    max_displacement_pct: float
    min_displacement_pct: float
    distance_from_low: float
    velocity: float                 # displacement_pct / elapsed_seconds
    acceleration: float             # (vel_now - vel_30s_ago) / 30
    elapsed_seconds: float
    momentum_slope_1min: float      # linear regression slope of last 60 prices


class WindowTracker:
    """Tracks intra-window trajectory features. reset() MUST be called on every new window."""

    def __init__(self) -> None:
        self._open_price: float | None = None
        self._open_detected_at: datetime | None = None
        self._price_history: deque[tuple[datetime, float]] = deque(maxlen=300)
        self._cross_events: int = 0
        self._max_price: float | None = None
        self._min_price: float | None = None
        self._last_side: int | None = None  # 1 above, -1 below

    def reset(self, open_price: float, open_time: datetime) -> None:
        self._open_price = open_price
        self._open_detected_at = open_time
        self._price_history.clear()
        self._cross_events = 0
        self._max_price = open_price
        self._min_price = open_price
        self._last_side = None
        log.info("WindowTracker reset | open=%.2f at %s", open_price, open_time.isoformat())

    def update(self, price: float, ts: datetime | None = None) -> None:
        if self._open_price is None:
            return
        if ts is None:
            ts = datetime.now(timezone.utc)
        self._price_history.append((ts, price))
        if price > self._max_price:
            self._max_price = price
        if price < self._min_price:
            self._min_price = price
        side = 1 if price > self._open_price else (-1 if price < self._open_price else 0)
        if side != 0 and self._last_side is not None and self._last_side != 0 and side != self._last_side:
            self._cross_events += 1
        if side != 0:
            self._last_side = side

    def get_features(self, current_price: float) -> WindowFeatures | None:
        if self._open_price is None or self._open_detected_at is None:
            return None
        if len(self._price_history) < 2:
            return None

        now = datetime.now(timezone.utc)
        elapsed = (now - self._open_detected_at).total_seconds()

        # RUNTIME ASSERTION: block trading if elapsed exceeds hard limit, continue logging
        if elapsed > ELAPSED_HARD_LIMIT:
            log.critical(
                "elapsed_seconds=%.1f > %d — CRITICAL BUG GUARD triggered. Trading blocked.",
                elapsed,
                ELAPSED_HARD_LIMIT,
            )
            return None

        open_price = self._open_price
        btc_move = current_price - open_price
        btc_move_pct = (btc_move / open_price) * 100.0

        direction = 1 if btc_move > 0 else (-1 if btc_move < 0 else 0)

        # direction_consistency: last 20 samples
        last20 = [p for _, p in list(self._price_history)[-20:]]
        if len(last20) >= 2:
            moves = [1 if p > open_price else -1 if p < open_price else 0 for p in last20]
            nonzero = [m for m in moves if m != 0]
            if nonzero:
                dominant = max(set(nonzero), key=nonzero.count)
                consistency = sum(1 for m in nonzero if m == dominant) / len(nonzero)
            else:
                consistency = 0.5
        else:
            consistency = 0.5

        prices = [p for _, p in self._price_history]
        time_above_pct = sum(1 for p in prices if p > open_price) / len(prices)

        max_disp = ((self._max_price - open_price) / open_price) * 100.0
        min_disp = ((self._min_price - open_price) / open_price) * 100.0

        price_range = self._max_price - self._min_price
        if price_range == 0:
            distance_from_low = 0.5
        else:
            distance_from_low = (current_price - self._min_price) / price_range

        velocity = btc_move_pct / elapsed if elapsed > 0 else 0.0

        # acceleration: compare velocity now vs velocity 30s ago
        cutoff_30s = now.timestamp() - 30.0
        older = [(ts, p) for ts, p in self._price_history if ts.timestamp() <= cutoff_30s]
        if older:
            ts_30, p_30 = older[-1]
            elapsed_30 = (ts_30 - self._open_detected_at).total_seconds()
            vel_30 = ((p_30 - open_price) / open_price * 100.0) / elapsed_30 if elapsed_30 > 0 else 0.0
            acceleration = (velocity - vel_30) / 30.0
        else:
            acceleration = 0.0

        # momentum slope: linear regression of last 60 prices
        last60_prices = [p for _, p in list(self._price_history)[-60:]]
        momentum_slope = _linear_slope(last60_prices)

        return WindowFeatures(
            btc_move_since_open=btc_move,
            btc_move_pct=btc_move_pct,
            displacement_pct=btc_move_pct,
            abs_displacement_pct=abs(btc_move_pct),
            direction=direction,
            direction_consistency=consistency,
            cross_count=self._cross_events,
            time_above_pct=time_above_pct,
            max_displacement_pct=max_disp,
            min_displacement_pct=min_disp,
            distance_from_low=distance_from_low,
            velocity=velocity,
            acceleration=acceleration,
            elapsed_seconds=elapsed,
            momentum_slope_1min=momentum_slope,
        )

    @property
    def is_active(self) -> bool:
        return self._open_price is not None

    @property
    def elapsed_seconds(self) -> float:
        if self._open_detected_at is None:
            return 0.0
        return (datetime.now(timezone.utc) - self._open_detected_at).total_seconds()


def _linear_slope(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n
    num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return num / den if den != 0 else 0.0


class WindowLifecycleManager:
    """ONLY component that calls tracker.reset(). Triggered by market discovery."""

    def __init__(self, tracker: WindowTracker) -> None:
        self._tracker = tracker
        self.current_market_id: str | None = None
        self._current_end_time: datetime | None = None

    def on_market_discovered(
        self,
        market_id: str,
        condition_id: str,
        end_time: datetime,
        chainlink_price: float,
    ) -> bool:
        """Returns True if a new window was started."""
        if condition_id == self.current_market_id:
            return False
        log.info(
            "Market transition: %s → %s",
            self.current_market_id,
            condition_id,
        )
        self.current_market_id = condition_id
        self._current_end_time = end_time
        now = datetime.now(timezone.utc)
        self._tracker.reset(chainlink_price, now)
        return True

    def is_trading_blocked(self) -> bool:
        """RUNTIME ASSERTION per spec: elapsed > 330s blocks trading at the manager level."""
        return self._tracker.elapsed_seconds > ELAPSED_HARD_LIMIT

    def check_expiry(self) -> bool:
        """Returns True if current window has expired."""
        if self._current_end_time is None:
            return False
        now = datetime.now(timezone.utc)
        end = self._current_end_time
        # make both offset-aware or both naive
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if now > end:
            log.info("Window expired at %s — clearing until next discovery", end.isoformat())
            self.current_market_id = None
            self._current_end_time = None
            return True
        return False
