# Phase 6 — Decision Engine & Risk Manager

Read CONTEXT.md first. Phases 1-5 must be complete.

## Build

### 1. Decision Engine (src/kalbot/engine/decision.py)
Takes ScorerResult + WindowSnapshot + RiskState. Checks in order, stop at first fail:
1. signal != "PASS"
2. edge > min_edge_pct (3%)
3. risk manager allows new position
4. bid/ask depth > order size
5. spread < $0.10
6. Kelly sizing: f* = (p*b - q) / b, apply 0.25 fraction, cap at max_position_usd ($25), floor $5

Strategy selection:
- remaining > 120s → "maker"
- 60-120s → "adaptive"
- < 60s → "taker" (if edge > taker fee)

### 2. Risk Manager (src/kalbot/risk/risk_manager.py)

HARD LIMITS (cannot be overridden):
- MAX_POSITION_USD_HARD = 50
- MAX_DAILY_LOSS_HARD = 30
- MAX_DRAWDOWN_HARD = 25.0%

Configurable (must be <= hard limits):
- max_position_usd = 25, max_daily_loss_usd = 15, max_drawdown_pct = 15%, max_concurrent_positions = 2

Methods: can_trade() → (bool, reason), register_trade(), register_settlement(), is_circuit_breaker_active()
Circuit breaker: daily loss > limit OR drawdown > limit OR 10+ consecutive losses

### 3. Kill Switch
Circuit breaker → cancel all orders, stop trading, CONTINUE LOGGING, send alert, resume next day.

## Done when
- Decision engine correctly filters by edge, risk, liquidity
- Kelly sizing produces sane values for $100 bankroll
- Circuit breaker triggers and blocks trading while continuing data collection
