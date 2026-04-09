from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from datetime import datetime, timezone
from typing import Any

from .config import load_config
from .data.db import Database
from .data.logger import DailyStatsAggregator, TickLogger, WindowLogger
from .engine.decision import DecisionEngine
from .engine.scorer import RuleScorer
from .engine.snapshot_builder import build_snapshot, parse_strike
from .engine.window_tracker import WindowLifecycleManager, WindowTracker
from .execution.adaptive import AdaptiveExecutor
from .execution.order_manager import OrderManager
from .execution.ramp import SizeRamp
from .feeds.base import PriceUpdate
from .feeds.chainlink import ChainlinkFeed
from .feeds.polymarket import MarketInfo, OrderbookSnapshot, PolymarketClient
from .feeds.spot_feed import SpotFeed
from .kill_switch import KillSwitch
from .monitoring.alerts import AlertManager
from .monitoring.edge_monitor import EdgeMonitor
from .monitoring.live_monitor import LiveMonitor
from .monitoring.metrics import MetricsCollector
from .risk.risk_manager import RiskManager


def _setup_logging() -> None:
    class _Json(logging.Formatter):
        def format(self, r: logging.LogRecord) -> str:
            p: dict[str, Any] = {"ts": datetime.now(timezone.utc).isoformat(),
                                  "level": r.levelname, "logger": r.name, "msg": r.getMessage()}
            if r.exc_info:
                p["exc"] = self.formatException(r.exc_info)
            return json.dumps(p)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(_Json())
    root = logging.getLogger()
    root.addHandler(h)
    root.setLevel(logging.INFO)


log = logging.getLogger(__name__)


