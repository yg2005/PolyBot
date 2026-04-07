# PolyBot — Shared Context (read by every phase)

## Repo Structure
```
src/kalbot/
├── __init__.py
├── main.py                 # Entry point — orchestrator
├── config.py               # Config loading & validation
├── types.py                # All shared types/dataclasses
├── feeds/
│   ├── __init__.py
│   ├── chainlink.py        # Chainlink BTC/USD via Polymarket RTDS WebSocket
│   ├── spot_feed.py        # Coinbase/Kraken REST spot price (confirmation)
│   ├── polymarket.py       # Gamma API discovery + CLOB API orderbook/orders
│   └── base.py             # Abstract feed interface
├── engine/
│   ├── __init__.py
│   ├── window_tracker.py   # Intra-window trajectory features
│   ├── scorer.py           # Rule-based signal scorer
│   ├── ml_scorer.py        # XGBoost model scorer (same interface)
│   └── decision.py         # Edge check + risk filter → trade/pass
├── execution/
│   ├── __init__.py
│   ├── order_manager.py
│   ├── adaptive.py
│   └── paper.py
├── risk/
│   ├── __init__.py
│   └── risk_manager.py
├── data/
│   ├── __init__.py
│   ├── logger.py
│   ├── db.py
│   └── schemas.py
├── ml/
│   ├── __init__.py
│   ├── features.py
│   ├── train.py
│   ├── calibrate.py
│   ├── evaluate.py
│   └── backtest.py
├── monitoring/
│   ├── __init__.py
│   ├── metrics.py
│   ├── edge_monitor.py
│   └── alerts.py
└── dashboard/
    ├── __init__.py
    ├── app.py
    ├── templates/
    └── static/
```

## Tech Stack
Python 3.11+, httpx, websockets, aiosqlite, xgboost, scikit-learn, pydantic v2, FastAPI, uvicorn, jinja2, python-dotenv, tomli, numpy, pandas. Package manager: uv.

## Core Types
```python
@dataclass
class WindowSnapshot:
    window_id: str; market_id: str; strike_price: float
    window_open_time: datetime; window_close_time: datetime
    open_price: float; snapshot_price: float; close_price: float | None
    displacement_pct: float; abs_displacement_pct: float; direction: int
    direction_consistency: float; cross_count: int
    time_above_pct: float; time_below_pct: float
    max_displacement_pct: float; min_displacement_pct: float
    velocity: float; acceleration: float; distance_from_low: float
    spot_price: float; spot_displacement_pct: float; spot_trend_1m: float
    spot_confirms: bool; spot_source: str
    yes_bid: float; yes_ask: float; no_bid: float; no_ask: float
    spread: float; mid_price: float; bid_depth_usd: float; ask_depth_usd: float
    depth_imbalance: float; market_move_speed: float
    elapsed_seconds: int; remaining_seconds: int; snapshot_time: datetime
    settlement_outcome: str | None; settlement_price: float | None
    traded: bool; trade_side: str | None; trade_entry_price: float | None
    trade_fill_price: float | None; trade_pnl: float | None
    rule_signal: str | None; model_prob: float | None

class BaseScorer(ABC):
    @abstractmethod
    async def score(self, snapshot: WindowSnapshot) -> ScorerResult: ...

@dataclass
class ScorerResult:
    signal: str        # "YES", "NO", or "PASS"
    confidence: float  # 0.0–1.0
    edge_estimate: float
    reasoning: str
    features_used: dict

@dataclass
class DecisionResult:
    action: str           # "TRADE" or "PASS"
    side: str | None
    target_price: float | None
    size_usd: float | None
    strategy: str | None  # "maker", "taker", "adaptive"
    pass_reason: str | None
    scorer_result: ScorerResult
```

## SQL Tables
windows, price_ticks, orders, daily_stats, edge_tracker, model_registry — see PHASE_01.md for full DDL.

## Config (config/default.toml)
```toml
[feeds]
chainlink_ws_url = "wss://ws-live-data.polymarket.com"
chainlink_ping_interval_s = 5
chainlink_stale_threshold_s = 10
spot_sources = ["coinbase", "kraken"]
spot_poll_interval_s = 2.0
gamma_api_url = "https://gamma-api.polymarket.com"
clob_api_url = "https://clob.polymarket.com"
market_discovery_interval_s = 30
market_series_ticker = "POLYBTC5M"

[engine]
min_elapsed_seconds = 60
max_elapsed_seconds = 270
min_displacement_pct = 0.02
min_direction_consistency = 0.60
max_cross_count = 6
min_time_above_yes = 0.55
max_time_above_no = 0.45
spot_trend_conflict_threshold = 0.0008
require_spot_confirmation = true

[execution]
mode = "paper"
default_order_size_usd = 10.0
maker_timeout_seconds = 30
taker_threshold_seconds = 60
cancel_on_reversal = true

[risk]
max_position_usd = 25.0
max_daily_loss_usd = 15.0
max_drawdown_pct = 15.0
max_concurrent_positions = 2
min_edge_pct = 3.0
starting_bankroll_usd = 100.0

[data]
db_path = "data/kalbot.db"
log_all_windows = true
tick_logging = true
snapshot_at_seconds = [120, 150, 180, 210, 240]
```

## CRITICAL BUG TO PREVENT
The old bot defined window_tracker.reset() but NEVER CALLED IT. 82% of training data was corrupted. The new build uses WindowLifecycleManager that auto-resets on every market transition. If elapsed_seconds > 330 at any point, log CRITICAL error and block trading.
