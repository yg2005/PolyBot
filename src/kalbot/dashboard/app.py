from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import KalbotConfig, load_config
from ..data.db import Database
from ..monitoring.edge_monitor import EdgeMonitor
from ..monitoring.metrics import BotMetrics, MetricsCollector

log = logging.getLogger(__name__)

_HERE = Path(__file__).parent
_TEMPLATES_DIR = _HERE / "templates"
_STATIC_DIR = _HERE / "static"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def create_app(
    cfg: KalbotConfig | None = None,
    metrics: MetricsCollector | None = None,
) -> FastAPI:
    if cfg is None:
        cfg = load_config()

    _metrics = metrics or MetricsCollector()
    _db = Database(cfg.data.db_path)
    _edge_monitor = EdgeMonitor(cfg.data.db_path)

    app = FastAPI(title="KalBot Dashboard", docs_url=None, redoc_url=None)

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # ------------------------------------------------------------------ #
    # / — live view                                                        #
    # ------------------------------------------------------------------ #

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        m = _metrics.snapshot()
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "m": m,
                "poll_ms": 5000,
            },
        )

    @app.get("/htmx/live", response_class=HTMLResponse)
    async def htmx_live(request: Request) -> Response:
        m = _metrics.snapshot()
        return templates.TemplateResponse(
            "partials/live.html",
            {"request": request, "m": m},
        )

    # ------------------------------------------------------------------ #
    # /performance                                                         #
    # ------------------------------------------------------------------ #

    @app.get("/performance", response_class=HTMLResponse)
    async def performance(request: Request) -> Response:
        stats = await _get_daily_stats(_db)
        weekly = await _edge_monitor.get_recent_weeks(8)
        return templates.TemplateResponse(
            "performance.html",
            {
                "request": request,
                "stats": stats,
                "weekly": weekly,
                "poll_ms": 30000,
            },
        )

    @app.get("/htmx/performance", response_class=HTMLResponse)
    async def htmx_performance(request: Request) -> Response:
        stats = await _get_daily_stats(_db)
        weekly = await _edge_monitor.get_recent_weeks(8)
        return templates.TemplateResponse(
            "partials/performance.html",
            {"request": request, "stats": stats, "weekly": weekly},
        )

    # ------------------------------------------------------------------ #
    # /data                                                                #
    # ------------------------------------------------------------------ #

    @app.get("/data", response_class=HTMLResponse)
    async def data_page(request: Request) -> Response:
        m = _metrics.snapshot()
        dist = await _get_feature_distribution(_db)
        return templates.TemplateResponse(
            "data.html",
            {
                "request": request,
                "m": m,
                "dist": dist,
                "poll_ms": 30000,
            },
        )

    @app.get("/htmx/data", response_class=HTMLResponse)
    async def htmx_data(request: Request) -> Response:
        m = _metrics.snapshot()
        dist = await _get_feature_distribution(_db)
        return templates.TemplateResponse(
            "partials/data.html",
            {"request": request, "m": m, "dist": dist},
        )

    # ------------------------------------------------------------------ #
    # /model                                                               #
    # ------------------------------------------------------------------ #

    @app.get("/model", response_class=HTMLResponse)
    async def model_page(request: Request) -> Response:
        m = _metrics.snapshot()
        model_info = await _get_active_model(_db)
        backtest = await _get_backtest_summary(_db)
        return templates.TemplateResponse(
            "model.html",
            {
                "request": request,
                "m": m,
                "model": model_info,
                "backtest": backtest,
                "poll_ms": 30000,
            },
        )

    @app.get("/htmx/model", response_class=HTMLResponse)
    async def htmx_model(request: Request) -> Response:
        m = _metrics.snapshot()
        model_info = await _get_active_model(_db)
        backtest = await _get_backtest_summary(_db)
        return templates.TemplateResponse(
            "partials/model.html",
            {"request": request, "m": m, "model": model_info, "backtest": backtest},
        )

    # ------------------------------------------------------------------ #
    # /settings                                                            #
    # ------------------------------------------------------------------ #

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> Response:
        m = _metrics.snapshot()
        return templates.TemplateResponse(
            "settings.html",
            {
                "request": request,
                "cfg": cfg,
                "m": m,
                "poll_ms": 30000,
            },
        )

    @app.post("/settings/kill")
    async def kill_switch(request: Request) -> JSONResponse:
        """Activate circuit breaker via dashboard kill switch."""
        log.critical("Kill switch activated via dashboard")
        await _metrics.update_circuit_breaker(True, "manual kill switch")
        return JSONResponse({"status": "ok", "message": "Kill switch activated"})

    @app.post("/settings/retrain")
    async def trigger_retrain(request: Request) -> JSONResponse:
        """Signal that model retrain is requested (consumed by bot loop)."""
        log.info("Retrain requested via dashboard")
        return JSONResponse({"status": "ok", "message": "Retrain queued"})

    # ------------------------------------------------------------------ #
    # /api — JSON endpoints                                                #
    # ------------------------------------------------------------------ #

    @app.get("/api/metrics")
    async def api_metrics() -> JSONResponse:
        m = _metrics.snapshot()
        return JSONResponse(_metrics_to_dict(m))

    @app.get("/api/windows/recent")
    async def api_recent_windows() -> JSONResponse:
        rows = await _db.get_recent_windows(50)
        return JSONResponse(rows)

    return app


