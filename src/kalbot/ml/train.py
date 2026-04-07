"""Train XGBoost model on collected window data.

Usage:
    python -m kalbot.ml.train [--db data/kalbot.db] [--min-date YYYY-MM-DD] [--max-date YYYY-MM-DD]
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
from sklearn.model_selection import TimeSeriesSplit

from kalbot.ml.calibrate import expected_calibration_error, fit_calibrator, save_calibrator
from kalbot.ml.features import get_feature_matrix

log = logging.getLogger(__name__)

XGB_PARAMS = {
    "max_depth": 4,
    "n_estimators": 200,
    "learning_rate": 0.05,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "min_child_weight": 5,
    "eval_metric": "auc",
    "use_label_encoder": False,
    "random_state": 42,
}

MIN_AUC = 0.75
TRAIN_FRAC = 0.75
MODELS_DIR = Path("models")


def temporal_split(
    X: np.ndarray,
    y: np.ndarray,
    train_frac: float = TRAIN_FRAC,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Strictly temporal split — no shuffling."""
    n_train = int(len(X) * train_frac)
    return X[:n_train], X[n_train:], y[:n_train], y[n_train:]


def walk_forward_auc(
    X: np.ndarray,
    y: np.ndarray,
    n_splits: int = 5,
) -> float:
    """Rolling train/val splits; returns mean AUC across folds."""
    tscv = TimeSeriesSplit(n_splits=n_splits)
    aucs: list[float] = []
    for fold, (train_idx, val_idx) in enumerate(tscv.split(X)):
        X_tr, X_va = X[train_idx], X[val_idx]
        y_tr, y_va = y[train_idx], y[val_idx]
        if len(np.unique(y_va)) < 2:
            log.warning("Fold %d: only one class in val — skipping", fold)
            continue
        model = xgb.XGBClassifier(**XGB_PARAMS)
        model.fit(X_tr, y_tr, eval_set=[(X_va, y_va)], verbose=False)
        proba = model.predict_proba(X_va)[:, 1]
        auc = roc_auc_score(y_va, proba)
        aucs.append(auc)
        log.info("Walk-forward fold %d: AUC=%.4f (n_train=%d n_val=%d)", fold, auc, len(y_tr), len(y_va))
    return float(np.mean(aucs)) if aucs else 0.0


async def _register_model(
    db_path: str,
    model_id: str,
    training_samples: int,
    auc_score: float,
    calibration_error: float,
    feature_names: list[str],
    feature_importance: dict[str, float],
    model_path: str,
    cal_path: str,
) -> None:
    paths = json.dumps({"model": model_path, "calibrator": cal_path})
    hyperparams = json.dumps({
        "params": XGB_PARAMS,
        "feature_importance": feature_importance,
    })
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE model_registry SET is_active=0")
        await db.execute(
            """INSERT OR REPLACE INTO model_registry
               (model_id, trained_at, training_samples, auc_score,
                calibration_error, features_used, hyperparams, model_path, is_active)
               VALUES (?,?,?,?,?,?,?,?,1)""",
            (
                model_id,
                datetime.now(timezone.utc).isoformat(),
                training_samples,
                auc_score,
                calibration_error,
                json.dumps(feature_names),
                hyperparams,
                paths,
            ),
        )
        await db.commit()
    log.info("Model %s registered as active in model_registry", model_id)


async def train(
    db_path: str = "data/kalbot.db",
    min_date: str | None = None,
    max_date: str | None = None,
) -> str | None:
    """Full training pipeline. Returns model_id if successful, None if AUC < threshold."""
    X, y, feature_names = await get_feature_matrix(db_path, min_date, max_date)

    if len(X) < 50:
        log.error("Insufficient data: %d samples (need >= 50)", len(X))
        return None

    log.info("Feature matrix: %d samples × %d features", *X.shape)

    # Walk-forward validation
    wf_auc = walk_forward_auc(X, y)
    log.info("Walk-forward mean AUC: %.4f", wf_auc)

    # Final temporal split
    X_train, X_val, y_train, y_val = temporal_split(X, y)

    model = xgb.XGBClassifier(**XGB_PARAMS)
    model.fit(
        X_train, y_train,
        eval_set=[(X_val, y_val)],
        verbose=False,
    )

    val_proba = model.predict_proba(X_val)[:, 1]
    if len(np.unique(y_val)) < 2:
        log.error("Validation set has only one class — cannot compute AUC")
        return None

    val_auc = roc_auc_score(y_val, val_proba)
    log.info("Validation AUC: %.4f (threshold %.2f)", val_auc, MIN_AUC)

    if val_auc < MIN_AUC:
        log.warning("AUC=%.4f < threshold=%.2f — model NOT saved", val_auc, MIN_AUC)
        return None

    # Calibration
    cal = fit_calibrator(val_proba, y_val)
    cal_proba = cal.transform(val_proba)
    ece = expected_calibration_error(y_val, cal_proba)
    log.info("Post-calibration ECE: %.4f", ece)

    if ece >= 0.05:
        log.error("ECE=%.4f >= 0.05 — calibration failed, model NOT saved", ece)
        return None

    # Save
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    model_id = f"xgb_{ts}"
    model_path = str(MODELS_DIR / f"{model_id}.model")
    cal_path = str(MODELS_DIR / f"{model_id}_cal.pkl")

    model.save_model(model_path)
    save_calibrator(cal, cal_path)
    log.info("Model saved: %s", model_path)

    importance = dict(zip(feature_names, model.feature_importances_.tolist()))
    top_importance = dict(
        sorted(importance.items(), key=lambda x: x[1], reverse=True)[:10]
    )

    await _register_model(
        db_path=db_path,
        model_id=model_id,
        training_samples=len(X_train),
        auc_score=val_auc,
        calibration_error=ece,
        feature_names=feature_names,
        feature_importance=top_importance,
        model_path=model_path,
        cal_path=cal_path,
    )

    return model_id


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Train XGBoost model")
    p.add_argument("--db", default="data/kalbot.db")
    p.add_argument("--min-date", default=None)
    p.add_argument("--max-date", default=None)
    p.add_argument("--log-level", default="INFO")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    result = asyncio.run(train(args.db, args.min_date, args.max_date))
    if result:
        print(f"Trained model: {result}")
    else:
        print("Training failed or AUC below threshold")
