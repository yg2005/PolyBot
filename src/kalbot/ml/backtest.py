"""Out-of-sample backtest with fill simulation.

Fee schedule (Polymarket docs — docs.polymarket.com/trading/fees, crypto category):
    fee = contracts × feeRate × p × (1 − p)
        = stake × feeRate × (1 − p)    [since contracts = stake / p]
    Taker feeRate = 0.07 (7%), maker feeRate = 0.00.
    Fee charged on EVERY trade (wins and losses).
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
from kalbot.ml.train import temporal_split_3way

log = logging.getLogger(__name__)

# Fee schedule — Polymarket crypto category (docs.polymarket.com/trading/fees)
TAKER_FEE_RATE = 0.07   # 7% for crypto markets
MAKER_FEE_RATE = 0.00   # makers pay no fee
MAKER_FILL_RATE = 0.70  # conservative estimate: 70% of our orders fill as maker

# Trading constants — cannot be overridden
TRADE_THRESHOLD = 0.55
STAKE_USD = 10.0
KILL_ROI_PCT = 5.0


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
    bankroll_usd: float = 100.0,
    stake_usd: float = STAKE_USD,
    kelly_fraction: float = 0.25,
    compounding: bool = False,
    maker_fill_rate: float = MAKER_FILL_RATE,
) -> dict:
    """Simulates fills with bankroll constraints and correct Polymarket fee model.

    Position sizing:
      compounding=False: stake = min(stake_usd, equity * kelly_fraction)
                         skips trade if equity < stake_usd
      compounding=True:  stake = equity * kelly_fraction
                         skips trade if equity <= 0
    Stops entirely (does not skip) if equity reaches $0 after a trade.

    Fee formula (Polymarket docs, crypto category):
        fee = contracts × feeRate × p × (1 − p) = stake × feeRate × (1 − p)
    Effective feeRate = (1 − maker_fill_rate) × TAKER_FEE_RATE.
    Fee charged on every trade regardless of outcome.
    """
    effective_fee_rate = (1.0 - maker_fill_rate) * TAKER_FEE_RATE

    equity: float = bankroll_usd
    equity_curve: list[float] = [bankroll_usd]
    pnl_list: list[float] = []
    direction_correct: list[int] = []
    n_skipped: int = 0
    ruined_at_trade: int | None = None

    for prob, outcome, mid in zip(cal_proba, y_true, mid_prices):
        if prob > TRADE_THRESHOLD:
            side = "YES"
            entry = mid
        elif prob < (1 - TRADE_THRESHOLD):
            side = "NO"
            entry = 1.0 - mid
        else:
            continue

        if entry <= 0 or entry >= 1:
            continue

        # Hard stop — broke
        if equity <= 0:
            if ruined_at_trade is None:
                ruined_at_trade = len(pnl_list)
            break

        # Position sizing with solvency check
        if compounding:
            stake = equity * kelly_fraction
            if stake <= 0:
                n_skipped += 1
                continue
        else:
            if equity < stake_usd:
                n_skipped += 1
                continue
            stake = min(stake_usd, equity * kelly_fraction)

        contracts = stake / entry
        # fee = contracts × feeRate × entry × (1 − entry) = stake × feeRate × (1 − entry)
        fee = stake * effective_fee_rate * (1.0 - entry)

        # Directional accuracy: correct if predicted side matches settlement
        correct = int(outcome == 1 if side == "YES" else outcome == 0)
        direction_correct.append(correct)

        if correct:
            net = contracts - stake - fee
        else:
            net = -stake - fee

        pnl_list.append(net)
        equity += net
        equity_curve.append(equity)

        if equity <= 0 and ruined_at_trade is None:
            ruined_at_trade = len(pnl_list)
            break

    _eff = round(effective_fee_rate, 4)
    if not pnl_list:
        return {
            "n_trades": 0, "n_skipped": n_skipped,
            "total_pnl": 0.0, "roi_pct": 0.0,
            "directional_accuracy": 0.0, "pnl_win_rate": 0.0,
            "sharpe": 0.0, "max_drawdown_usd": 0.0,
            "equity_start": round(bankroll_usd, 2),
            "equity_low": round(bankroll_usd, 2),
            "equity_end": round(bankroll_usd, 2),
            "ruined_at_trade": ruined_at_trade,
            "effective_fee_rate": _eff,
        }

    pnl = np.array(pnl_list)
    eq = np.array(equity_curve)

    total_pnl = float(pnl.sum())
    n_trades = len(pnl)
    directional_accuracy = float(np.mean(direction_correct))
    pnl_win_rate = float((pnl > 0).mean())
    roi_pct = total_pnl / bankroll_usd * 100.0

    sharpe = float(pnl.mean() / pnl.std()) if pnl.std() > 0 else 0.0

    # Max drawdown: peak-to-trough in dollars on the equity curve
    peak = np.maximum.accumulate(eq)
    max_drawdown_usd = float((eq - peak).min())

    return {
        "n_trades": n_trades,
        "n_skipped": n_skipped,
        "total_pnl": round(total_pnl, 4),
        "roi_pct": round(roi_pct, 2),
        "directional_accuracy": round(directional_accuracy, 4),
        "pnl_win_rate": round(pnl_win_rate, 4),
        "sharpe": round(sharpe, 4),
        "max_drawdown_usd": round(max_drawdown_usd, 2),
        "equity_start": round(float(eq[0]), 2),
        "equity_low": round(float(eq.min()), 2),
        "equity_end": round(float(eq[-1]), 2),
        "ruined_at_trade": ruined_at_trade,
        "effective_fee_rate": _eff,
    }


async def backtest(
    db_path: str = "data/kalbot.db",
    bankroll_usd: float = 100.0,
    kelly_fraction: float = 0.25,
    compounding: bool = False,
    maker_fill_rate: float = MAKER_FILL_RATE,
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
    from kalbot.ml.features import build_feature_matrix

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

    # Pre-filter to match the exact rows build_feature_matrix will use, so
    # df_clean index aligns with X/y after the NULL filter inside build_feature_matrix.
    df_clean = df[df["settlement_outcome"].notna()].reset_index(drop=True)
    X, y, _ = build_feature_matrix(df_clean)
    _, _, X_val, _, _, y_val = temporal_split_3way(X, y)

    # mid_price for the test slice — uses df_clean so row count matches y_val exactly
    n_val = len(y_val)
    df_val = df_clean.iloc[-n_val:].reset_index(drop=True)
    mid_prices = df_val["mid_price"].fillna(0.5).to_numpy(dtype=float)

    raw_proba = model.predict_proba(X_val)[:, 1]
    cal_proba = cal.transform(raw_proba)

    results = _simulate_fills(
        cal_proba, y_val, mid_prices,
        bankroll_usd=bankroll_usd,
        stake_usd=STAKE_USD,
        kelly_fraction=kelly_fraction,
        compounding=compounding,
        maker_fill_rate=maker_fill_rate,
    )
    results["model_id"] = reg["model_id"]
    results["taker_fee_rate"] = TAKER_FEE_RATE
    results["maker_fill_rate"] = maker_fill_rate
    results["kelly_fraction"] = kelly_fraction
    results["compounding"] = compounding

    roi = results["roi_pct"]
    log.info("=== Backtest Results ===")
    log.info("  Model        : %s", reg["model_id"])
    log.info("  Bankroll     : $%.2f", bankroll_usd)
    log.info("  Compounding  : %s  kelly=%.2f", compounding, kelly_fraction)
    log.info("  Trades exec  : %d  skipped: %d", results["n_trades"], results["n_skipped"])
    log.info("  Dir accuracy : %.2f%%", results["directional_accuracy"] * 100)
    log.info("  P&L win rate : %.2f%%", results["pnl_win_rate"] * 100)
    log.info("  Total P&L    : $%.2f", results["total_pnl"])
    log.info("  ROI          : %.2f%%", roi)
    log.info("  Equity       : $%.2f → low $%.2f → end $%.2f",
             results["equity_start"], results["equity_low"], results["equity_end"])
    log.info("  Max DD       : $%.2f", results["max_drawdown_usd"])
    log.info("  Sharpe       : %.4f", results["sharpe"])
    if results["ruined_at_trade"] is not None:
        log.critical("  RUIN at trade #%d — equity hit $0", results["ruined_at_trade"])

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
    p.add_argument("--kelly-fraction", type=float, default=0.25)
    p.add_argument("--compounding", action="store_true", default=False)
    p.add_argument("--maker-fill-rate", type=float, default=MAKER_FILL_RATE)
    p.add_argument("--min-date", default=None)
    p.add_argument("--max-date", default=None)
    p.add_argument("--log-level", default="INFO")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    results = asyncio.run(backtest(
        args.db, args.bankroll, args.kelly_fraction, args.compounding,
        args.maker_fill_rate, args.min_date, args.max_date,
    ))
    for k, v in results.items():
        print(f"  {k}: {v}")
