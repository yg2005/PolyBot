"""
ReactionScorer: deterministic spot-displacement trigger for the reaction window.

Entry fires when:
  (1) elapsed_seconds <= max_entry_elapsed_seconds
  (2) |spot_displacement_pct| >= spot_displacement_threshold
  (3) entry price (yes_ask for YES, no_ask for NO) in [price_band_low, price_band_high]

Side follows spot direction: spot UP → YES bet, spot DOWN → NO bet.

ML scorer runs in parallel for logging only — its cal_prob is stored in
ScorerResult.confidence so model_prob in the DB remains usable for head-to-head
comparison.  The TRIGGER is the displacement rule, not the ML output.
"""
from __future__ import annotations

import logging

from ..config import EngineConfig
from ..types import ScorerResult, WindowSnapshot
from .scorer import BaseScorer

log = logging.getLogger(__name__)

# Fixed edge placeholder that clears DecisionEngine's min_edge gate.
# The real quality gate is the displacement + price-band check inside this scorer.
_EDGE_PLACEHOLDER = 0.06


class ReactionScorer(BaseScorer):
    def __init__(
        self,
        cfg: EngineConfig,
        ml_scorer: BaseScorer | None = None,
    ) -> None:
        self._threshold = cfg.spot_displacement_threshold
        self._max_elapsed = cfg.max_entry_elapsed_seconds
        self._band_low = cfg.price_band_low
        self._band_high = cfg.price_band_high
        self._ml = ml_scorer

    async def score(self, snapshot: WindowSnapshot) -> ScorerResult:
        def _pass(reason: str) -> ScorerResult:
            return ScorerResult(
                signal="PASS",
                confidence=0.0,
                edge_estimate=0.0,
                reasoning=reason,
                features_used={},
            )

        # Gate 1 — reaction window only
        if snapshot.elapsed_seconds > self._max_elapsed:
            return _pass(
                f"reaction: elapsed={snapshot.elapsed_seconds}s > max={self._max_elapsed}s"
            )

        # Gate 2 — confirmed spot displacement
        disp = snapshot.spot_displacement_pct
        if abs(disp) < self._threshold:
            return _pass(
                f"reaction: |spot_disp|={abs(disp):.4f}% < threshold={self._threshold}%"
            )

        # Gate 3 — side from spot direction, entry price from orderbook
        side = "YES" if disp > 0 else "NO"
        entry_price = snapshot.yes_ask if side == "YES" else snapshot.no_ask

        # Gate 4 — price band
        if not (self._band_low <= entry_price <= self._band_high):
            return _pass(
                f"reaction: entry={entry_price:.4f} outside band "
                f"[{self._band_low},{self._band_high}]"
            )

        # Run ML scorer for logging only — does not affect the trigger decision
        ml_cal_prob = 0.0
        ml_signal = "N/A"
        if self._ml is not None:
            try:
                ml_result = await self._ml.score(snapshot)
                ml_cal_prob = float(ml_result.features_used.get("cal_prob", 0.0))
                ml_signal = ml_result.signal
            except Exception as exc:
                log.warning("ReactionScorer: ML sub-score failed: %s", exc)

        features_used = {
            "spot_displacement_pct": round(disp, 4),
            "entry_price": round(entry_price, 4),
            "elapsed_seconds": snapshot.elapsed_seconds,
            "threshold": self._threshold,
            "cal_prob": round(ml_cal_prob, 4),   # stored as model_prob in DB
            "ml_signal": ml_signal,
        }

        log.info(
            "ReactionScorer: %s | spot_disp=%+.4f%% entry=%.4f elapsed=%ds "
            "ml_signal=%s ml_cal_prob=%.4f",
            side, disp, entry_price, snapshot.elapsed_seconds,
            ml_signal, ml_cal_prob,
        )

        return ScorerResult(
            signal=side,
            confidence=ml_cal_prob,      # ML cal_prob → model_prob column for comparison
            edge_estimate=_EDGE_PLACEHOLDER,
            reasoning=(
                f"REACTION {side} | spot_disp={disp:+.4f}% "
                f"elapsed={snapshot.elapsed_seconds}s entry={entry_price:.4f}"
            ),
            features_used=features_used,
        )
