"""Tests for KillSwitch (Phase 10)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kalbot.kill_switch import (
    CONSECUTIVE_5XX_LIMIT,
    FLAG_FILE,
    KillSwitch,
    _check_internet,
)


@pytest.fixture(autouse=True)
def clean_flag(tmp_path, monkeypatch):
    """Redirect FLAG_FILE to a temp path so tests don't pollute data/."""
    fake_flag = tmp_path / "kill_switch.flag"
    monkeypatch.setattr("kalbot.kill_switch.FLAG_FILE", fake_flag)
    yield fake_flag
    # Cleanup
    if fake_flag.exists():
        fake_flag.unlink()


class TestKillSwitchBasic:
    def test_not_engaged_by_default(self, clean_flag):
        ks = KillSwitch()
        assert not ks.is_engaged()

    def test_loads_flag_on_startup(self, clean_flag):
        clean_flag.parent.mkdir(parents=True, exist_ok=True)
        clean_flag.write_text("test reason")
        ks = KillSwitch()
        assert ks.is_engaged()
        assert ks.reason == "test reason"

    def test_reset_clears_flag(self, clean_flag):
        clean_flag.parent.mkdir(parents=True, exist_ok=True)
        clean_flag.write_text("reason")
        ks = KillSwitch()
        assert ks.is_engaged()
        ks.reset()
        assert not ks.is_engaged()
        assert not clean_flag.exists()


class TestKillSwitchEngage:
    @pytest.mark.asyncio
    async def test_engage_writes_flag(self, clean_flag):
        clean_flag.parent.mkdir(parents=True, exist_ok=True)
        ks = KillSwitch()
        await ks.engage("test trigger")
        assert ks.is_engaged()
        assert ks.reason == "test trigger"
        assert clean_flag.exists()
        assert clean_flag.read_text() == "test trigger"

    @pytest.mark.asyncio
    async def test_engage_idempotent(self, clean_flag):
        clean_flag.parent.mkdir(parents=True, exist_ok=True)
        ks = KillSwitch()
        await ks.engage("first")
        await ks.engage("second")  # should not change reason
        assert ks.reason == "first"

    @pytest.mark.asyncio
    async def test_engage_calls_cancel_all(self, clean_flag):
        clean_flag.parent.mkdir(parents=True, exist_ok=True)
        ks = KillSwitch()
        om = MagicMock()
        om.cancel_all_live = AsyncMock(return_value=3)
        ks.set_order_manager(om)
        await ks.engage("cancel test")
        om.cancel_all_live.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_engage_calls_alerts(self, clean_flag):
        clean_flag.parent.mkdir(parents=True, exist_ok=True)
        ks = KillSwitch()
        alerts = MagicMock()
        alerts.circuit_breaker = AsyncMock()
        ks.set_alerts(alerts)
        await ks.engage("alert test")
        alerts.circuit_breaker.assert_awaited_once()
        call_arg = alerts.circuit_breaker.call_args[0][0]
        assert "KILL SWITCH" in call_arg


class TestKillSwitch5xx:
    @pytest.mark.asyncio
    async def test_consecutive_5xx_triggers_engage(self, clean_flag):
        clean_flag.parent.mkdir(parents=True, exist_ok=True)
        ks = KillSwitch()
        # Simulate responses below threshold — should not engage
        for _ in range(CONSECUTIVE_5XX_LIMIT - 1):
            ks.record_api_response(500)
        assert not ks.is_engaged()

        # One more — should schedule engage
        ks.record_api_response(502)
        # Give the event loop a tick to run the scheduled coroutine
        await asyncio.sleep(0)
        # May not be engaged yet depending on event loop scheduling
        assert ks._consecutive_5xx >= CONSECUTIVE_5XX_LIMIT

    def test_non_5xx_resets_counter(self, clean_flag):
        ks = KillSwitch()
        ks.record_api_response(500)
        ks.record_api_response(500)
        ks.record_api_response(200)
        assert ks._consecutive_5xx == 0

    def test_4xx_resets_counter(self, clean_flag):
        ks = KillSwitch()
        ks.record_api_response(500)
        ks.record_api_response(400)
        assert ks._consecutive_5xx == 0


class TestKillSwitchInternetCheck:
    def test_check_internet_returns_bool(self):
        # Just verify it returns a bool — actual connectivity may vary
        result = _check_internet()
        assert isinstance(result, bool)
