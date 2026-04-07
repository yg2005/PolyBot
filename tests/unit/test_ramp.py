"""Tests for SizeRamp (Phase 10 gradual ramp)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from kalbot.execution.ramp import SizeRamp


def _ramp(start: date) -> SizeRamp:
    return SizeRamp(live_start_date=start)


class TestSizeRampMultiplier:
    def test_day_1_is_25_pct(self):
        r = _ramp(date.today())
        assert r.multiplier(0.0, 15.0) == 0.25

    def test_day_3_is_25_pct(self):
        r = _ramp(date.today() - timedelta(days=2))
        assert r.multiplier(0.0, 15.0) == 0.25

    def test_day_4_is_50_pct(self):
        r = _ramp(date.today() - timedelta(days=3))
        assert r.multiplier(0.0, 15.0) == 0.50

    def test_day_7_is_50_pct(self):
        r = _ramp(date.today() - timedelta(days=6))
        assert r.multiplier(0.0, 15.0) == 0.50

    def test_day_8_is_100_pct(self):
        r = _ramp(date.today() - timedelta(days=7))
        assert r.multiplier(0.0, 15.0) == 1.0

    def test_day_30_is_100_pct(self):
        r = _ramp(date.today() - timedelta(days=29))
        assert r.multiplier(0.0, 15.0) == 1.0


class TestSizeRampDeRamp:
    def test_deramp_at_50pct_daily_loss(self):
        """Daily loss >= 50% of limit triggers de-ramp back to 25%."""
        # Day 8+ would normally be 100%
        r = _ramp(date.today() - timedelta(days=10))
        # Loss of 7.5 on a 15.0 limit = exactly 50%
        assert r.multiplier(-7.5, 15.0) == 0.25

    def test_deramp_at_over_50pct_daily_loss(self):
        r = _ramp(date.today() - timedelta(days=10))
        assert r.multiplier(-10.0, 15.0) == 0.25

    def test_no_deramp_below_50pct_loss(self):
        r = _ramp(date.today() - timedelta(days=10))
        # Loss of 7.4 < 50% of 15.0 → no de-ramp
        assert r.multiplier(-7.4, 15.0) == 1.0

    def test_no_deramp_on_profit(self):
        r = _ramp(date.today() - timedelta(days=10))
        assert r.multiplier(5.0, 15.0) == 1.0

    def test_deramp_overrides_day_number(self):
        """Even day 4-7 (50%) gets de-ramped to 25% on big loss."""
        r = _ramp(date.today() - timedelta(days=4))
        assert r.multiplier(-8.0, 15.0) == 0.25


class TestSizeRampApply:
    def test_apply_scales_correctly(self):
        r = _ramp(date.today())
        assert r.apply(10.0, 0.0, 15.0) == pytest.approx(2.50)

    def test_apply_day_8_full_size(self):
        r = _ramp(date.today() - timedelta(days=7))
        assert r.apply(10.0, 0.0, 15.0) == pytest.approx(10.0)

    def test_apply_rounds_to_2dp(self):
        r = _ramp(date.today())
        # 10.333... * 0.25 = 2.583... → rounds to 2.58
        result = r.apply(10.333, 0.0, 15.0)
        assert result == round(10.333 * 0.25, 2)


class TestSizeRampDayNumber:
    def test_day_number_today(self):
        r = _ramp(date.today())
        assert r.day_number == 1

    def test_day_number_yesterday(self):
        r = _ramp(date.today() - timedelta(days=1))
        assert r.day_number == 2
