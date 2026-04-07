# Phase 9 — Dashboard & Monitoring

Read CONTEXT.md first. Phase 5 must be complete.

## Build

### 1. Metrics Collector (src/kalbot/monitoring/metrics.py)
In-memory, flushed to DB. Tracks: current window state, active positions, today P&L, feed health, model state, data collection progress (X/500 windows).

### 2. Edge Monitor (src/kalbot/monitoring/edge_monitor.py)
Weekly: realized_edge vs predicted_edge. market_efficiency_score = avg time for CLOB to reflect Chainlink move >0.05%. Alert if edge declining 3 consecutive weeks.

### 3. Alerts (src/kalbot/monitoring/alerts.py)
Discord webhook or Telegram bot. Alert on: trade, settlement, error, circuit breaker, edge decay, model retrain.

### 4. Dashboard (src/kalbot/dashboard/)
FastAPI + Jinja2 + HTMX. No React. No build step.
- / : current window, time bar, features, positions, feed health, today P&L
- /performance : daily P&L chart, cumulative line, win rate, edge decay
- /data : progress bar to 500, feature distributions
- /model : AUC, calibration, feature importance, backtest
- /settings : read-only config, kill switch button, retrain trigger
HTMX poll: 5s on live view, 30s on others.

## Done when
- Dashboard shows live window state and feed health
- Performance page renders P&L charts
- Alerts fire on trade/error events
