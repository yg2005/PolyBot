# Phase 4 — Full-Window Logger

Read CONTEXT.md first. Phases 1-3 must be complete.

## Build

### 1. Window Logger (src/kalbot/data/logger.py)
- Snapshot at configured intervals (120s, 150s, 180s, 210s, 240s into each window)
- Grabs: WindowTracker.get_features() + spot feed data + Polymarket orderbook state
- Inserts into windows table. Updates if window_id exists.
- Primary snapshot (for ML) = the one at decision point. Tag is_primary=True.
- LOGS EVERY WINDOW — traded or not. traded=False, trade_* fields null for non-trades.
- 288 samples/day. Model needs to learn "no signal" too.

### 2. Settlement callback
- On window close: capture Chainlink price at endDate
- outcome = "YES" if settlement > strike else "NO"
- Update: settlement_outcome, settlement_price, close_price
- If traded: compute trade_pnl. YES bought at $0.55, outcome YES → pnl = (1.00-0.55)*size

### 3. Tick Logger
- Every Chainlink + spot price update → price_ticks table
- Batch inserts every 5s to avoid DB thrashing
- Include window_id for per-window replay

### 4. Daily Stats Aggregator
- Runs at midnight UTC
- Computes: total_windows, traded_windows, wins, losses, gross_pnl, net_pnl, fill_rate, maker_pct
- Stores in daily_stats table

### 5. Data Export
- get_training_data(min_date, max_date) → DataFrame
- get_feature_matrix() → X, y, feature_names (ready for XGBoost)
- export_csv(path)

## Done when
- Every 5-min window appears in windows table with features, whether traded or not
- Settlement updates outcome correctly
- `SELECT count(*) FROM windows` grows by ~12/hour
