from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite
import numpy as np
import pandas as pd

from .schemas import DDL
from ..types import WindowSnapshot

_FEATURE_COLS = [
    "displacement_pct", "abs_displacement_pct", "direction",
    "direction_consistency", "cross_count", "time_above_pct", "time_below_pct",
    "max_displacement_pct", "min_displacement_pct", "velocity", "acceleration",
    "distance_from_low", "spot_displacement_pct", "spot_trend_1m",
    "spot_confirms", "depth_imbalance", "market_move_speed", "elapsed_seconds",
]


class Database:
    def __init__(self, db_path: str) -> None:
        self._path = Path(db_path)

    async def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.executescript(DDL)
            await _migrate(db)
            await db.commit()

    async def insert_window(self, snapshot: WindowSnapshot, is_primary: bool = False) -> None:
        row = (
            snapshot.window_id,
            snapshot.market_id,
            snapshot.strike_price,
            snapshot.window_open_time.isoformat(),
            snapshot.window_close_time.isoformat(),
            snapshot.open_price,
            snapshot.snapshot_price,
            snapshot.close_price,
            snapshot.displacement_pct,
            snapshot.abs_displacement_pct,
            snapshot.direction,
            snapshot.direction_consistency,
            snapshot.cross_count,
            snapshot.time_above_pct,
            snapshot.time_below_pct,
            snapshot.max_displacement_pct,
            snapshot.min_displacement_pct,
            snapshot.velocity,
            snapshot.acceleration,
            snapshot.distance_from_low,
            snapshot.spot_price,
            snapshot.spot_displacement_pct,
            snapshot.spot_trend_1m,
            int(snapshot.spot_confirms),
            snapshot.spot_source,
            snapshot.yes_bid,
            snapshot.yes_ask,
            snapshot.no_bid,
            snapshot.no_ask,
            snapshot.spread,
            snapshot.mid_price,
            snapshot.bid_depth_usd,
            snapshot.ask_depth_usd,
            snapshot.depth_imbalance,
            snapshot.market_move_speed,
            snapshot.elapsed_seconds,
            snapshot.remaining_seconds,
            snapshot.snapshot_time.isoformat(),
            snapshot.settlement_outcome,
            snapshot.settlement_price,
            int(snapshot.traded),
            snapshot.trade_side,
            snapshot.trade_entry_price,
            snapshot.trade_fill_price,
            snapshot.trade_pnl,
            snapshot.rule_signal,
            snapshot.model_prob,
            int(is_primary),
        )
        sql = """
            INSERT OR REPLACE INTO windows (
                window_id, market_id, strike_price,
                window_open_time, window_close_time,
                open_price, snapshot_price, close_price,
                displacement_pct, abs_displacement_pct, direction,
                direction_consistency, cross_count,
                time_above_pct, time_below_pct,
                max_displacement_pct, min_displacement_pct,
                velocity, acceleration, distance_from_low,
                spot_price, spot_displacement_pct, spot_trend_1m,
                spot_confirms, spot_source,
                yes_bid, yes_ask, no_bid, no_ask,
                spread, mid_price, bid_depth_usd, ask_depth_usd,
                depth_imbalance, market_move_speed,
                elapsed_seconds, remaining_seconds, snapshot_time,
                settlement_outcome, settlement_price,
                traded, trade_side,
                trade_entry_price, trade_fill_price, trade_pnl,
                rule_signal, model_prob, is_primary
            ) VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                ?,?,?,?,?,?,?,?
            )
        """
        async with aiosqlite.connect(self._path) as db:
            await db.execute(sql, row)
            await db.commit()

    async def update_settlement(
        self,
        window_id: str,
        outcome: str,
        price: float,
    ) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "UPDATE windows SET settlement_outcome=?, settlement_price=?, close_price=? WHERE window_id=?",
                (outcome, price, price, window_id),
            )
            await db.commit()

    async def update_settlement_all(
        self,
        condition_id: str,
        outcome: str,
        price: float,
    ) -> None:
        """Update settlement on ALL rows for a condition (traded + untraded)."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "UPDATE windows SET settlement_outcome=?, settlement_price=?, close_price=? WHERE window_id LIKE ?",
                (outcome, price, price, f"{condition_id}_%"),
            )
            await db.commit()

    async def update_trade_pnl(self, window_id: str, pnl: float) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "UPDATE windows SET trade_pnl=? WHERE window_id=?",
                (pnl, window_id),
            )
            await db.commit()

    async def insert_tick(
        self,
        window_id: str,
        source: str,
        price: float,
        timestamp: datetime,
    ) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO price_ticks (window_id, source, price, timestamp) VALUES (?,?,?,?)",
                (window_id, source, price, timestamp.isoformat()),
            )
            await db.commit()

    async def insert_ticks_batch(
        self,
        batch: list[tuple[str, str, float, datetime]],
    ) -> None:
        rows = [(wid, src, price, ts.isoformat()) for wid, src, price, ts in batch]
        async with aiosqlite.connect(self._path) as db:
            await db.executemany(
                "INSERT INTO price_ticks (window_id, source, price, timestamp) VALUES (?,?,?,?)",
                rows,
            )
            await db.commit()

    async def get_windows_for_date(self, date_str: str) -> list[dict[str, Any]]:
        """Returns all windows whose snapshot_time starts with date_str (YYYY-MM-DD)."""
        sql = "SELECT * FROM windows WHERE snapshot_time LIKE ? ORDER BY snapshot_time"
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (f"{date_str}%",)) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def upsert_daily_stats(
        self,
        date: str,
        total_windows: int,
        traded_windows: int,
        wins: int,
        losses: int,
        gross_pnl: float,
        net_pnl: float,
        fill_rate: float,
        maker_pct: float,
    ) -> None:
        sql = """
            INSERT OR REPLACE INTO daily_stats
                (date, total_windows, traded_windows, wins, losses,
                 gross_pnl, net_pnl, fill_rate, maker_pct)
            VALUES (?,?,?,?,?,?,?,?,?)
        """
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                sql,
                (date, total_windows, traded_windows, wins, losses,
                 gross_pnl, net_pnl, fill_rate, maker_pct),
            )
            await db.commit()

    # ------------------------------------------------------------------ #
    # Data export                                                          #
    # ------------------------------------------------------------------ #

    async def get_training_data(
        self,
        limit: int = 10_000,
        only_settled: bool = True,
        min_date: str | None = None,
        max_date: str | None = None,
    ) -> pd.DataFrame:
        """Returns a DataFrame of windows for ML training."""
        clauses: list[str] = []
        params: list[Any] = []
        if only_settled:
            clauses.append("settlement_outcome IS NOT NULL")
        if min_date:
            clauses.append("snapshot_time >= ?")
            params.append(min_date)
        if max_date:
            clauses.append("snapshot_time <= ?")
            params.append(max_date + "T23:59:59")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM windows {where} ORDER BY snapshot_time DESC LIMIT ?"
        params.append(limit)
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame([dict(r) for r in rows])

    async def get_feature_matrix(
        self,
        min_date: str | None = None,
        max_date: str | None = None,
    ) -> tuple[np.ndarray, np.ndarray, list[str]]:
        """Returns (X, y, feature_names) ready for XGBoost.

        y = 1 if settlement_outcome == 'YES' else 0.
        Only settled windows are included.
        """
        df = await self.get_training_data(
            limit=100_000,
            only_settled=True,
            min_date=min_date,
            max_date=max_date,
        )
        if df.empty:
            return np.empty((0, len(_FEATURE_COLS))), np.empty(0, dtype=int), list(_FEATURE_COLS)
        X = df[_FEATURE_COLS].fillna(0).to_numpy(dtype=float)
        y = (df["settlement_outcome"] == "YES").astype(int).to_numpy()
        return X, y, list(_FEATURE_COLS)

    async def export_csv(self, path: str, min_date: str | None = None, max_date: str | None = None) -> int:
        """Exports settled training windows to CSV. Returns row count."""
        df = await self.get_training_data(
            limit=100_000,
            only_settled=True,
            min_date=min_date,
            max_date=max_date,
        )
        if df.empty:
            return 0
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out, index=False)
        return len(df)

    async def get_recent_windows(self, n: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM windows ORDER BY created_at DESC LIMIT ?"
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (n,)) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def _migrate(db: aiosqlite.Connection) -> None:
    """Idempotent column migrations for DBs created before schema additions."""
    async with db.execute("PRAGMA table_info(windows)") as cur:
        existing = {row[1] for row in await cur.fetchall()}
    if "is_primary" not in existing:
        await db.execute("ALTER TABLE windows ADD COLUMN is_primary INTEGER DEFAULT 0")
