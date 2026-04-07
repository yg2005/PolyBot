from __future__ import annotations

import os
from datetime import datetime, timezone

import aiosqlite
import pytest
import pytest_asyncio

from kalbot.data.db import Database
from kalbot.data.logger import WindowLogger, TickLogger, _compute_pnl
from kalbot.types import WindowSnapshot


def _make_snapshot(**kwargs) -> WindowSnapshot:
    defaults = dict(
        window_id="w-001",
        market_id="m-001",
        strike_price=85000.0,
        window_open_time=datetime(2026, 4, 5, 12, 0, tzinfo=timezone.utc),
        window_close_time=datetime(2026, 4, 5, 12, 5, tzinfo=timezone.utc),
        open_price=85000.0,
        snapshot_price=85200.0,
        close_price=None,
        displacement_pct=0.235,
        abs_displacement_pct=0.235,
        direction=1,
        direction_consistency=0.75,
        cross_count=2,
        time_above_pct=0.70,
        time_below_pct=0.30,
        max_displacement_pct=0.30,
        min_displacement_pct=-0.05,
        velocity=0.002,
        acceleration=0.0001,
        distance_from_low=0.85,
        spot_price=85190.0,
        spot_displacement_pct=0.22,
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
        ask_depth_usd=300.0,
        depth_imbalance=0.25,
        market_move_speed=0.8,
        elapsed_seconds=180,
        remaining_seconds=120,
        snapshot_time=datetime(2026, 4, 5, 12, 3, tzinfo=timezone.utc),
    )
    defaults.update(kwargs)
    return WindowSnapshot(**defaults)


@pytest_asyncio.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    await d.init()
    return d


@pytest.mark.asyncio
async def test_log_snapshot_inserts_row(db):
    logger = WindowLogger(db, [120, 150, 180, 210, 240])
    snap = _make_snapshot()
    await logger.log_snapshot(snap)
    rows = await db.get_recent_windows(10)
    assert len(rows) == 1
    assert rows[0]["window_id"] == "w-001"
    assert rows[0]["traded"] == 0


@pytest.mark.asyncio
async def test_log_snapshot_upsert(db):
    """Second insert with same window_id replaces the row."""
    logger = WindowLogger(db, [120, 180])
    snap1 = _make_snapshot(elapsed_seconds=120)
    snap2 = _make_snapshot(elapsed_seconds=180)
    await logger.log_snapshot(snap1)
    await logger.log_snapshot(snap2)
    rows = await db.get_recent_windows(10)
    assert len(rows) == 1
    assert rows[0]["elapsed_seconds"] == 180


@pytest.mark.asyncio
async def test_settlement_yes(db):
    logger = WindowLogger(db, [180])
    snap = _make_snapshot()
    await logger.log_snapshot(snap)

    await logger.on_settlement(
        window_id="w-001",
        settlement_price=85300.0,
        strike_price=85000.0,
        traded=False,
        trade_side=None,
        trade_entry_price=None,
        size_usd=None,
    )
    rows = await db.get_recent_windows(1)
    assert rows[0]["settlement_outcome"] == "YES"
    assert rows[0]["settlement_price"] == 85300.0
    assert rows[0]["close_price"] == 85300.0  # spec: also update close_price


@pytest.mark.asyncio
async def test_settlement_no(db):
    logger = WindowLogger(db, [180])
    snap = _make_snapshot()
    await logger.log_snapshot(snap)

    await logger.on_settlement(
        window_id="w-001",
        settlement_price=84900.0,
        strike_price=85000.0,
        traded=False,
        trade_side=None,
        trade_entry_price=None,
        size_usd=None,
    )
    rows = await db.get_recent_windows(1)
    assert rows[0]["settlement_outcome"] == "NO"
    assert rows[0]["close_price"] == 84900.0


@pytest.mark.asyncio
async def test_settlement_pnl_yes_win(db):
    """Buy YES at 0.55, outcome YES → profit."""
    logger = WindowLogger(db, [180])
    snap = _make_snapshot(traded=True, trade_side="YES", trade_entry_price=0.55)
    await logger.log_snapshot(snap)

    await logger.on_settlement(
        window_id="w-001",
        settlement_price=85300.0,
        strike_price=85000.0,
        traded=True,
        trade_side="YES",
        trade_entry_price=0.55,
        size_usd=10.0,
    )
    rows = await db.get_recent_windows(1)
    pnl = rows[0]["trade_pnl"]
    assert pnl is not None
    assert pnl == pytest.approx(10.0 * (1.0 - 0.55) / 0.55, rel=1e-4)