class KalBot:
    def __init__(self) -> None:
        self._cfg = load_config()
        self._db = Database(self._cfg.data.db_path)
        self._shutdown = asyncio.Event()

        self._chainlink: ChainlinkFeed | None = None
        self._spot: SpotFeed | None = None
        self._poly: PolymarketClient | None = None

        self._tracker = WindowTracker()
        self._lifecycle = WindowLifecycleManager(self._tracker)
        self._scorer: RuleScorer | None = None
        self._decision: DecisionEngine | None = None
        self._risk: RiskManager | None = None
        self._order_mgr: OrderManager | None = None
        self._adaptive: AdaptiveExecutor | None = None
        self._win_logger: WindowLogger | None = None
        self._tick_logger: TickLogger | None = None

        self._cl_price: float = 0.0
        self._cl_ts: datetime | None = None
        self._spot_price: float = 0.0
        self._spot_source: str = "none"

        # feeds_ready: all three feeds must have ≥1 update before scoring
        self._polymarket_ready: bool = False
        self._feeds_ready_logged: bool = False  # guards the first-time log

        self._metrics = MetricsCollector()
        self._alerts: AlertManager | None = None
        self._edge_monitor: EdgeMonitor | None = None
        self._kill_switch: KillSwitch = KillSwitch()
        self._ramp: SizeRamp | None = None
        self._live_monitor: LiveMonitor | None = None

        self._daily_stats: DailyStatsAggregator | None = None
        self._current_market: MarketInfo | None = None
        self._last_ob: OrderbookSnapshot | None = None
        self._traded_windows: set[str] = set()
        self._logged_intervals: set[int] = set()
        self._window_signal: str = "PASS"
        self._window_entry: float | None = None
        self._primary_bucket_logged: bool = False

    # ------------------------------------------------------------------ #
    # Callbacks                                                            #
    # ------------------------------------------------------------------ #

    @property
    def _feeds_ready(self) -> bool:
        """True only when all three feeds have delivered ≥1 update.

        Derived directly from each feed's _last_update_time so readiness is
        guaranteed the moment a price lands in _emit(), independent of whether
        the _on_price callback chain fires without error.
        """
        cl_ready = self._chainlink is not None and self._chainlink._last_update_time is not None
        sp_ready = self._spot is not None and self._spot._last_update_time is not None
        if cl_ready and sp_ready and self._polymarket_ready and not self._feeds_ready_logged:
            self._feeds_ready_logged = True
            log.info(
                "feeds_ready — chainlink last=%.2fs ago, spot last=%.2fs ago, polymarket ready",
                (datetime.now(timezone.utc) - self._chainlink._last_update_time).total_seconds()
                if self._chainlink and self._chainlink._last_update_time else -1,
                (datetime.now(timezone.utc) - self._spot._last_update_time).total_seconds()
                if self._spot and self._spot._last_update_time else -1,
            )
        return cl_ready and sp_ready and self._polymarket_ready

    @property
    def _network_healthy(self) -> bool:
        """False if any feed has an active gaierror (DNS failure)."""
        cl_err = self._chainlink._network_error if self._chainlink else False
        sp_err = self._spot._network_error if self._spot else False
        return not cl_err and not sp_err

    async def _on_chainlink_price(self, update: PriceUpdate) -> None:
        self._cl_price = update.price
        self._cl_ts = update.timestamp
        if self._lifecycle.current_market_id and not self._lifecycle.is_trading_blocked():
            self._tracker.update(update.price, update.timestamp)
        if self._tick_logger and self._lifecycle.current_market_id:
            self._tick_logger.record(f"{self._lifecycle.current_market_id}_ticks",
                                     "chainlink", update.price, update.timestamp)
        stale = self._chainlink.is_stale if self._chainlink else False
        await self._metrics.update_chainlink(ok=True, stale=stale, last_update=update.timestamp)

    async def _on_spot_price(self, update: PriceUpdate) -> None:
        self._spot_price = update.price
        self._spot_source = update.source
        if self._tick_logger and self._lifecycle.current_market_id:
            self._tick_logger.record(f"{self._lifecycle.current_market_id}_ticks",
                                     update.source, update.price, update.timestamp)
        await self._metrics.update_spot(ok=True, source=update.source, last_update=update.timestamp)

    # ------------------------------------------------------------------ #
    # Market monitor                                                       #
    # ------------------------------------------------------------------ #

    async def _polymarket_monitor(self) -> None:
        assert self._poly is not None
        while not self._shutdown.is_set():
            markets = self._poly.active_markets
            if markets:
                if not self._polymarket_ready:
                    self._polymarket_ready = True
                    log.info("Polymarket ready — %d market(s)", len(markets))
                await self._metrics.update_polymarket(ok=True, market_count=len(markets))
                if not self._feeds_ready:
                    await asyncio.sleep(5)
                    continue
                nearest = self._poly.get_nearest_market()
                if nearest and self._cl_price and nearest.end_date.replace(
                    tzinfo=timezone.utc if nearest.end_date.tzinfo is None else nearest.end_date.tzinfo
                ) > datetime.now(timezone.utc):
                    if self._lifecycle.on_market_discovered(
                        nearest.market_id, nearest.condition_id,
                        nearest.end_date, self._cl_price,
                    ):
                        self._current_market = nearest
                        self._logged_intervals.clear()
                        self._window_signal = "PASS"
                        self._window_entry = None
                        self._primary_bucket_logged = False
                        ob = await self._poly.fetch_orderbook(nearest)
                        if ob:
                            self._last_ob = ob
                        log.info("Window started: %s", nearest.question)
                        await self._metrics.update_window(
                            market_id=nearest.market_id,
                            question=nearest.question,
                            strike_price=parse_strike(nearest.question) or 0.0,
                            elapsed_seconds=0,
                            remaining_seconds=300,
                            signal="PASS",
                            traded=False,
                        )
                        await self._metrics.increment_session_windows()

            if self._lifecycle.check_expiry() and self._current_market:
                await self._settle_window(self._current_market)
                self._current_market = None
            await asyncio.sleep(5)

    # ------------------------------------------------------------------ #
    # Snapshot + trading loop                                              #
    # ------------------------------------------------------------------ #

    async def _snapshot_task(self) -> None:
        while not self._shutdown.is_set():
            await asyncio.sleep(0.5)
            if not self._current_market or not self._feeds_ready:
                continue
            if not self._network_healthy:
                log.warning("Network unhealthy (gaierror) — pausing trading")
                continue
            if self._lifecycle.is_trading_blocked():
                continue
            # Phase 2 spec: Chainlink OR Polymarket stale >10s → pause trading
            if self._chainlink and self._chainlink.is_stale:
                log.warning("Chainlink stale — pausing trading")
                continue
            elapsed = int(self._tracker.elapsed_seconds)
            for bucket in self._cfg.data.snapshot_at_seconds:
                if elapsed >= bucket and bucket not in self._logged_intervals:
                    self._logged_intervals.add(bucket)
                    await self._do_snapshot(bucket)

    async def _do_snapshot(self, bucket: int) -> None:
        market = self._current_market
        assert self._win_logger and self._scorer and self._decision and self._risk
        assert self._order_mgr and self._adaptive
        if not market:
            return

        snap = build_snapshot(market, bucket, self._tracker, self._cl_price,
                              self._spot, self._spot_price, self._spot_source, self._last_ob)
        if snap is None:
            return

        await self._metrics.update_window(
            elapsed_seconds=snap.elapsed_seconds,
            remaining_seconds=snap.remaining_seconds,
            current_price=snap.snapshot_price,
            displacement_pct=snap.displacement_pct,
            direction=snap.direction,
            direction_consistency=snap.direction_consistency,
            yes_bid=snap.yes_bid,
            yes_ask=snap.yes_ask,
            mid_price=snap.mid_price,
            spread=snap.spread,
        )

        in_window = self._cfg.engine.min_elapsed_seconds <= bucket <= self._cfg.engine.max_elapsed_seconds
        not_traded = market.condition_id not in self._traded_windows

        if in_window and not_traded:
            score = await self._scorer.score(snap)
            snap.rule_signal = score.signal
            await self._metrics.update_window(
                signal=score.signal,
                model_prob=snap.model_prob,
            )
            dec = self._decision.decide(score, snap)

            # First scored snapshot per window is the primary training row
            is_first_scored = not self._primary_bucket_logged
            self._primary_bucket_logged = True

            if dec.action == "TRADE":
                side = dec.side
                assert side is not None
                ob = self._last_ob
                entry = (ob.yes_ask if side == "YES" else ob.no_ask) if ob else 0.55
                size = dec.size_usd or self._cfg.execution.default_order_size_usd
                wid = f"{market.condition_id}_{bucket}"

                strategy = dec.strategy or "taker"
                if strategy == "adaptive":
                    # Spec: limit at (mid - spread/4) for YES; mirror for NO
                    if ob is not None:
                        if side == "YES":
                            limit_price = round(ob.mid_price - ob.spread / 4, 4)
                        else:
                            limit_price = round((1.0 - ob.mid_price) - ob.spread / 4, 4)
                    else:
                        limit_price = round(entry - 0.01, 4)

                    def _get_remaining() -> int:
                        end = self._lifecycle._current_end_time
                        if end is None:
                            return 0
                        if end.tzinfo is None:
                            end = end.replace(tzinfo=timezone.utc)
                        return max(0, int((end - datetime.now(timezone.utc)).total_seconds()))

                    def _get_direction() -> int:
                        feats = self._tracker.get_features(self._cl_price)
                        return feats.direction if feats else 0

                    order_id, fill = await self._adaptive.execute(
                        wid, side, limit_price, size,
                        score.edge_estimate,
                        get_remaining=_get_remaining,
                        get_btc_direction=_get_direction,
                        initial_direction=snap.direction,
                    )
                else:
                    order_id, fill = await self._order_mgr.place_order(wid, side, strategy, entry, size)

                # Phase 10: reconcile live fill vs expected (fire-and-forget)
                if (
                    self._live_monitor is not None
                    and self._cfg.execution.mode == "live"
                    and order_id
                ):
                    asyncio.create_task(
                        self._live_monitor.reconcile_fill(order_id, entry, size),
                        name=f"Reconcile_{order_id}",
                    )

                self._traded_windows.add(market.condition_id)
                self._window_signal = side
                self._window_entry = fill if fill is not None else entry
                snap.window_id = wid
                snap.traded = True
                snap.trade_side = side
                snap.trade_entry_price = entry
                snap.trade_fill_price = fill
                self._risk.register_trade(size)
                await self._win_logger.log_snapshot(snap, is_primary=True)
                await self._metrics.open_position(wid, side, fill or entry, size)
                if self._alerts:
                    await self._alerts.trade(side, fill or entry, size, market.question)
                return
            elif dec.pass_reason:
                log.info("PASS: %s", dec.pass_reason)

            snap.window_id = f"{market.condition_id}_{bucket}"
            await self._win_logger.log_snapshot(snap, is_primary=is_first_scored)
            return

        snap.window_id = f"{market.condition_id}_{bucket}"
        await self._win_logger.log_snapshot(snap)

    # ------------------------------------------------------------------ #
    # Settlement                                                           #
    # ------------------------------------------------------------------ #

    async def _settle_window(self, market: MarketInfo) -> None:
        assert self._win_logger and self._order_mgr and self._risk
        price = self._cl_price
        feats = self._tracker.get_features(price)
        # Fallback: derive open_price from tracker so we never compare price > 0 incorrectly
        denom = 1.0 + (feats.displacement_pct / 100.0) if feats else 1.0
        open_price = price / denom if denom != 0 else price
        strike = parse_strike(market.question) or open_price
        outcome = "YES" if price > strike else "NO"

        fill_wid: str | None = None
        for b in self._cfg.data.snapshot_at_seconds:
            wid = f"{market.condition_id}_{b}"
            if self._order_mgr.get_fill(wid):
                fill_wid = wid
                break

        # Always stamp settlement_outcome on every row for this condition (ML training)
        await self._db.update_settlement_all(market.condition_id, outcome, price)

        pnl: float | None = None
        if fill_wid:
            pnl = await self._order_mgr.settle_positions(fill_wid, outcome)
            pos = self._order_mgr.get_fill(fill_wid)
            if pos and pnl is not None:
                await self._db.update_trade_pnl(fill_wid, pnl)
                log.info(
                    "Settlement | id=%s price=%.2f strike=%.2f outcome=%s pnl=%.4f",
                    fill_wid, price, strike, outcome, pnl,
                )
                self._risk.register_settlement(pnl)
                await self._metrics.close_position(fill_wid, pnl)
                if self._alerts:
                    await self._alerts.settlement(outcome, pnl, market.question)
                rs = self._risk.state
                if rs.circuit_breaker_active:
                    await self._metrics.update_circuit_breaker(
                        True, rs.circuit_breaker_reason
                    )
                    if self._alerts:
                        await self._alerts.circuit_breaker(rs.circuit_breaker_reason)
        else:
            log.info(
                "Settlement (untraded) | condition=%s price=%.2f strike=%.2f outcome=%s",
                market.condition_id[:16], price, strike, outcome,
            )

        disp = (price - strike) / strike * 100 if strike else 0.0
        cons = feats.direction_consistency if feats else 0.0
        trade_str = (f" | TRADE @ ${self._window_entry:.2f} | Outcome: {outcome}"
                     f" | P&L: {pnl:+.2f}" if pnl is not None else " | P&L: N/A"
                     ) if self._window_signal != "PASS" and self._window_entry is not None else ""
        log.info("Window %s | BTC %+.2f%% | Consistency: %.2f | Signal: %s%s",
                 market.end_date.strftime("%H:%M"), disp, cons, self._window_signal, trade_str)

    # ------------------------------------------------------------------ #
    # Metrics sync                                                         #
    # ------------------------------------------------------------------ #

    async def _metrics_sync_task(self) -> None:
        """Periodically syncs windows_collected from DB (ML progress bar)."""
        while not self._shutdown.is_set():
            await asyncio.sleep(60)
            try:
                async with __import__("aiosqlite").connect(self._db._path) as db:
                    async with db.execute(
                        "SELECT COUNT(*) FROM windows WHERE settlement_outcome IS NOT NULL AND is_primary=1"
                    ) as cur:
                        row = await cur.fetchone()
                count = row[0] if row else 0
                await self._metrics.update_windows_collected(count)
            except Exception as exc:
                log.error("metrics_sync failed: %s", exc)

    # ------------------------------------------------------------------ #
    # Dashboard server                                                     #
    # ------------------------------------------------------------------ #

    async def _dashboard_server(self) -> None:
        import uvicorn
        from .dashboard.app import create_app

        app = create_app(cfg=self._cfg, metrics=self._metrics)
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=8080,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        log.info("Dashboard starting at http://127.0.0.1:8080")
        try:
            await server.serve()
        except asyncio.CancelledError:
            server.should_exit = True

    # ------------------------------------------------------------------ #
    # Start / stop                                                         #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        log.info("KalBot starting in %s mode", self._cfg.execution.mode)
        await self._db.init()

        fc = self._cfg.feeds
        self._chainlink = ChainlinkFeed(
            ws_url=fc.chainlink_ws_url,
            ping_interval_s=fc.chainlink_ping_interval_s,
            stale_threshold_s=fc.chainlink_stale_threshold_s,
            on_price=self._on_chainlink_price,
            source=fc.chainlink_source,
            poll_interval_s=fc.chainlink_poll_interval_s,
        )
        self._spot = SpotFeed(poll_interval_s=fc.spot_poll_interval_s, on_price=self._on_spot_price)
        self._poly = PolymarketClient(gamma_url=fc.gamma_api_url, clob_url=fc.clob_api_url,
            discovery_interval_s=fc.market_discovery_interval_s, series_ticker=fc.market_series_ticker)

        self._scorer = RuleScorer(self._tracker, fc.market_series_ticker, fc.chainlink_stale_threshold_s)
        self._risk = RiskManager(self._cfg)
        self._decision = DecisionEngine(self._cfg, self._risk)
        self._order_mgr = OrderManager(self._cfg, self._db)
        self._adaptive = AdaptiveExecutor(self._order_mgr, self._cfg.execution)
        self._win_logger = WindowLogger(self._db, self._cfg.data.snapshot_at_seconds)
        self._tick_logger = TickLogger(self._db)
        self._daily_stats = DailyStatsAggregator(self._db)

        self._alerts = AlertManager(
            discord_webhook_url=self._cfg.discord_webhook_url,
            telegram_bot_token=self._cfg.telegram_bot_token,
            telegram_chat_id=self._cfg.telegram_chat_id,
        )
        self._edge_monitor = EdgeMonitor(
            db_path=self._cfg.data.db_path,
            alert_callback=self._alerts.send,
        )

        # Phase 10: live-mode wiring
        if self._cfg.execution.mode == "live":
            self._ramp = SizeRamp()
            self._live_monitor = LiveMonitor(
                clob_url=self._cfg.feeds.clob_api_url,
                api_key=self._cfg.polymarket_api_key,
                mode="live",
            )
            self._kill_switch.set_order_manager(self._order_mgr)
            self._kill_switch.set_alerts(self._alerts)
            self._order_mgr.set_kill_switch(self._kill_switch)
            self._order_mgr.set_ramp(self._ramp)
            self._order_mgr.set_risk_manager(self._risk)
            if self._kill_switch.is_engaged():
                log.critical(
                    "KillSwitch flag present at startup — bot will NOT trade. "
                    "Remove data/kill_switch.flag to re-enable."
                )

        self._chainlink.start()
        self._spot.start()
        self._poly.start()
        self._tick_logger.start()
        self._daily_stats.start()
        self._edge_monitor.start()
        if self._live_monitor:
            self._live_monitor.start()
        if self._cfg.execution.mode == "live":
            self._kill_switch.start_monitor()

        tasks = [
            asyncio.create_task(self._polymarket_monitor(), name="PolymarketMonitor"),
            asyncio.create_task(self._snapshot_task(), name="SnapshotTask"),
            asyncio.create_task(self._dashboard_server(), name="Dashboard"),
            asyncio.create_task(self._metrics_sync_task(), name="MetricsSync"),
        ]
        log.info("All feeds started")
        await self._shutdown.wait()

        for t in tasks:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

        if self._adaptive:
            self._adaptive.cancel_all()
        await self._chainlink.stop()
        await self._spot.stop()
        await self._poly.stop()
        await self._tick_logger.stop()
        if self._daily_stats:
            await self._daily_stats.stop()
        if self._edge_monitor:
            await self._edge_monitor.stop()
        if self._live_monitor:
            await self._live_monitor.stop()
        await self._kill_switch.stop_monitor()
        if self._live_monitor:
            report = self._live_monitor.reconciliation_report()
            log.info("LiveMonitor final reconciliation: %s", report)
        log.info("KalBot shut down cleanly")

    def request_shutdown(self) -> None:
        log.info("Shutdown requested")
        self._shutdown.set()


async def _main() -> None:
    _setup_logging()
    bot = KalBot()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, bot.request_shutdown)

    # Phase 10: engage kill switch on uncaught asyncio exception
    def _uncaught_exception_handler(loop: asyncio.AbstractEventLoop, ctx: dict) -> None:
        exc = ctx.get("exception")
        msg = ctx.get("message", "unknown")
        log.critical("Uncaught asyncio exception: %s — %s", type(exc).__name__ if exc else "?", msg)
        if bot._kill_switch is not None and bot._cfg.execution.mode == "live":
            asyncio.ensure_future(
                bot._kill_switch.engage(f"uncaught exception: {type(exc).__name__}: {msg}"),
                loop=loop,
            )
        bot.request_shutdown()

    loop.set_exception_handler(_uncaught_exception_handler)
    await bot.start()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
