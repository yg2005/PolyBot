from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import xgboost as xgb

from kalbot.engine.scorer import BaseScorer
from kalbot.ml.calibrate import IsotonicCalibrator, PlattCalibrator, load_calibrator
from kalbot.ml.features import FEATURE_COLS
from kalbot.types import ScorerResult, WindowSnapshot

log = logging.getLogger(__name__)

# Minimum calibrated-prob edge over market mid to emit a signal
MIN_EDGE = 0.03       # 3% edge
CONF_SCALE = 0.90     # cap confidence slightly below 1.0


def _snapshot_to_features(snap: WindowSnapshot) -> np.ndarray:
    """Extract the 25 FEATURE_COLS from a live WindowSnapshot."""
    ts = snap.snapshot_time
    hour = ts.hour + ts.minute / 60.0
    dow = ts.weekday()

    displacement_per_cross = snap.displacement_pct / (snap.cross_count + 1)
    momentum_score = snap.velocity * snap.direction_consistency
    market_implied_move = (snap.mid_price - 0.5) * 0.02
    market_disagreement = abs(snap.displacement_pct - market_implied_move)
    chainlink_spot_divergence = abs(snap.displacement_pct - snap.spot_displacement_pct)

    row = [
        snap.displacement_pct,
        snap.abs_displacement_pct,
        snap.direction_consistency,
        float(snap.cross_count),
        snap.time_above_pct,
        snap.velocity,
        snap.acceleration,
        snap.max_displacement_pct,
        snap.min_displacement_pct,
        snap.distance_from_low,
        snap.mid_price,
        snap.spread,
        snap.depth_imbalance,
        snap.market_move_speed,
        float(snap.spot_confirms),
        snap.spot_displacement_pct,
        snap.spot_trend_1m,
        chainlink_spot_divergence,
        float(snap.elapsed_seconds),
        float(snap.remaining_seconds),
        hour,
        float(dow),
        displacement_per_cross,
        momentum_score,
        market_disagreement,
    ]
    assert len(row) == len(FEATURE_COLS), f"Feature count mismatch: {len(row)} vs {len(FEATURE_COLS)}"
    return np.array(row, dtype=float)


class MLScorer(BaseScorer):
    """XGBoost + calibration scorer. Drop-in replacement for RuleScorer."""

    def __init__(
        self,
        model_path: str,
        calibrator_path: str,
        min_edge: float = MIN_EDGE,
    ) -> None:
        self._model = xgb.XGBClassifier()
        self._model.load_model(model_path)
        self._cal: IsotonicCalibrator | PlattCalibrator = load_calibrator(calibrator_path)
        self._min_edge = min_edge
        log.info("MLScorer loaded: model=%s cal=%s", model_path, calibrator_path)

    @classmethod
    def from_registry_row(cls, row: dict, min_edge: float = MIN_EDGE) -> "MLScorer":
        """Build from a model_registry DB row."""
        paths = json.loads(row["model_path"])
        return cls(paths["model"], paths["calibrator"], min_edge=min_edge)

    async def score(self, snapshot: WindowSnapshot) -> ScorerResult:
        def _pass(reason: str) -> ScorerResult:
            return ScorerResult(
                signal="PASS",
                confidence=0.0,
                edge_estimate=0.0,
                reasoning=reason,
                features_used={},
            )

        # Elapsed guard — CRITICAL per CLAUDE.md
        if snapshot.elapsed_seconds > 330:
            log.critical("elapsed_seconds=%d > 330 — BLOCKING trade", snapshot.elapsed_seconds)
            return _pass("critical: elapsed > 330")

        try:
            feats = _snapshot_to_features(snapshot)
        except Exception as exc:
            log.error("Feature extraction failed: %s", exc)
            return _pass(f"feature_error: {exc}")

        X = feats.reshape(1, -1)
        raw_prob = float(self._model.predict_proba(X)[0, 1])
        cal_prob = float(self._cal.transform(np.array([raw_prob]))[0])

        mid = snapshot.mid_price
        if mid <= 0 or mid >= 1:
            return _pass(f"invalid mid_price={mid}")

        yes_edge = cal_prob - mid
        no_edge = (1.0 - cal_prob) - (1.0 - mid)  # = mid - cal_prob

        features_used = {
            "cal_prob": round(cal_prob, 4),
            "raw_prob": round(raw_prob, 4),
            "mid_price": round(mid, 4),
            "yes_edge": round(yes_edge, 4),
            "no_edge": round(no_edge, 4),
            "elapsed_seconds": snapshot.elapsed_seconds,
        }

        if yes_edge >= self._min_edge:
            signal = "YES"
            edge = yes_edge
            conf = min(CONF_SCALE, cal_prob)
        elif no_edge >= self._min_edge:
            signal = "NO"
            edge = no_edge
            conf = min(CONF_SCALE, 1.0 - cal_prob)
        else:
            return ScorerResult(
                signal="PASS",
                confidence=0.0,
                edge_estimate=max(yes_edge, no_edge),
                reasoning=f"insufficient edge: yes={yes_edge:.4f} no={no_edge:.4f} min={self._min_edge:.2f}",
                features_used=features_used,
            )

        log.info(
            "MLScorer: %s | cal_prob=%.4f mid=%.4f edge=%.4f conf=%.3f elapsed=%ds",
            signal, cal_prob, mid, edge, conf, snapshot.elapsed_seconds,
        )

        return ScorerResult(
            signal=signal,
            confidence=conf,
            edge_estimate=edge,
            reasoning=(
                f"{signal} | cal_prob={cal_prob:.4f} mid={mid:.4f} "
                f"edge={edge:.4f} elapsed={snapshot.elapsed_seconds}s"
            ),
            features_used=features_used,
        )
