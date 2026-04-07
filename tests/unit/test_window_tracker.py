"""Tests for WindowTracker and WindowLifecycleManager."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kalbot.engine.window_tracker import (
    WindowLifecycleManager,
    WindowTracker,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_tracker_with_prices(
    open_price: float,
    prices: list[float],
    start: datetime | None = None,
) -> tuple[WindowTracker, datetime]:
    t = WindowTracker()
    if start is None:
        start = _now() - timedelta(seconds=len(prices))
    t.reset(open_price, start)
    for i, p in enumerate(prices):
        ts = start + timedelta(seconds=i + 1)
        t.update(p, ts)
    return t, start


# ------------------------------------------------------------------ #
# Uptrend                                                              #
# ------------------------------------------------------------------ #
class TestUptrend:
    def test_direction_positive(self):
        prices = [100.0 + i * 0.01 for i in range(90)]
        tracker, _ = _make_tracker_with_prices(100.0, prices)
        f = tracker.get_features(prices[-1])
        assert f is not None
        assert f.direction == 1

    def test_positive_displacement(self):
        prices = [100.0 + i * 0.01 for i in range(90)]
        tracker, _ = _make_tracker_with_prices(100.0, prices)
        f = tracker.get_features(prices[-1])
        assert f.displacement_pct > 0

    def test_time_above_high(self):
        prices = [100.5] * 90  # all above 100.0 open
        tracker, _ = _make_tracker_with_prices(100.0, prices)
        f = tracker.get_features(100.5)
        assert f.time_above_pct > 0.9

    def test_high_consistency(self):
        prices = [100.5] * 90
        tracker, _ = _make_tracker_with_prices(100.0, prices)
        f = tracker.get_features(100.5)
        assert f.direction_consistency >= 0.9

    def test_cross_count_zero(self):
        prices = [100.5] * 90
        tracker, _ = _make_tracker_with_prices(100.0, prices)
        f = tracker.get_features(100.5)
        assert f.cross_count == 0


# ------------------------------------------------------------------ #
# Downtrend                                                            #
# ------------------------------------------------------------------ #
class TestDowntrend:
    def test_direction_negative(self):
        prices = [100.0 - i * 0.01 for i in range(90)]
        tracker, _ = _make_tracker_with_prices(100.0, prices)
        f = tracker.get_features(prices[-1])
        assert f.direction == -1

    def test_negative_displacement(self):
        prices = [100.0 - i * 0.01 for i in range(90)]
        tracker, _ = _make_tracker_with_prices(100.0, prices)
        f = tracker.get_features(prices[-1])
        assert f.displacement_pct < 0
        assert f.abs_displacement_pct > 0

    def test_time_below_dominant(self):
        prices = [99.5] * 90
        tracker, _ = _make_tracker_with_prices(100.0, prices)
        f = tracker.get_features(99.5)
        assert f.time_above_pct < 0.1


# ------------------------------------------------------------------ #
# Choppy                                                               #
# ------------------------------------------------------------------ #
class TestChoppy:
    def test_many_crosses(self):
        # price alternates above/below open every sample
        prices = [100.5 if i % 2 == 0 else 99.5 for i in range(60)]
        tracker, _ = _make_tracker_with_prices(100.0, prices)
        f = tracker.get_features(prices[-1])
        assert f.cross_count > 5

    def test_low_consistency(self):
        prices = [100.5 if i % 2 == 0 else 99.5 for i in range(60)]
        tracker, _ = _make_tracker_with_prices(100.0, prices)
        f = tracker.get_features(prices[-1])
        # alternating → consistency should be near 0.5
        assert f.direction_consistency <= 0.6


# ------------------------------------------------------------------ #
# Flat                                                                 #
# ------------------------------------------------------------------ #
class TestFlat:
    def test_flat_direction_zero(self):
        prices = [100.0] * 60
        tracker, _ = _make_tracker_with_prices(100.0, prices)
        f = tracker.get_features(100.0)
        assert f.direction == 0

    def test_flat_abs_displacement_zero(self):
        prices = [100.0] * 60
        tracker, _ = _make_tracker_with_prices(100.0, prices)
        f = tracker.get_features(100.0)
        assert f.abs_displacement_pct == pytest.approx(0.0)

    def test_distance_from_low_flat(self):
        prices = [100.0] * 60
        tracker, _ = _make_tracker_with_prices(100.0, prices)
        f = tracker.get_features(100.0)
        # zero range → 0.5
        assert f.distance_from_low == pytest.approx(0.5)


# ------------------------------------------------------------------ #
# At-open edge cases                                                   #
# ------------------------------------------------------------------ #
class TestEdgeCases:
    def test_none_before_first_update(self):
        t = WindowTracker()
        t.reset(100.0, _now() - timedelta(seconds=90))
        # no updates
        assert t.get_features(100.0) is None

    def test_none_with_one_update(self):
        t = WindowTracker()
        start = _now() - timedelta(seconds=90)
        t.reset(100.0, start)
        t.update(100.1, start + timedelta(seconds=1))
        # only 1 sample → None
        assert t.get_features(100.1) is None

    def test_reset_clears_state(self):
        prices = [100.5] * 60
        tracker, _ = _make_tracker_with_prices(100.0, prices)
        f1 = tracker.get_features(100.5)
        assert f1 is not None

        tracker.reset(200.0, _now() - timedelta(seconds=90))
        assert tracker.get_features(200.0) is None  # no updates yet

    def test_velocity_positive_uptrend(self):
        prices = [100.0 + i * 0.01 for i in range(90)]
        tracker, _ = _make_tracker_with_prices(100.0, prices)
        f = tracker.get_features(prices[-1])
        assert f.velocity > 0

    def test_330s_hard_limit_blocks(self):
        # open_time 400s ago → elapsed > 330 → get_features must return None
        prices = [100.5] * 60
        start = _now() - timedelta(seconds=400)
        tracker, _ = _make_tracker_with_prices(100.0, prices, start=start)
        assert tracker.get_features(100.5) is None


# ------------------------------------------------------------------ #
# WindowLifecycleManager                                               #
# ------------------------------------------------------------------ #
class TestLifecycleManager:
    def test_reset_on_new_market(self):
        tracker = WindowTracker()
        mgr = WindowLifecycleManager(tracker)
        end = _now() + timedelta(minutes=5)

        mgr.on_market_discovered("m1", "cond_A", end, 100.0)
        assert mgr.current_market_id == "cond_A"

    def test_no_reset_same_market(self):
        tracker = WindowTracker()
        mgr = WindowLifecycleManager(tracker)
        end = _now() + timedelta(minutes=5)

        mgr.on_market_discovered("m1", "cond_A", end, 100.0)
        # feed it 60 prices
        start = _now() - timedelta(seconds=60)
        for i in range(60):
            tracker.update(100.5, start + timedelta(seconds=i))

        # same condition_id — should NOT reset
        result = mgr.on_market_discovered("m1", "cond_A", end, 99.0)
        assert result is False
        # tracker still has data
        assert tracker.get_features(100.5) is not None

    def test_reset_on_different_condition(self):
        tracker = WindowTracker()
        mgr = WindowLifecycleManager(tracker)
        end = _now() + timedelta(minutes=5)

        mgr.on_market_discovered("m1", "cond_A", end, 100.0)
        start = _now() - timedelta(seconds=60)
        for i in range(60):
            tracker.update(100.5, start + timedelta(seconds=i))

        # new condition → reset
        mgr.on_market_discovered("m2", "cond_B", end, 101.0)
        assert mgr.current_market_id == "cond_B"
        # tracker was reset — no data yet
        assert tracker.get_features(101.0) is None

    def test_check_expiry(self):
        tracker = WindowTracker()
        mgr = WindowLifecycleManager(tracker)
        past_end = _now() - timedelta(seconds=1)
        mgr.on_market_discovered("m1", "cond_A", past_end, 100.0)

        assert mgr.check_expiry() is True
        assert mgr.current_market_id is None

    def test_is_trading_blocked_false_normal(self):
        tracker = WindowTracker()
        mgr = WindowLifecycleManager(tracker)
        end = _now() + timedelta(minutes=5)
        mgr.on_market_discovered("m1", "cond_A", end, 100.0)
        assert mgr.is_trading_blocked() is False

    def test_is_trading_blocked_true_over_330s(self):
        tracker = WindowTracker()
        mgr = WindowLifecycleManager(tracker)
        # Manually reset tracker with open_time 400s ago
        stale_open = _now() - timedelta(seconds=400)
        tracker.reset(100.0, stale_open)
        mgr.current_market_id = "cond_A"
        assert mgr.is_trading_blocked() is True
