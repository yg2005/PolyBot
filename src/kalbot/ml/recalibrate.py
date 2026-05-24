"""Re-calibrate an existing XGBoost model using Platt (sigmoid) scaling.

Loads the active model from model_registry, re-fits a PlattCalibrator on the
same cal holdout split used during training, evaluates on the test split, and
saves a new calibrator pickle.  The XGBoost weights are NOT retrained.

Usage:
    python -m kalbot.ml.recalibrate [--db data/kalbot.db]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import numpy as np
import xgboost as xgb
from sklearn.metrics import roc_auc_score

from kalbot.ml.calibrate import (
    PlattCalibrator,
    expected_calibration_error,
    save_calibrator,
)
from kalbot.ml.features import build_feature_matrix
from kalbot.ml.train import temporal_split_3way

log = logging.getLogger(__name__)

MODELS_DIR = Path("models")


async def _load_active_model(db_path: str) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM model_registry WHERE is_active=1 LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def recalibrate(db_path: str = "data/kalbot.db") -> str | None:
    """Re-fit Platt calibrator on active model's cal split. Returns new cal path."""
    reg = await _load_active_model(db_path)
    if not reg:
        log.error("No active model in model_registry")
        return None

    paths = json.loads(reg["model_path"])
    model_path = paths["model"]
    old_cal_path = paths["calibrator"]
    model_id = reg["model_id"]

    log.info("Recalibrating model: %s", model_id)
    log.info("  XGBoost weights: %s (unchanged)", model_path)
    log.info("  Old calibrator : %s", old_cal_path)

    model = xgb.XGBClassifier()
    model.load_model(model_path)

    # Reload same data + same split used during training
    from kalbot.data.db import Database

    db = Database(db_path)
    df = await db.get_training_data(limit=200_000, only_settled=True)
    if df.empty or len(df) < 50:
        log.error("Insufficient data: %d rows", len(df))
        return None

    df_clean = df[df["settlement_outcome"].notna()].reset_index(drop=True)
    X, y, feature_names = build_feature_matrix(df_clean)
    X_train, X_cal, X_test, y_train, y_cal, y_test = temporal_split_3way(X, y)
    log.info("Split — train: %d  cal: %d  test: %d", len(y_train), len(y_cal), len(y_test))

    # Platt calibration on the held-out cal split
    cal_raw = model.predict_proba(X_cal)[:, 1]
    cal = PlattCalibrator()
    cal.fit(cal_raw, y_cal)

    # Evaluate on test set
    test_raw = model.predict_proba(X_test)[:, 1]
    test_proba = cal.transform(test_raw)

    if len(np.unique(y_test)) < 2:
        log.error("Test set has only one class — cannot compute AUC")
        return None

    test_auc = roc_auc_score(y_test, test_proba)
    ece = expected_calibration_error(y_test, test_proba)

    log.info("Platt calibrator — test AUC: %.4f  ECE: %.4f", test_auc, ece)

    # Unique calibrated prob count
    unique_probs = np.unique(np.round(test_proba, 4))
    log.info("Distinct calibrated prob values on test set: %d", len(unique_probs))
    log.info("Prob range: %.4f – %.4f", test_proba.min(), test_proba.max())

    # Directional accuracy at TRADE_THRESHOLD=0.55
    threshold = 0.55
    trade_mask = (test_proba > threshold) | (test_proba < (1 - threshold))
    if trade_mask.sum() > 0:
        yes_mask = test_proba[trade_mask] > threshold
        no_mask = ~yes_mask
        y_trade = y_test[trade_mask]
        correct = np.where(yes_mask, y_trade == 1, y_trade == 0)
        win_rate = correct.mean()
        log.info(
            "Simulated trades: %d  win rate: %.2f%%", trade_mask.sum(), win_rate * 100
        )
    else:
        win_rate = 0.0
        log.warning("No trades simulated at threshold %.2f", threshold)

    # Save new calibrator alongside the existing model
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    new_cal_path = str(MODELS_DIR / f"{model_id}_cal_platt_{ts}.pkl")
    save_calibrator(cal, new_cal_path)

    # Update registry: same model_id, same XGB weights, new calibrator path
    new_paths = json.dumps({"model": model_path, "calibrator": new_cal_path})
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE model_registry SET model_path=?, calibration_error=? WHERE model_id=?",
            (new_paths, ece, model_id),
        )
        await db.commit()
    log.info("Registry updated: model_id=%s new_cal=%s", model_id, new_cal_path)

    return new_cal_path


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Re-calibrate active model with Platt scaling")
    p.add_argument("--db", default="data/kalbot.db")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    result = asyncio.run(recalibrate(args.db))
    if result:
        print(f"\nNew calibrator: {result}")
    else:
        print("Recalibration failed")