# ------------------------------------------------------------------ #
# DB helpers                                                           #
# ------------------------------------------------------------------ #


async def _get_daily_stats(db: Database) -> list[dict]:
    try:
        async with aiosqlite.connect(db._path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT * FROM daily_stats ORDER BY date DESC LIMIT 30"
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.error("_get_daily_stats failed: %s", exc)
        return []


async def _get_feature_distribution(db: Database) -> dict[str, Any]:
    """Returns basic stats for key features across all settled windows."""
    try:
        async with aiosqlite.connect(db._path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                """SELECT
                     COUNT(*) as total,
                     AVG(displacement_pct) as avg_disp,
                     AVG(direction_consistency) as avg_dir_con,
                     AVG(elapsed_seconds) as avg_elapsed,
                     SUM(CASE WHEN traded=1 THEN 1 ELSE 0 END) as traded_count,
                     SUM(CASE WHEN settlement_outcome='YES' THEN 1 ELSE 0 END) as yes_count,
                     SUM(CASE WHEN settlement_outcome='NO' THEN 1 ELSE 0 END) as no_count
                   FROM windows
                   WHERE settlement_outcome IS NOT NULL"""
            ) as cur:
                row = await cur.fetchone()
        return dict(row) if row else {}
    except Exception as exc:
        log.error("_get_feature_distribution failed: %s", exc)
        return {}


async def _get_active_model(db: Database) -> dict | None:
    try:
        async with aiosqlite.connect(db._path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT * FROM model_registry WHERE is_active=1 LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        m = dict(row)
        # Parse features_used JSON array → list
        try:
            m["features_list"] = json.loads(m.get("features_used") or "[]")
        except (json.JSONDecodeError, TypeError):
            m["features_list"] = []
        # Parse hyperparams JSON → extract feature_importance
        try:
            hp = json.loads(m.get("hyperparams") or "{}")
            m["feature_importance"] = hp.get("feature_importance", {})
            m["xgb_params"] = hp.get("params", hp)  # backwards compat: old rows stored params directly
        except (json.JSONDecodeError, TypeError):
            m["feature_importance"] = {}
            m["xgb_params"] = {}
        return m
    except Exception as exc:
        log.error("_get_active_model failed: %s", exc)
        return None


async def _get_backtest_summary(db: Database) -> dict[str, Any]:
    """Backtest proxy: aggregate daily_stats over all recorded history."""
    try:
        async with aiosqlite.connect(db._path) as conn:
            async with conn.execute(
                """SELECT
                     COUNT(*) as trading_days,
                     SUM(traded_windows) as total_trades,
                     SUM(wins) as total_wins,
                     SUM(losses) as total_losses,
                     SUM(net_pnl) as total_pnl,
                     MIN(net_pnl) as worst_day,
                     MAX(net_pnl) as best_day,
                     AVG(net_pnl) as avg_daily_pnl
                   FROM daily_stats"""
            ) as cur:
                row = await cur.fetchone()
        if not row or not row[0]:
            return {}
        r = {
            "trading_days": row[0],
            "total_trades": row[1] or 0,
            "total_wins": row[2] or 0,
            "total_losses": row[3] or 0,
            "total_pnl": round(row[4] or 0.0, 4),
            "worst_day": round(row[5] or 0.0, 4),
            "best_day": round(row[6] or 0.0, 4),
            "avg_daily_pnl": round(row[7] or 0.0, 4),
        }
        r["win_rate"] = round(r["total_wins"] / r["total_trades"], 4) if r["total_trades"] else 0.0
        return r
    except Exception as exc:
        log.error("_get_backtest_summary failed: %s", exc)
        return {}


def _metrics_to_dict(m: BotMetrics) -> dict:
    return {
        "today_pnl": m.today_pnl,
        "today_trades": m.today_trades,
        "today_wins": m.today_wins,
        "today_losses": m.today_losses,
        "session_windows": m.session_windows,
        "windows_collected": m.windows_collected,
        "windows_target": m.windows_target,
        "circuit_breaker_active": m.circuit_breaker_active,
        "circuit_breaker_reason": m.circuit_breaker_reason,
        "scorer_mode": m.scorer_mode,
        "model_id": m.model_id,
        "model_auc": m.model_auc,
        "feed_health": {
            "chainlink_ok": m.feed_health.chainlink_ok,
            "chainlink_stale": m.feed_health.chainlink_stale,
            "spot_ok": m.feed_health.spot_ok,
            "spot_source": m.feed_health.spot_source,
            "polymarket_ok": m.feed_health.polymarket_ok,
            "polymarket_markets": m.feed_health.polymarket_markets,
        },
        "window": {
            "market_id": m.window.market_id,
            "question": m.window.question,
            "elapsed_seconds": m.window.elapsed_seconds,
            "remaining_seconds": m.window.remaining_seconds,
            "displacement_pct": m.window.displacement_pct,
            "direction": m.window.direction,
            "signal": m.window.signal,
            "mid_price": m.window.mid_price,
            "current_price": m.window.current_price,
        },
        "active_positions": [
            {
                "window_id": p.window_id,
                "side": p.side,
                "entry_price": p.entry_price,
                "size_usd": p.size_usd,
            }
            for p in m.active_positions
        ],
        "updated_at": m.updated_at.isoformat(),
    }
