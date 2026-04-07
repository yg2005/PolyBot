from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from kalbot.engine.window_tracker import WindowFeatures, WindowTracker
from kalbot.types import ScorerResult, WindowSnapshot

log = logging.getLogger(__name__)

# Thresholds (mirrors config defaults — kept as constants per CLAUDE.md)
MIN_ELAPSED = 60
MAX_ELAPSED = 270
MIN_REMAINING = 30
MAX_CROSS = 6
MIN_ABS_MOVE_PCT = 0.02
MIN_DIRECTION_CONSISTENCY = 0.60
MIN_YES_BID = 0.20
MAX_YES_BID = 0.80
MIN_TIME_ABOVE_YES = 0.55
MAX_TIME_ABOVE_NO = 0.45
SPOT_TREND_CONFLICT = 0.0008  # 0.08%

# Edge formula params
EDGE_BASE = 0.003
EDGE_MOVE_SCALE = 0.015
EDGE_CONSISTENCY_BONUS = 0.008
EDGE_MOVE_CAP = 0.3   # 0.3% normalises to 1.0

# Confidence TTC boosts
BOOST_UNDER_90S = 1.15
BOOST_UNDER_180S = 1.08
CONF_CAP = 0.95


class BaseScorer(ABC):
    @abstractmethod
    async def score(self, snapshot: WindowSnapshot) -> ScorerResult: ...


def _quality_confidence(strength: float) -> float:
    """Maps [0,1] strength to ~0.55–0.75 confidence range."""
    return 0.55 + strength * 0.20


class RuleScorer(BaseScorer):
    """17-step waterfall. All gates must pass."""

    def __init__(
        self,
        tracker: WindowTracker,
        series_ticker: str = "POLYBTC5M",
        chainlink_stale_threshold_s: float = 10.0,
    ) -> None:
        self._tracker = tracker
        self._series_ticker = series_ticker
        self._stale_threshold_s = chainlink_stale_threshold_s

    async def score(
        self,
        snapshot: WindowSnapshot,
        features: WindowFeatures | None = None,
        market_ticker: str | None = None,
        chainlink_age_s: float = 0.0,
    ) -> ScorerResult:
        def _pass(reason: str) -> ScorerResult:
            return ScorerResult(
                signal="PASS",
                confidence=0.0,
                edge_estimate=0.0,
                reasoning=reason,
                features_used={},
            )

        # Gate 1: BTC5M market
        ticker = market_ticker or ""
        if self._series_ticker not in ticker and self._series_ticker != ticker:
            # allow if not provided (backwards compat in tests)
            if ticker:
                return _pass(f"gate1: not BTC5M market ({ticker})")

        # Gate 2: chainlink freshness (spot fallback acceptable)
        if chainlink_age_s >= self._stale_threshold_s:
            if not snapshot.spot_confirms:
                return _pass(f"gate2: chainlink stale ({chainlink_age_s:.1f}s) and no spot confirm")

        # Gate 3: remaining_seconds >= 30
        if snapshot.remaining_seconds < MIN_REMAINING:
            return _pass(f"gate3: remaining={snapshot.remaining_seconds}s < {MIN_REMAINING}")

        # Gate 4: elapsed <= 270
        if snapshot.elapsed_seconds > MAX_ELAPSED:
            return _pass(f"gate4: elapsed={snapshot.elapsed_seconds}s > {MAX_ELAPSED}")

        # Gate 5: window features available
        if features is None:
            features = self._tracker.get_features(snapshot.snapshot_price)
        if features is None:
            return _pass("gate5: no window features (insufficient data)")

        # Gate 6: elapsed >= 60
        if features.elapsed_seconds < MIN_ELAPSED:
            return _pass(f"gate6: elapsed={features.elapsed_seconds:.0f}s < {MIN_ELAPSED}")

        # Gate 7: cross_count <= 6
        if features.cross_count > MAX_CROSS:
            return _pass(f"gate7: cross_count={features.cross_count} > {MAX_CROSS}")

        # Gate 8: abs(btc_move_pct) >= 0.02
        if features.abs_displacement_pct < MIN_ABS_MOVE_PCT:
            return _pass(f"gate8: abs_move={features.abs_displacement_pct:.4f}% < {MIN_ABS_MOVE_PCT}%")

        # Gate 9: direction_consistency >= 0.60
        if features.direction_consistency < MIN_DIRECTION_CONSISTENCY:
            return _pass(
                f"gate9: consistency={features.direction_consistency:.2f} < {MIN_DIRECTION_CONSISTENCY}"
            )

        # Gate 10: yes_bid in [0.20, 0.80]
        if not (MIN_YES_BID <= snapshot.yes_bid <= MAX_YES_BID):
            return _pass(f"gate10: yes_bid={snapshot.yes_bid:.2f} out of [{MIN_YES_BID},{MAX_YES_BID}]")

        # Determine direction
        side = "YES" if features.btc_move_since_open > 0 else "NO"

        # Gate 11/12: time_above_pct thresholds
        if side == "YES" and snapshot.time_above_pct < MIN_TIME_ABOVE_YES:
            return _pass(
                f"gate11: time_above={snapshot.time_above_pct:.2f} < {MIN_TIME_ABOVE_YES} for YES"
            )
        if side == "NO" and snapshot.time_above_pct > MAX_TIME_ABOVE_NO:
            return _pass(
                f"gate12: time_above={snapshot.time_above_pct:.2f} > {MAX_TIME_ABOVE_NO} for NO"
            )

        # Gate 13/14: spot trend conflict
        spot_trend = snapshot.spot_trend_1m
        if side == "YES" and spot_trend < -SPOT_TREND_CONFLICT:
            return _pass(f"gate13: spot_trend={spot_trend:.4f} conflicts YES")
        if side == "NO" and spot_trend > SPOT_TREND_CONFLICT:
            return _pass(f"gate14: spot_trend={spot_trend:.4f} conflicts NO")

        # All gates passed — compute edge and confidence
        move_strength = min(1.0, features.abs_displacement_pct / EDGE_MOVE_CAP)
        consistency_bonus = max(0.0, features.direction_consistency - MIN_DIRECTION_CONSISTENCY)
        edge = EDGE_BASE + move_strength * EDGE_MOVE_SCALE + consistency_bonus * EDGE_CONSISTENCY_BONUS

        strength = min(1.0, move_strength * 0.6 + features.direction_consistency * 0.4)
        conf = _quality_confidence(strength)

        # TTC boost
        remaining = snapshot.remaining_seconds
        if remaining < 90:
            conf *= BOOST_UNDER_90S
        elif remaining < 180:
            conf *= BOOST_UNDER_180S
        conf = min(CONF_CAP, conf)

        feats_used = {
            "btc_move_pct": features.btc_move_pct,
            "direction_consistency": features.direction_consistency,
            "cross_count": features.cross_count,
            "time_above_pct": snapshot.time_above_pct,
            "elapsed_seconds": features.elapsed_seconds,
            "remaining_seconds": snapshot.remaining_seconds,
            "spot_trend_1m": spot_trend,
        }

        log.info(
            "RuleScorer: %s | edge=%.4f conf=%.3f move_pct=%.4f",
            side, edge, conf, features.btc_move_pct,
        )

        return ScorerResult(
            signal=side,
            confidence=conf,
            edge_estimate=edge,
            reasoning=(
                f"{side} signal | move={features.btc_move_pct:.3f}% "
                f"consistency={features.direction_consistency:.2f} "
                f"elapsed={features.elapsed_seconds:.0f}s"
            ),
            features_used=feats_used,
        )
