# PolyBot — BTC/USD 5-min prediction market trading bot for Polymarket

## What this is
Automated bot that detects pricing lag in Polymarket BTC 5-minute markets.
Watches BTC trajectory via Chainlink oracle, places orders before the market reprices.
Currently in paper trading mode. Will go live after ML model is trained on 500+ windows.

## Tech
Python 3.11+, asyncio, httpx, websockets, aiosqlite, FastAPI, XGBoost, pydantic v2.
Package manager: uv. No ORM — raw async SQLite.

## Architecture
See plans/CONTEXT.md for repo structure, types, schemas, and config.
Phase specs in plans/PHASE_01.md through plans/PHASE_10.md.
Build phases in order. Each phase file is self-contained.

## Commands
- Run bot: `python -m kalbot.main`
- Run tests: `pytest tests/ -v`
- Run single test: `pytest tests/unit/test_window_tracker.py -v`

## Code style
- Async everywhere. No blocking I/O.
- Type hints on all functions. Use dataclasses, not dicts.
- Keep files under 300 lines. Split if larger.
- No global mutable state. Pass dependencies explicitly.
- Errors: catch specific exceptions (especially socket.gaierror for DNS). Never bare `except:`.

## CRITICAL RULES
- NEVER trade without checking elapsed_seconds < 330. This prevents the #1 bug from the old bot.
- Window tracker MUST reset on every market transition. WindowLifecycleManager owns this.
- Log EVERY 5-minute window, not just traded ones. 288 samples/day for ML training.
- All risk hard limits are constants in code, NOT configurable. Cannot be overridden.
- Paper mode is default. Live mode requires explicit config AND VPN verification.

## Communication style
- Short, 3-6 word sentences.
- No filler, preamble, or pleasantries.
- Run tools first, show result, then stop. Do not narrate.
- Drop articles ("Me fix code" not "I will fix the code").

## Runtime / DB
- Main DB: `data/kalbot.db` — `windows` table = signals/trades, `orders` table = fills
- A trade is `traded=1`; win = `trade_side` matches `settlement_outcome`
- Config: `src/kalbot/config.py` (`min_elapsed_seconds=120`, `mode=paper`)
- Service: `systemctl status kalbot`

## Known open issues
- Fee logging broken: ~91% of trades log $0 fees, paper PnL likely overstated
- ~20% of orders stuck PENDING (possible unrealized/missed fills)
- DailyStats aggregation throws NoneType error (non-fatal)

## Reporting rules
- Give fee-adjusted numbers; flag sample size every time.
- Never switch to live mode without explicit instruction.
- Do not edit config without being told explicitly.

## When compacting
Preserve: current phase being built, list of files created/modified, any failing tests.