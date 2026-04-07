from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .db import Database
from ..types import WindowSnapshot

log = logging.getLogger(__name__)


class WindowLogger:
    """Logs every 5-min window at configured snapshot intervals.

    Snapshots are taken at elapsed_seconds in [120, 150, 180, 210, 240].
    The snapshot taken at the decision point is tagged is_primary=True.
    Every window is logged — traded or not. 288 samples/day.
    """

    def __init__(self, db: Database, snapshot_at_seconds: list[int]) -> None:
        self._db = db
        self._snapshot_at = sorted(snapshot_at_seconds)

    async def log_snapshot(self, snapshot: WindowSnapshot, is_primary: bool = False) -> None:
        """Insert or replace window row. Called at each snapshot interval."""
        try:
            await self._db.insert_window(snapshot, is_primary=is_primary)
            log.info(
                "Window logged | id=%s elapsed=%ds primary=%s traded=%s signal=%s",
                snapshot.window_id,
                snapshot.elapsed_seconds,
                is_primary,
                snapshot.traded,
                snapshot.rule_signal,
            )
        except Exception as exc:
            log.error("Failed to log window %s: %s", snapshot.window_id, exc)

    async def on_settlement(
        self,
        window_id: str,
        settlement_price: float,
        strike_price: float,
        traded: bool,
        trade_side: str | None,
        trade_entry_price: float | None,
        size_usd: float | None,
    ) -> None:
        """Called when window closes. Captures settlement and computes PnL."""
        outcome = "YES" if settlement_price > strike_price else "NO"

        pnl: float | None = None
        if traded and trade_side and trade_entry_price is not None and size_usd is not None:
            pnl = _compute_pnl(trade_side, trade_entry_price, outcome, size_usd)

        try:
            await self._db.update_settlement(window_id, outcome, settlement_price)
            if pnl is not None:
                await self._db.update_trade_pnl(window_id, pnl)
            log.info(
                "Settlement | id=%s price=%.2f strike=%.2f outcome=%s pnl=%s",
                window_id,
                settlement_price,
                strike_price,
                outcome,
                f"{pnl:.4f}" if pnl is not None else "N/A",
            )
        except Exception as exc:
            log.error("Failed to record settlement for %s: %s", window_id, exc)


def _compute_pnl(
    side: str,
    entry_price: float,
    outcome: str,
    size_usd: float,
) -> float:
    """Binary market PnL. Prices are in [0,1] (e.g. 0.55 = $0.55 per $1 contract).

    If side=YES, bought YES at entry_price per dollar of contract.
      - Outcome YES: pnl = (1.0 - entry_price) * contracts
      - Outcome NO:  pnl = -entry_price * contracts
    contracts = size_usd / entry_price
    """
    if entry_price <= 0:
        return 0.0
    contracts = size_usd / entry_price
    if side == "YES":
        return (1.0 - entry_price) * contracts if outcome == "YES" else -entry_price * contracts
    else:  # side == "NO"
        return (1.0 - entry_price) * contracts if outcome == "NO" else -entry_price * contracts


class TickLogger:
    """Batches Chainlink + spot price ticks and flushes to DB every 5s."""

    _FLUSH_INTERVAL_S = 5.0

    def __init__(self, db: Database) -> None:
        self._db = db
        self._batch: list[tuple[str, str, float, datetime]] = []  # (window_id, source, price, ts)
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._flush_loop(), name="TickLogger")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._flush()

    def record(self, window_id: str, source: str, price: float, ts: datetime | None = None) -> None:
        """Non-blocking — just appends to in-memory batch."""
        if ts is None:
            ts = datetime.now(timezone.utc)
        self._batch.append((window_id, source, price, ts))

    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._FLUSH_INTERVAL_S)
            await self._flush()

    async def _flush(self) -> None:
        if not self._batch:
            return
        batch, self._batch = self._batch, []
        try:
            await self._db.insert_ticks_batch(batch)
            log.debug("TickLogger flushed %d ticks", len(batch))
        except Exception as exc:
            log.error("TickLogger flush failed: %s", exc)


class DailyStatsAggregator:
    """Computes and stores daily_stats at midnight UTC."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._midnight_loop(), name="DailyStats")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _midnight_loop(self) -> None:
        while self._running:
            now = datetime.now(timezone.utc)
            seconds_to_midnight = (
                (24 - now.hour) * 3600 - now.minute * 60 - now.second
            )
            await asyncio.sleep(seconds_to_midnight)
            if not self._running:
                break
            # after sleeping to midnight, aggregate the day that just ended
            date_str = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
            await self._aggregate(date_str)

    async def _aggregate(self, date_str: str) -> None:
        try:
            rows = await self._db.get_windows_for_date(date_str)
            if not rows:
                log.info("DailyStats: no windows for %s", date_str)
                return

            total = len(rows)
            traded = [r for r in rows if r.get("traded")]
            wins = sum(1 for r in traded if r.get("trade_pnl", 0) > 0)
            losses = sum(1 for r in traded if r.get("trade_pnl", 0) < 0)
            gross_pnl = sum(r.get("trade_pnl") or 0 for r in traded)
            # no fee data yet — net_pnl = gross_pnl for now
            net_pnl = gross_pnl
            fill_rate = len(traded) / total if total else 0.0
            # maker_pct not tracked yet
            maker_pct = 0.0

            await self._db.upsert_daily_stats(
                date=date_str,
                total_windows=total,
                traded_windows=len(traded),
                wins=wins,
                losses=losses,
                gross_pnl=gross_pnl,
                net_pnl=net_pnl,
                fill_rate=fill_rate,
                maker_pct=maker_pct,
            )
            log.info(
                "DailyStats %s | windows=%d traded=%d wins=%d pnl=%.4f",
                date_str, total, len(traded), wins, net_pnl,
            )
        except Exception as exc:
            log.error("DailyStats aggregation failed for %s: %s", date_str, exc)
