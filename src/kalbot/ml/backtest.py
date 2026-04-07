"""Out-of-sample backtest with fill simulation.

Fee schedule: 1000 bps (10%) per Gamma API — applied to winning payouts.
KILL CRITERIA: ROI < 5% on paper bankroll → DO NOT go live.

Usage:
    python -m kalbot.ml.backtest [--db data/kalbot.db] [--bankroll 100]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging

import aiosqlite
import numpy as np
import xgboost as xgb

from kalbot.ml.calibrate import load_calibrator
from kalbot.ml.features import get_feature_matrix
from kalbot.ml.train import temporal_split

log = logging.getLogger(__name__)

# Constants — cannot be overridden
FEE_BPS = 1000         # 10% fee per Gamma
FEE_RATE = FEE_BPS / 10_000
TRADE_THRESHOLD = 0.55  # min calibrated prob to trade
STAKE_USD = 10.0        # per trade fixed stake
KILL_ROI_PCT = 5.0      # minimum ROI to allow live


async def _load_active_model(db_path: str) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM model_registry WHERE is_active=1 LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


def _simulate_fills(
    cal_proba: np.ndarray,
    y_true: np.ndarray,
    mid_prices: np.ndarray,
    stake_usd: float = STAKE_USD,
) -> dict:
    """Simulates binary prediction market fills.

    For YES trades: buy YES at ask (~mid_price + spread/2, approximated as mid).
    Payout = 1.0 if YES wins, 0 otherwise.
    Net P&L per trade = payout * (1 - FEE_RATE) * stake - stake
    """
    pnl_list: list[float] = []
    equity: list[float] = [0.0]

    for prob, outcome, mid in zip(cal_proba, y_true, mid_prices):
        # Decide side
        if prob > TRADE_THRESHOLD:
            side = "YES"
            entry = mid  # fill at market mid
        elif prob < (1 - TRADE_THRESHOLD):
            side = "NO"
            entry = 1.0 - mid
        else:
            continue

        if entry <= 0 or entry >= 1:
            continue

        contracts = stake_usd / entry
        if side == "YES":
            win = outcome == 1
        else:
            win = outcome == 0

        if win:
            gross = contracts * 1.0
            net = gross * (1.0 - FEE_RATE) - stake_usd
        else:
            net = -stake_usd

        pnl_list.append(net)
        equity.append(equity[-1] + net)

    if not pnl_list:
        return {"n_trades": 0, "total_pnl": 0.0, "roi_pct": 0.0,
                "win_rate": 0.0, "sharpe": 0.0, "max_drawdown_pct": 0.0}

    pnl = np.array(pnl_list)
    eq = np.array(equity)

    total_pnl = float(pnl.sum())
    n_trades = len(pnl)
    win_rate = float((pnl > 0).mean())
    roi_pct = total_pnl / (n_trades * stake_usd) * 100.0

    # Sharpe (per-trade, not annualised)
    sharpe = float(pnl.mean() / pnl.std()) if pnl.std() > 0 else 0.0

    # Max drawdown
    peak = np.maximum.accumulate(eq)
    drawdowns = (eq - peak) / (np.abs(peak) + 1e-9) * 100.0
    max_drawdown_pct = float(drawdowns.min())

    return {
        "n_trades": n_trades,
        "total_pnl": round(total_pnl, 4),
        "roi_pct": round(roi_pct, 2),
        "win_rate": round(win_rate, 4),
        "sharpe": round(sharpe, 4),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
    }


async def backtest(
    db_path: str = "data/kalbot.db",
    bankroll_usd: float = 100.0,
    min_date: str | None = None,
    max_date: str | None = None,
) -> dict:
    reg = await _load_active_model(db_path)
    if not reg:
        log.error("No active model in model_registry")
        return {}

    paths = json.loads(reg["model_path"])
    model = xgb.XGBClassifier()
    model.load_model(paths["model"])
    cal = load_calibrator(paths["calibrator"])

    # Load data — need mid_price alongside features
    from kalbot.data.db import Database
    from kalbot.ml.features import FEATURE_COLS, _add_derived, build_feature_matrix

    db = Database(db_path)
    df = await db.get_training_data(
        limit=200_000,
        only_settled=True,
        min_date=min_date,
        max_date=max_date,
    )
    if df.empty or len(df) < 20:
        log.error("Insufficient data for backtest")
        return {}

    X, y, _ = build_feature_matrix(df)
    _, X_val, _, y_val = temporal_split(X, y)

    # mid_price for the validation slice
    n_val = len(y_val)
    df_val = df.iloc[-n_val:].reset_index(drop=True)
    mid_prices = df_val["mid_price"].fillna(0.5).to_numpy(dtype=float)

    raw_proba = model.predict_proba(X_val)[:, 1]
    cal_proba = cal.transform(raw_proba)

    results = _simulate_fills(cal_proba, y_val, mid_prices, stake_usd=STAKE_USD)
    results["model_id"] = reg["model_id"]
    results["fee_bps"] = FEE_BPS
    results["stake_usd"] = STAKE_USD

    roi = results["roi_pct"]
    log.info("=== Backtest Results ===")
    log.info("  Model    : %s", reg["model_id"])
    log.info("  Trades   : %d", results["n_trades"])
    log.info("  Total P&L: $%.2f", results["total_pnl"])
    log.info("  ROI      : %.2f%%", roi)
    log.info("  Win rate : %.2f%%", results["win_rate"] * 100)
    log.info("  Sharpe   : %.4f", results["sharpe"])
    log.info("  Max DD   : %.2f%%", results["max_drawdown_pct"])

    if roi < KILL_ROI_PCT:
        log.critical(
            "KILL CRITERIA: ROI=%.2f%% < %.1f%% — DO NOT go live",
            roi, KILL_ROI_PCT,
        )
        results["go_live"] = False
    else:
        log.info("ROI=%.2f%% >= %.1f%% — OK to proceed", roi, KILL_ROI_PCT)
        results["go_live"] = True

    return results


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/kalbot.db")
    p.add_argument("--bankroll", type=float, default=100.0)
    p.add_argument("--min-date", default=None)
    p.add_argument("--max-date", default=None)
    p.add_argument("--log-level", default="INFO")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    results = asyncio.run(backtest(args.db, args.bankroll, args.min_date, args.max_date))
    for k, v in results.items():
        print(f"  {k}: {v}")
