"""Model evaluation: AUC-ROC, AUC-PR, calibration, feature importance.

Usage:
    python -m kalbot.ml.evaluate [--db data/kalbot.db] [--model-id xgb_...]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging

import aiosqlite
import numpy as np
import xgboost as xgb
from sklearn.calibration import calibration_curve
from sklearn.metrics import average_precision_score, roc_auc_score

from kalbot.ml.calibrate import expected_calibration_error, load_calibrator
from kalbot.ml.features import get_feature_matrix
from kalbot.ml.train import temporal_split

log = logging.getLogger(__name__)


async def _load_active_model(db_path: str, model_id: str | None) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        if model_id:
            sql = "SELECT * FROM model_registry WHERE model_id=?"
            params = (model_id,)
        else:
            sql = "SELECT * FROM model_registry WHERE is_active=1 LIMIT 1"
            params = ()
        async with db.execute(sql, params) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def evaluate(
    db_path: str = "data/kalbot.db",
    model_id: str | None = None,
    min_date: str | None = None,
    max_date: str | None = None,
) -> dict:
    reg = await _load_active_model(db_path, model_id)
    if not reg:
        log.error("No model found (model_id=%s)", model_id)
        return {}

    paths = json.loads(reg["model_path"])
    model = xgb.XGBClassifier()
    model.load_model(paths["model"])
    cal = load_calibrator(paths["calibrator"])

    # Load raw DataFrame to get mid_price for edge distribution
    from kalbot.data.db import Database
    from kalbot.ml.features import build_feature_matrix

    db = Database(db_path)
    df = await db.get_training_data(limit=200_000, only_settled=True,
                                    min_date=min_date, max_date=max_date)
    if df.empty or len(df) < 10:
        log.error("Insufficient data for evaluation")
        return {}

    X, y, feature_names = build_feature_matrix(df)
    _, X_val, _, y_val = temporal_split(X, y)

    n_val = len(y_val)
    df_val = df.iloc[-n_val:].reset_index(drop=True)
    mid_prices = df_val["mid_price"].fillna(0.5).to_numpy(dtype=float)

    raw_proba = model.predict_proba(X_val)[:, 1]
    cal_proba = cal.transform(raw_proba)

    if len(np.unique(y_val)) < 2:
        log.error("Validation set has only one class")
        return {}

    auc_roc = roc_auc_score(y_val, cal_proba)
    auc_pr = average_precision_score(y_val, cal_proba)
    ece = expected_calibration_error(y_val, cal_proba)

    # Calibration curve
    frac_pos, mean_pred = calibration_curve(y_val, cal_proba, n_bins=10, strategy="uniform")
    cal_curve = {
        "fraction_of_positives": frac_pos.tolist(),
        "mean_predicted_value": mean_pred.tolist(),
    }

    # Feature importance
    importance = dict(zip(feature_names, model.feature_importances_.tolist()))
    top_features = sorted(importance.items(), key=lambda x: x[1], reverse=True)[:10]

    # Edge distribution vs actual market price
    yes_edge = cal_proba - mid_prices          # positive → we think YES is underpriced
    no_edge = (1.0 - cal_proba) - (1.0 - mid_prices)  # = mid_prices - cal_proba
    best_edge = np.where(yes_edge >= no_edge, yes_edge, no_edge)
    mean_edge = float(best_edge.mean())
    pos_edge_pct = float((best_edge > 0.02).mean() * 100)

    # Profitability simulation (simplified — see backtest.py for full sim)
    threshold = 0.55
    trade_mask = (cal_proba > threshold) | (cal_proba < (1 - threshold))
    n_trades = int(trade_mask.sum())
    if n_trades > 0:
        yes_mask = cal_proba[trade_mask] > 0.5
        no_mask = ~yes_mask
        y_t = y_val[trade_mask]
        wins = int((yes_mask & (y_t == 1)).sum() + (no_mask & (y_t == 0)).sum())
        win_rate = wins / n_trades
    else:
        win_rate = 0.0

    results = {
        "model_id": reg["model_id"],
        "auc_roc": round(auc_roc, 4),
        "auc_pr": round(auc_pr, 4),
        "ece": round(ece, 4),
        "calibration_curve": cal_curve,
        "n_val_samples": len(y_val),
        "n_simulated_trades": n_trades,
        "simulated_win_rate": round(win_rate, 4),
        "mean_best_edge": round(mean_edge, 4),
        "pct_positive_edge_2pct": round(pos_edge_pct, 2),
        "top_features": top_features,
    }

    log.info("=== Evaluation Results ===")
    log.info("  AUC-ROC : %.4f", auc_roc)
    log.info("  AUC-PR  : %.4f", auc_pr)
    log.info("  ECE     : %.4f", ece)
    log.info("  Win rate (sim): %.2f%%", win_rate * 100)
    log.info("  Mean edge vs market: %.4f", mean_edge)
    log.info("  Top features: %s", top_features[:5])

    return results


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/kalbot.db")
    p.add_argument("--model-id", default=None)
    p.add_argument("--min-date", default=None)
    p.add_argument("--max-date", default=None)
    p.add_argument("--log-level", default="INFO")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    results = asyncio.run(evaluate(args.db, args.model_id, args.min_date, args.max_date))
    for k, v in results.items():
        print(f"  {k}: {v}")
