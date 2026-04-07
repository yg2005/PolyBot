from __future__ import annotations

import logging
import pickle
from pathlib import Path
from typing import Protocol

import numpy as np
from sklearn.calibration import CalibratedClassifierCV, calibration_curve
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

log = logging.getLogger(__name__)

ECE_THRESHOLD = 0.05
N_BINS = 10


def expected_calibration_error(y_true: np.ndarray, y_prob: np.ndarray, n_bins: int = N_BINS) -> float:
    """Computes ECE: weighted avg abs difference between confidence and accuracy."""
    bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
    ece = 0.0
    n = len(y_true)
    for lo, hi in zip(bin_edges[:-1], bin_edges[1:]):
        mask = (y_prob >= lo) & (y_prob < hi)
        if mask.sum() == 0:
            continue
        acc = y_true[mask].mean()
        conf = y_prob[mask].mean()
        ece += mask.sum() / n * abs(acc - conf)
    return ece


class Calibrator(Protocol):
    def predict_proba(self, X: np.ndarray) -> np.ndarray: ...
    def transform(self, y_prob: np.ndarray) -> np.ndarray: ...


class IsotonicCalibrator:
    """Wraps IsotonicRegression for use after XGBoost predict_proba."""

    def __init__(self) -> None:
        self._iso = IsotonicRegression(out_of_bounds="clip")

    def fit(self, y_prob: np.ndarray, y_true: np.ndarray) -> "IsotonicCalibrator":
        self._iso.fit(y_prob, y_true)
        return self

    def transform(self, y_prob: np.ndarray) -> np.ndarray:
        return self._iso.transform(y_prob).clip(0.0, 1.0)


class PlattCalibrator:
    """Sigmoid (Platt) scaling using logistic regression on log-odds."""

    def __init__(self) -> None:
        self._lr = LogisticRegression(C=1.0, solver="lbfgs")

    def fit(self, y_prob: np.ndarray, y_true: np.ndarray) -> "PlattCalibrator":
        X = y_prob.reshape(-1, 1)
        self._lr.fit(X, y_true)
        return self

    def transform(self, y_prob: np.ndarray) -> np.ndarray:
        X = y_prob.reshape(-1, 1)
        return self._lr.predict_proba(X)[:, 1].clip(0.0, 1.0)


def fit_calibrator(
    y_prob: np.ndarray,
    y_true: np.ndarray,
) -> IsotonicCalibrator | PlattCalibrator:
    """Fits the right calibrator and checks ECE."""
    n = len(y_true)
    if n > 200:
        cal: IsotonicCalibrator | PlattCalibrator = IsotonicCalibrator()
        method = "isotonic"
    else:
        cal = PlattCalibrator()
        method = "platt"

    cal.fit(y_prob, y_true)
    y_cal = cal.transform(y_prob)
    ece = expected_calibration_error(y_true, y_cal)

    log.info("Calibration (%s): ECE=%.4f (threshold %.2f)", method, ece, ECE_THRESHOLD)
    if ece >= ECE_THRESHOLD:
        log.warning("ECE=%.4f exceeds threshold %.2f — model may be miscalibrated", ece, ECE_THRESHOLD)

    return cal


def save_calibrator(cal: IsotonicCalibrator | PlattCalibrator, path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(cal, f)
    log.info("Calibrator saved to %s", path)


def load_calibrator(path: str) -> IsotonicCalibrator | PlattCalibrator:
    with open(path, "rb") as f:
        return pickle.load(f)
