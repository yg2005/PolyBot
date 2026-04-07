from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

_CLOB_MOVE_THRESHOLD = 0.0005  # 0.05% mid-price move to count as repricing


class EdgeMonitor:
    """Weekly realized vs predicted edge tracker.

    Queries edge_tracker table and computes weekly stats. Fires decay alert
    if realized edge has declined for 3 consecutive weeks.
    """

    def __init__(
        self,
        db_path: str,
        alert_callback: Any | None = None,  # async callable(event_type, payload)
        check_interval_s: int = 3600,
    ) -> None:
        self._db_path = db_path
        self._alert_cb = alert_callback
        self._check_interval_s = check_interval_s
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="EdgeMonitor")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------ #
    # Weekly update                                                        #
    # ------------------------------------------------------------------ #

    async def record_week(
        self,
        week_start: str,
        realized_edge: float,
        predicted_edge: float,
        trade_count: int,
        win_rate: float,
        avg_repricing_ms: float,
    ) -> None:
        efficiency = await self._compute_market_efficiency(week_start)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO edge_tracker
                   (week_start, realized_edge, predicted_edge,
                    market_efficiency_score, avg_repricing_speed_ms,
                    trade_count, win_rate)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    week_start,
                    realized_edge,
                    predicted_edge,
                    efficiency,
                    avg_repricing_ms,
                    trade_count,
                    win_rate,
                ),
            )
            await db.commit()
        log.info(
            "EdgeTracker week=%s realized=%.4f predicted=%.4f efficiency=%.4f",
            week_start,
            realized_edge,
            predicted_edge,
            efficiency,
        )
        await self._check_decay()

    # ------------------------------------------------------------------ #
    # Queries                                                              #
    # ------------------------------------------------------------------ #

    async def get_recent_weeks(self, n: int = 8) -> list[dict]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM edge_tracker ORDER BY week_start DESC LIMIT ?", (n,)
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def compute_current_week_stats(self) -> dict:
        """Compute realized edge for the current ISO week from windows table."""
        now = datetime.now(timezone.utc)
        # Monday of this week
        week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        week_end = (now - timedelta(days=now.weekday()) + timedelta(days=7)).strftime(
            "%Y-%m-%d"
        )

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT trade_pnl, trade_entry_price, trade_side, mid_price,
                          model_prob, settlement_outcome
                   FROM windows
                   WHERE snapshot_time >= ? AND snapshot_time < ?
                     AND traded=1 AND settlement_outcome IS NOT NULL""",
                (week_start, week_end),
            ) as cur:
                rows = await cur.fetchall()

        rows_d = [dict(r) for r in rows]
        if not rows_d:
            return {"week_start": week_start, "trade_count": 0}

        trade_count = len(rows_d)
        wins = sum(1 for r in rows_d if (r.get("trade_pnl") or 0) > 0)
        win_rate = wins / trade_count if trade_count else 0.0

        # realized edge = actual PnL / invested capital
        total_pnl = sum(r.get("trade_pnl") or 0.0 for r in rows_d)
        total_size = sum(
            r.get("trade_entry_price", 0.5) or 0.5 for r in rows_d
        )  # approx
        realized_edge = total_pnl / total_size if total_size > 0 else 0.0

        # predicted edge = avg |model_prob - mid_price| where we had a signal
        pred_edges = [
            abs((r.get("model_prob") or 0.5) - (r.get("mid_price") or 0.5))
            for r in rows_d
            if r.get("model_prob") is not None
        ]
        predicted_edge = sum(pred_edges) / len(pred_edges) if pred_edges else 0.0

        return {
            "week_start": week_start,
            "realized_edge": round(realized_edge, 4),
            "predicted_edge": round(predicted_edge, 4),
            "trade_count": trade_count,
            "win_rate": round(win_rate, 4),
        }

    # ------------------------------------------------------------------ #
    # Internal                                                             #
    # ------------------------------------------------------------------ #

    async def _loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._check_interval_s)
            try:
                stats = await self.compute_current_week_stats()
                if stats.get("trade_count", 0) > 0:
                    await self.record_week(
                        week_start=stats["week_start"],
                        realized_edge=stats.get("realized_edge", 0.0),
                        predicted_edge=stats.get("predicted_edge", 0.0),
                        trade_count=stats["trade_count"],
                        win_rate=stats.get("win_rate", 0.0),
                        avg_repricing_ms=0.0,
                    )
                else:
                    await self._check_decay()
            except Exception as exc:
                log.error("EdgeMonitor loop error: %s", exc)

    async def _check_decay(self) -> None:
        weeks = await self.get_recent_weeks(n=3)
        if len(weeks) < 3:
            return
        # weeks sorted DESC — oldest is weeks[2]
        edges = [w.get("realized_edge", 0.0) for w in weeks]
        # declining = each week lower than the previous (chronologically)
        if edges[0] < edges[1] < edges[2]:
            msg = (
                f"Edge decay: 3 consecutive weeks declining "
                f"({edges[2]:.4f} → {edges[1]:.4f} → {edges[0]:.4f})"
            )
            log.warning("EDGE DECAY ALERT: %s", msg)
            if self._alert_cb:
                try:
                    await self._alert_cb("edge_decay", {"message": msg, "weeks": weeks[:3]})
                except Exception as exc:
                    log.error("Alert callback failed: %s", exc)

    async def _compute_market_efficiency(self, week_start: str) -> float:
        """Avg time (ms) for CLOB mid_price to move >0.05% after Chainlink tick.

        Approximated: we don't have tick-level timestamps aligned, so we use
        market_move_speed from windows table as proxy.
        """
        week_end = (
            datetime.strptime(week_start, "%Y-%m-%d") + timedelta(days=7)
        ).strftime("%Y-%m-%d")
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                """SELECT AVG(market_move_speed) FROM windows
                   WHERE snapshot_time >= ? AND snapshot_time < ?
                     AND ABS(displacement_pct) > ?""",
                (week_start, week_end, _CLOB_MOVE_THRESHOLD),
            ) as cur:
                row = await cur.fetchone()
        val = row[0] if row and row[0] is not None else 0.0
        return round(float(val), 4)