@pytest.mark.asyncio
async def test_settlement_pnl_yes_loss(db):
    """Buy YES at 0.55, outcome NO → loss."""
    logger = WindowLogger(db, [180])
    snap = _make_snapshot(traded=True, trade_side="YES", trade_entry_price=0.55)
    await logger.log_snapshot(snap)

    await logger.on_settlement(
        window_id="w-001",
        settlement_price=84900.0,
        strike_price=85000.0,
        traded=True,
        trade_side="YES",
        trade_entry_price=0.55,
        size_usd=10.0,
    )
    rows = await db.get_recent_windows(1)
    assert rows[0]["trade_pnl"] < 0


def test_compute_pnl_yes_win():
    pnl = _compute_pnl("YES", 0.55, "YES", 10.0)
    assert pnl == pytest.approx(10.0 * 0.45 / 0.55, rel=1e-6)


def test_compute_pnl_yes_loss():
    pnl = _compute_pnl("YES", 0.55, "NO", 10.0)
    assert pnl == pytest.approx(-10.0, rel=1e-6)


def test_compute_pnl_no_win():
    pnl = _compute_pnl("NO", 0.40, "NO", 10.0)
    assert pnl == pytest.approx(10.0 * 0.60 / 0.40, rel=1e-6)


def test_compute_pnl_zero_entry():
    assert _compute_pnl("YES", 0.0, "YES", 10.0) == 0.0


@pytest.mark.asyncio
async def test_tick_logger_batch(db, tmp_path):
    tl = TickLogger(db)
    now = datetime.now(timezone.utc)
    tl.record("w-001", "chainlink", 85000.0, now)
    tl.record("w-001", "coinbase", 85010.0, now)
    await tl._flush()

    async with aiosqlite.connect(db._path) as conn:
        async with conn.execute("SELECT count(*) FROM price_ticks") as cur:
            row = await cur.fetchone()
    assert row[0] == 2


@pytest.mark.asyncio
async def test_is_primary_stored(db):
    logger = WindowLogger(db, [180])
    snap = _make_snapshot()
    await logger.log_snapshot(snap, is_primary=True)
    rows = await db.get_recent_windows(1)
    assert rows[0]["is_primary"] == 1


@pytest.mark.asyncio
async def test_get_training_data_returns_dataframe(db):
    import pandas as pd
    logger = WindowLogger(db, [180])
    snap = _make_snapshot()
    await logger.log_snapshot(snap)
    await db.update_settlement("w-001", "YES", 85300.0)

    df = await db.get_training_data()
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 1
    assert df.iloc[0]["window_id"] == "w-001"


@pytest.mark.asyncio
async def test_get_feature_matrix(db):
    import numpy as np
    logger = WindowLogger(db, [180])
    snap = _make_snapshot()
    await logger.log_snapshot(snap)
    await db.update_settlement("w-001", "YES", 85300.0)

    X, y, cols = await db.get_feature_matrix()
    assert isinstance(X, np.ndarray)
    assert isinstance(y, np.ndarray)
    assert len(X) == 1
    assert y[0] == 1
    assert "displacement_pct" in cols


@pytest.mark.asyncio
async def test_export_csv(db, tmp_path):
    logger = WindowLogger(db, [180])
    snap = _make_snapshot()
    await logger.log_snapshot(snap)
    await db.update_settlement("w-001", "YES", 85300.0)

    out = str(tmp_path / "export.csv")
    count = await db.export_csv(out)
    assert count == 1
    assert os.path.exists(out)


@pytest.mark.asyncio
async def test_daily_stats_upsert(db):
    await db.upsert_daily_stats(
        date="2026-04-04",
        total_windows=12,
        traded_windows=3,
        wins=2,
        losses=1,
        gross_pnl=5.5,
        net_pnl=5.5,
        fill_rate=0.25,
        maker_pct=0.0,
    )

    async with aiosqlite.connect(db._path) as conn:
        async with conn.execute(
            "SELECT total_windows, wins FROM daily_stats WHERE date='2026-04-04'"
        ) as cur:
            row = await cur.fetchone()
    assert row[0] == 12
    assert row[1] == 2


@pytest.mark.asyncio
async def test_get_windows_for_date(db):
    logger = WindowLogger(db, [180])
    for i in range(3):
        snap = _make_snapshot(
            window_id=f"w-{i:03d}",
            snapshot_time=datetime(2026, 4, 4, 12, i, tzinfo=timezone.utc),
        )
        await logger.log_snapshot(snap)
    # different date
    snap_other = _make_snapshot(
        window_id="w-999",
        snapshot_time=datetime(2026, 4, 5, 12, 0, tzinfo=timezone.utc),
    )
    await logger.log_snapshot(snap_other)

    rows = await db.get_windows_for_date("2026-04-04")
    assert len(rows) == 3
