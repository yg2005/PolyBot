# Phase 10 — Go Live

Read CONTEXT.md first. Phases 8+9 must be complete.

## Pre-live Checklist (ALL must pass)
- Model AUC >= 0.75 out-of-sample
- Calibration error < 0.05
- Backtest P&L positive after fees
- Paper trading profitable last 7 days
- All feeds healthy >1 hour
- Risk limits configured and tested
- Alerts working (send test)
- Polymarket API credentials valid
- USDC balance sufficient
- VPN to Switzerland active
- VERIFY ACTUAL FEE SCHEDULE (Gamma shows 1000 bps — if 10% real, edge math needs recalibration)
- Window tracker elapsed <300 for last 100 windows

## Build
1. Live executor: real CLOB orders via POST /order. Auth + nonce. Handle rejections, partial fills.
2. Gradual ramp: Day 1-3 = 25% size, Day 4-7 = 50%, Day 8+ = 100%. Auto de-ramp if daily loss hits 50% of limit.
3. Live monitoring: real USDC balance, actual fill prices vs expected, real fees, reconciliation vs paper.
4. kill_switch.py: cancel all, close all, disable trading. Auto-kill on: internet drop >60s, uncaught exception, 5 consecutive API 5xx.

## Done when
- Live orders placed and filled on Polymarket CLOB
- P&L tracked against real USDC balance
- Kill switch works from command line
