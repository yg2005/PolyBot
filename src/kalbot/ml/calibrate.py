from __future__ import annotations

import logging
import pickle
from pathlib import Path
from typing import Protocol

import numpy as np
from scipy.optimize import minimize_scalar
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

log = logging.getLogger(__name__)

ECE_THRESHOLD = 0.08
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


class TemperatureCalibrator:
    """Temperature scaling: divides logit by a single learned T.

    Preferred over Platt for temporally-ordered financial data: fits one
    parameter (no base-rate shift), preserves discrimination exactly, and
    doesn't amplify cal-set label-rate drift onto the test set.
    """

    def __init__(self) -> None:
        self.T: float = 1.0

    @staticmethod
    def _sigmoid(x: np.ndarray) -> np.ndarray:
        return 1.0 / (1.0 + np.exp(-x))

    @staticmethod
    def _logit(p: np.ndarray) -> np.ndarray:
        p = np.clip(p, 1e-7, 1.0 - 1e-7)
        return np.log(p / (1.0 - p))

    def _nll(self, T: float, y_prob: np.ndarray, y_true: np.ndarray) -> float:
        scaled = self._sigmoid(self._logit(y_prob) / T)
        scaled = np.clip(scaled, 1e-7, 1.0 - 1e-7)
        return -float(np.mean(y_true * np.log(scaled) + (1 - y_true) * np.log(1 - scaled)))

    def fit(self, y_prob: np.ndarray, y_true: np.ndarray) -> "TemperatureCalibrator":
        res = minimize_scalar(
            lambda T: self._nll(T, y_prob, y_true),
            bounds=(0.1, 10.0),
            method="bounded",
        )
        self.T = float(res.x)
        return self

    def transform(self, y_prob: np.ndarray) -> np.ndarray:
        return self._sigmoid(self._logit(y_prob) / self.T).clip(0.0, 1.0)


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
) -> TemperatureCalibrator:
    """Fits temperature scaling calibrator and checks in-sample ECE.

    Temperature scaling fits one parameter (T) on the calibration set NLL.
    Unlike Platt, it does not shift the base-rate, so temporal label-rate
    drift between the cal and test sets does not corrupt calibration.
    """
    cal = TemperatureCalibrator()
    cal.fit(y_prob, y_true)

    y_cal = cal.transform(y_prob)
    ece = expected_calibration_error(y_true, y_cal)
    log.info(
        "Calibration (temperature T=%.4f) in-sample ECE=%.4f (informational only)",
        cal.T, ece,
    )
    if ece >= ECE_THRESHOLD:
        log.warning("Even in-sample ECE=%.4f >= %.2f — severe miscalibration", ece, ECE_THRESHOLD)

    return cal


def save_calibrator(cal: TemperatureCalibrator, path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(cal, f)
    log.info("Calibrator saved to %s", path)


def load_calibrator(path: str) -> TemperatureCalibrator:
    with open(path, "rb") as f:
        return pickle.load(f)
