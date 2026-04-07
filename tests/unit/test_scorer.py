"""Tests for RuleScorer — all 17 threshold gates."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kalbot.engine.scorer import RuleScorer
from kalbot.engine.window_tracker import WindowFeatures, WindowTracker
from kalbot.types import WindowSnapshot


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _snapshot(**overrides) -> WindowSnapshot:
    defaults = dict(
        window_id="w1",
        market_id="m1",
        strike_price=100_000.0,
        window_open_time=_now() - timedelta(minutes=3),
        window_close_time=_now() + timedelta(minutes=2),
        open_price=100_000.0,
        snapshot_price=100_300.0,
        close_price=None,
        displacement_pct=0.3,
        abs_displacement_pct=0.3,
        direction=1,
        direction_consistency=0.80,
        cross_count=1,
        time_above_pct=0.70,
        time_below_pct=0.30,
        max_displacement_pct=0.35,
        min_displacement_pct=-0.02,
        velocity=0.002,
        acceleration=0.0,
        distance_from_low=0.9,
        spot_price=100_300.0,
        spot_displacement_pct=0.3,
        spot_trend_1m=0.001,
        spot_confirms=True,
        spot_source="coinbase",
        yes_bid=0.60,
        yes_ask=0.62,
        no_bid=0.38,
        no_ask=0.40,
        spread=0.02,
        mid_price=0.61,
        bid_depth_usd=500.0,
        ask_depth_usd=400.0,
        depth_imbalance=0.1,
        market_move_speed=0.5,
        elapsed_seconds=180,
        remaining_seconds=120,
        snapshot_time=_now(),
    )
    defaults.update(overrides)
    return WindowSnapshot(**defaults)


def _features(**overrides) -> WindowFeatures:
    defaults = dict(
        btc_move_since_open=300.0,
        btc_move_pct=0.30,
        displacement_pct=0.30,
        abs_displacement_pct=0.30,
        direction=1,
        direction_consistency=0.80,
        cross_count=1,
        time_above_pct=0.70,
        max_displacement_pct=0.35,
        min_displacement_pct=-0.02,
        distance_from_low=0.9,
        velocity=0.002,
        acceleration=0.0,
        elapsed_seconds=180.0,
        momentum_slope_1min=0.001,
    )
    defaults.update(overrides)
    return WindowFeatures(**defaults)


@pytest.fixture
def scorer():
    tracker = WindowTracker()
    return RuleScorer(tracker, series_ticker="POLYBTC5M")


async def _score(scorer, snap, feats, ticker="POLYBTC5M", chainlink_age=0.0):
    return await scorer.score(snap, features=feats, market_ticker=ticker, chainlink_age_s=chainlink_age)


# ------------------------------------------------------------------ #
# Passing baseline                                                     #
# ------------------------------------------------------------------ #
class TestBaseline:
    @pytest.mark.asyncio
    async def test_baseline_yes(self, scorer):
        snap = _snapshot()
        feats = _features()
        result = await _score(scorer, snap, feats)
        assert result.signal == "YES"
        assert result.edge_estimate > 0
        assert 0 < result.confidence <= 0.95


# ------------------------------------------------------------------ #
# Gate 1: market ticker                                                #
# ------------------------------------------------------------------ #
class TestGate1:
    @pytest.mark.asyncio
    async def test_wrong_ticker(self, scorer):
        snap = _snapshot()
        feats = _features()
        result = await _score(scorer, snap, feats, ticker="WRONGMARKET")
        assert result.signal == "PASS"
        assert "gate1" in result.reasoning


# ------------------------------------------------------------------ #
# Gate 2: chainlink staleness                                          #
# ------------------------------------------------------------------ #
class TestGate2:
    @pytest.mark.asyncio
    async def test_stale_no_spot_confirm(self, scorer):
        snap = _snapshot(spot_confirms=False)
        feats = _features()
        result = await _score(scorer, snap, feats, chainlink_age=15.0)
        assert result.signal == "PASS"
        assert "gate2" in result.reasoning

    @pytest.mark.asyncio
    async def test_stale_but_spot_confirms(self, scorer):
        snap = _snapshot(spot_confirms=True)
        feats = _features()
        result = await _score(scorer, snap, feats, chainlink_age=15.0)
        # spot fallback — should proceed
        assert result.signal == "YES"


# ------------------------------------------------------------------ #
# Gate 3: remaining_seconds                                            #
# ------------------------------------------------------------------ #
class TestGate3:
    @pytest.mark.asyncio
    async def test_too_little_remaining(self, scorer):
        snap = _snapshot(remaining_seconds=20)
        feats = _features()
        result = await _score(scorer, snap, feats)
        assert result.signal == "PASS"
        assert "gate3" in result.reasoning


# ------------------------------------------------------------------ #
# Gate 4: elapsed > 270                                                #
# ------------------------------------------------------------------ #
class TestGate4:
    @pytest.mark.asyncio
    async def test_elapsed_too_high(self, scorer):
        snap = _snapshot(elapsed_seconds=280)
        feats = _features(elapsed_seconds=280.0)
        result = await _score(scorer, snap, feats)
        assert result.signal == "PASS"
        assert "gate4" in result.reasoning


# ------------------------------------------------------------------ #
# Gate 5: no features                                                  #
# ------------------------------------------------------------------ #
class TestGate5:
    @pytest.mark.asyncio
    async def test_no_features(self, scorer):
        snap = _snapshot()
        result = await scorer.score(snap, features=None, market_ticker="POLYBTC5M")
        assert result.signal == "PASS"
        assert "gate5" in result.reasoning


# ------------------------------------------------------------------ #
# Gate 6: elapsed < 60                                                 #
# ------------------------------------------------------------------ #
class TestGate6:
    @pytest.mark.asyncio
    async def test_elapsed_too_low(self, scorer):
        snap = _snapshot(elapsed_seconds=30)
        feats = _features(elapsed_seconds=30.0)
        result = await _score(scorer, snap, feats)
        assert result.signal == "PASS"
        assert "gate6" in result.reasoning


# ------------------------------------------------------------------ #
# Gate 7: cross_count                                                  #
# ------------------------------------------------------------------ #
class TestGate7:
    @pytest.mark.asyncio
    async def test_too_choppy(self, scorer):
        snap = _snapshot()
        feats = _features(cross_count=8)
        result = await _score(scorer, snap, feats)
        assert result.signal == "PASS"
        assert "gate7" in result.reasoning


# ------------------------------------------------------------------ #
# Gate 8: abs displacement                                             #
# ------------------------------------------------------------------ #
class TestGate8:
    @pytest.mark.asyncio
    async def test_too_small_move(self, scorer):
        snap = _snapshot()
        feats = _features(abs_displacement_pct=0.005, btc_move_pct=0.005, btc_move_since_open=5.0)
        result = await _score(scorer, snap, feats)
        assert result.signal == "PASS"
        assert "gate8" in result.reasoning


# ------------------------------------------------------------------ #
# Gate 9: direction consistency                                        #
# ------------------------------------------------------------------ #
class TestGate9:
    @pytest.mark.asyncio
    async def test_low_consistency(self, scorer):
        snap = _snapshot()
        feats = _features(direction_consistency=0.50)
        result = await _score(scorer, snap, feats)
        assert result.signal == "PASS"
        assert "gate9" in result.reasoning


# ------------------------------------------------------------------ #
# Gate 10: yes_bid range                                               #
# ------------------------------------------------------------------ #
class TestGate10:
    @pytest.mark.asyncio
    async def test_bid_too_low(self, scorer):
        snap = _snapshot(yes_bid=0.10)
        feats = _features()
        result = await _score(scorer, snap, feats)
        assert result.signal == "PASS"
        assert "gate10" in result.reasoning

    @pytest.mark.asyncio
    async def test_bid_too_high(self, scorer):
        snap = _snapshot(yes_bid=0.90)
        feats = _features()
        result = await _score(scorer, snap, feats)
        assert result.signal == "PASS"
        assert "gate10" in result.reasoning


# ------------------------------------------------------------------ #
# Gate 11: time_above_pct for YES                                      #
# ------------------------------------------------------------------ #
class TestGate11:
    @pytest.mark.asyncio
    async def test_yes_time_above_too_low(self, scorer):
        snap = _snapshot(time_above_pct=0.40)
        feats = _features(btc_move_since_open=300.0)  # YES direction
        result = await _score(scorer, snap, feats)
        assert result.signal == "PASS"
        assert "gate11" in result.reasoning


# ------------------------------------------------------------------ #
# Gate 12: time_above_pct for NO                                       #
# ------------------------------------------------------------------ #
class TestGate12:
    @pytest.mark.asyncio
    async def test_no_time_above_too_high(self, scorer):
        snap = _snapshot(time_above_pct=0.60)
        feats = _features(
            btc_move_since_open=-300.0,
            btc_move_pct=-0.30,
            displacement_pct=-0.30,
            direction=-1,
        )
        result = await _score(scorer, snap, feats)
        assert result.signal == "PASS"
        assert "gate12" in result.reasoning


# ------------------------------------------------------------------ #
# Gate 13: spot trend conflict for YES                                 #
# ------------------------------------------------------------------ #
class TestGate13:
    @pytest.mark.asyncio
    async def test_yes_spot_trend_conflict(self, scorer):
        snap = _snapshot(spot_trend_1m=-0.002)
        feats = _features(btc_move_since_open=300.0)
        result = await _score(scorer, snap, feats)
        assert result.signal == "PASS"
        assert "gate13" in result.reasoning


# ------------------------------------------------------------------ #
# Gate 14: spot trend conflict for NO                                  #
# ------------------------------------------------------------------ #
class TestGate14:
    @pytest.mark.asyncio
    async def test_no_spot_trend_conflict(self, scorer):
        snap = _snapshot(spot_trend_1m=0.002, time_above_pct=0.25)
        feats = _features(
            btc_move_since_open=-300.0,
            btc_move_pct=-0.30,
            displacement_pct=-0.30,
            direction=-1,
        )
        result = await _score(scorer, snap, feats)
        assert result.signal == "PASS"
        assert "gate14" in result.reasoning


# ------------------------------------------------------------------ #
# NO signal                                                            #
# ------------------------------------------------------------------ #
class TestNoSignal:
    @pytest.mark.asyncio
    async def test_baseline_no(self, scorer):
        snap = _snapshot(time_above_pct=0.25, spot_trend_1m=-0.0001)
        feats = _features(
            btc_move_since_open=-300.0,
            btc_move_pct=-0.30,
            displacement_pct=-0.30,
            abs_displacement_pct=0.30,
            direction=-1,
        )
        result = await _score(scorer, snap, feats)
        assert result.signal == "NO"


# ------------------------------------------------------------------ #
# TTC confidence boost                                                 #
# ------------------------------------------------------------------ #
class TestTTCBoost:
    @pytest.mark.asyncio
    async def test_boost_under_90s(self, scorer):
        snap_normal = _snapshot(remaining_seconds=200)
        snap_urgent = _snapshot(remaining_seconds=60)
        feats = _features()
        r_normal = await _score(scorer, snap_normal, feats)
        r_urgent = await _score(scorer, snap_urgent, feats)
        assert r_urgent.confidence > r_normal.confidence

    @pytest.mark.asyncio
    async def test_confidence_capped(self, scorer):
        snap = _snapshot(remaining_seconds=30)
        feats = _features(direction_consistency=1.0, abs_displacement_pct=0.5)
        result = await _score(scorer, snap, feats)
        assert result.confidence <= 0.95
