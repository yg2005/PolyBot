# Phase 1 — Core Infrastructure

Read CONTEXT.md first. Build exactly what's described below.

## Build

1. pyproject.toml — deps: httpx, websockets, aiosqlite, xgboost, scikit-learn, pydantic>=2.0, fastapi, uvicorn, jinja2, python-dotenv, tomli, numpy, pandas, pytest, pytest-asyncio. Use uv. Python 3.11+.

2. src/kalbot/config.py — Load from TOML (config/default.toml, paper.toml, live.toml). KALBOT_ENV env var selects override. Pydantic validation. Secrets from .env only.

3. src/kalbot/types.py — WindowSnapshot, ScorerResult, DecisionResult dataclasses. OrderState enum (PENDING, OPEN, FILLED, PARTIAL, CANCELLED). TradeSide enum (YES, NO). All from CONTEXT.md.

4. src/kalbot/data/schemas.py + db.py — Async SQLite via aiosqlite. All tables:

```sql
CREATE TABLE windows (
    window_id TEXT PRIMARY KEY, market_id TEXT, strike_price REAL,
    window_open_time TEXT, window_close_time TEXT,
    open_price REAL, snapshot_price REAL, close_price REAL,
    displacement_pct REAL, abs_displacement_pct REAL, direction INTEGER,
    direction_consistency REAL, cross_count INTEGER,
    time_above_pct REAL, time_below_pct REAL,
    max_displacement_pct REAL, min_displacement_pct REAL,
    velocity REAL, acceleration REAL, distance_from_low REAL,
    spot_price REAL, spot_displacement_pct REAL, spot_trend_1m REAL,
    spot_confirms INTEGER, spot_source TEXT,
    yes_bid REAL, yes_ask REAL, no_bid REAL, no_ask REAL,
    spread REAL, mid_price REAL, bid_depth_usd REAL, ask_depth_usd REAL,
    depth_imbalance REAL, market_move_speed REAL,
    elapsed_seconds INTEGER, remaining_seconds INTEGER, snapshot_time TEXT,
    settlement_outcome TEXT, settlement_price REAL,
    traded INTEGER DEFAULT 0, trade_side TEXT,
    trade_entry_price REAL, trade_fill_price REAL, trade_pnl REAL,
    rule_signal TEXT, model_prob REAL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE price_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, window_id TEXT,
    source TEXT, price REAL, timestamp TEXT,
    FOREIGN KEY (window_id) REFERENCES windows(window_id)
);

CREATE TABLE orders (
    order_id TEXT PRIMARY KEY, window_id TEXT, side TEXT,
    order_type TEXT, price REAL, size REAL, status TEXT,
    placed_at TEXT, filled_at TEXT, fill_price REAL, fees REAL,
    cancel_reason TEXT,
    FOREIGN KEY (window_id) REFERENCES windows(window_id)
);

CREATE TABLE daily_stats (
    date TEXT PRIMARY KEY, total_windows INTEGER, traded_windows INTEGER,
    wins INTEGER, losses INTEGER, gross_pnl REAL, net_pnl REAL,
    avg_edge REAL, max_drawdown REAL, fill_rate REAL, maker_pct REAL, model_auc REAL
);

CREATE TABLE edge_tracker (
    week_start TEXT PRIMARY KEY, realized_edge REAL, predicted_edge REAL,
    market_efficiency_score REAL, avg_repricing_speed_ms REAL,
    trade_count INTEGER, win_rate REAL
);

CREATE TABLE model_registry (
    model_id TEXT PRIMARY KEY, trained_at TEXT, training_samples INTEGER,
    auc_score REAL, calibration_error REAL, features_used TEXT,
    hyperparams TEXT, model_path TEXT, is_active INTEGER DEFAULT 0
);
```

Helper methods: insert_window, update_settlement, get_training_data, get_recent_windows.

5. src/kalbot/main.py — Async skeleton. Component init order. Graceful shutdown on SIGTERM/SIGINT. Structured JSON logging. Do NOT implement feeds/engine/execution — just ABCs and stubs.

6. .env.example:
```
POLYMARKET_API_KEY=
POLYMARKET_PRIVATE_KEY=
DISCORD_WEBHOOK_URL=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
KALBOT_ENV=paper
DASHBOARD_SECRET_KEY=change-me
```

7. Config files: config/default.toml (from CONTEXT.md), config/paper.toml (empty overrides), config/live.toml (mode="live").

## Done when
- `python -m kalbot.main` starts, creates DB, prints "KalBot starting in paper mode", shuts down cleanly on Ctrl+C.
- All tables exist in data/kalbot.db.
