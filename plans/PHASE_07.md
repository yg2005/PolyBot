# Phase 7 — Adaptive Order Execution

Read CONTEXT.md first. Phases 1-6 must be complete.

## Build

### 1. Order Manager (src/kalbot/execution/order_manager.py)
Interface: place_order, amend_order, cancel_order, get_order_status, settle_positions

### 2. Adaptive Execution (src/kalbot/execution/adaptive.py)
- "maker": limit at (mid - spread/4) for YES. Better price, may not fill.
- "adaptive": start maker → after 30s improve by 1¢ → repeat every 10s → if remaining <60s and edge > taker fee, convert to taker. Cancel on BTC reversal if enabled.
- "taker": take best available immediately. Only when time running out + large edge.

### 3. Polymarket CLOB integration
- POST /order with POLYMARKET_API_KEY + POLYMARKET_PRIVATE_KEY
- Gated behind config.execution.mode == "live". Paper mode writes to SQLite only.
- Handle: rejection, partial fills, nonce management

### 4. Update Paper Executor
- Simulate adaptive logic, partial fills, amendments, cancellations
- Track: time-to-fill, maker/taker ratio, slippage

## Done when
- Paper orders escalate maker→taker correctly based on time remaining
- Cancel-on-reversal works
- All order state changes logged to orders table
