from __future__ import annotations

import logging

from ..config import KalbotConfig
from ..risk.risk_manager import RiskManager
from ..types import DecisionResult, ScorerResult, WindowSnapshot

log = logging.getLogger(__name__)

KELLY_FRACTION = 0.25
MIN_SIZE_USD = 5.0
MAX_SPREAD_USD = 0.10


class DecisionEngine:
    def __init__(self, cfg: KalbotConfig, risk: RiskManager) -> None:
        self._min_edge_pct = cfg.risk.min_edge_pct
        self._max_position_usd = cfg.risk.max_position_usd
        self._default_size_usd = cfg.execution.default_order_size_usd
        self._bankroll = cfg.risk.starting_bankroll_usd
        self._risk = risk

    def decide(
        self,
        score: ScorerResult,
        snapshot: WindowSnapshot,
    ) -> DecisionResult:
        def _pass(reason: str) -> DecisionResult:
            return DecisionResult(
                action="PASS",
                side=None,
                target_price=None,
                size_usd=None,
                strategy=None,
                pass_reason=reason,
                scorer_result=score,
            )

        # 1. Signal must not be PASS
        if score.signal == "PASS":
            return _pass("scorer=PASS")

        # 2. Edge > min_edge_pct
        edge_pct = score.edge_estimate * 100.0
        if edge_pct < self._min_edge_pct:
            return _pass(f"edge={edge_pct:.2f}% < min={self._min_edge_pct:.1f}%")

        # 3. Risk manager gate (use default size; checks circuit breaker, positions, daily loss)
        allowed, reason = self._risk.can_trade(self._default_size_usd)
        if not allowed:
            return _pass(f"risk: {reason}")

        # 4. Liquidity: bid/ask depth > default order size
        depth = (
            snapshot.bid_depth_usd
            if score.signal == "YES"
            else snapshot.ask_depth_usd
        )
        if depth < self._default_size_usd:
            return _pass(f"depth={depth:.2f} < size={self._default_size_usd:.2f}")

        # 5. Spread < $0.10
        if snapshot.spread > MAX_SPREAD_USD:
            return _pass(f"spread={snapshot.spread:.4f} > max={MAX_SPREAD_USD}")

        # 6. Kelly sizing — computed last; floor $5
        size_usd = self._kelly_size(score, snapshot)
        if size_usd < MIN_SIZE_USD:
            return _pass(f"kelly_size={size_usd:.2f} < floor={MIN_SIZE_USD:.2f}")

        # 7. Strategy selection
        remaining = snapshot.remaining_seconds
        if remaining > 120:
            strategy = "maker"
        elif remaining >= 60:
            strategy = "adaptive"
        else:
            # taker only if edge covers taker fee (approx 0.5% = 0.005)
            if score.edge_estimate < 0.005:
                return _pass(f"taker_edge={edge_pct:.2f}% insufficient for taker fee")
            strategy = "taker"

        # Target entry price: bid for YES, ask for NO
        target_price = snapshot.yes_bid if score.signal == "YES" else snapshot.no_bid

        log.info(
            "TRADE signal=%s edge=%.2f%% size=%.2f strategy=%s remaining=%ds",
            score.signal,
            edge_pct,
            size_usd,
            strategy,
            remaining,
        )

        return DecisionResult(
            action="TRADE",
            side=score.signal,
            target_price=target_price,
            size_usd=size_usd,
            strategy=strategy,
            pass_reason=None,
            scorer_result=score,
        )

    # ------------------------------------------------------------------
    # Kelly sizing
    # ------------------------------------------------------------------

    def _kelly_size(self, score: ScorerResult, snapshot: WindowSnapshot) -> float:
        """f* = (p*b - q) / b, where b = net odds (payout - 1)."""
        p = score.confidence  # P(win)
        q = 1.0 - p

        # mid_price is the market-implied probability; payout = 1/mid_price
        mid = snapshot.mid_price
        if mid <= 0 or mid >= 1:
            return self._default_size_usd

        b = (1.0 / mid) - 1.0  # net odds
        if b <= 0:
            return self._default_size_usd

        f_star = (p * b - q) / b
        if f_star <= 0:
            return 0.0

        raw = f_star * KELLY_FRACTION * self._bankroll
        return max(0.0, min(raw, self._max_position_usd))
