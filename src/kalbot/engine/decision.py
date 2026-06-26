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
        self._max_live_stake_usd = cfg.execution.max_live_stake_usd
        self._bankroll = cfg.risk.starting_bankroll_usd
        self._max_entry_price = cfg.risk.max_entry_price
        self._risk = risk

    def decide(
        self,
        score: ScorerResult,
        snapshot: WindowSnapshot,
    ) -> DecisionResult:
        def _pass(reason: str) -> DecisionResult:
            log.info(
                "GATE BLOCK | signal=%s elapsed=%ds | %s",
                score.signal, snapshot.elapsed_seconds, reason,
            )
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
            return _pass(f"gate2_edge: {edge_pct:.2f}% < min={self._min_edge_pct:.1f}%")

        # 3. Risk manager gate (use default size; checks circuit breaker, positions, daily loss)
        allowed, risk_reason = self._risk.can_trade(self._default_size_usd)
        if not allowed:
            return _pass(f"gate3_risk: {risk_reason}")

        # 4. Liquidity: bid/ask depth > default order size
        depth = (
            snapshot.bid_depth_usd
            if score.signal == "YES"
            else snapshot.ask_depth_usd
        )
        if depth < self._default_size_usd:
            return _pass(
                f"gate4_depth: {depth:.2f} < min={self._default_size_usd:.2f} "
                f"(bid={snapshot.bid_depth_usd:.2f} ask={snapshot.ask_depth_usd:.2f})"
            )

        # 5. Spread < $0.10
        if snapshot.spread > MAX_SPREAD_USD:
            return _pass(f"gate5_spread: {snapshot.spread:.4f} > max={MAX_SPREAD_USD}")

        # 5b. Entry price cap: skip expensive tokens where 75% win rate is negative EV
        entry_ask = snapshot.yes_ask if score.signal == "YES" else snapshot.no_ask
        if entry_ask > self._max_entry_price:
            return _pass(
                f"gate5b_price_cap: {score.signal} ask={entry_ask:.4f} > max={self._max_entry_price:.2f}"
            )

        # 5c. Taker fee check: edge must exceed predictable taker cost
        taker_fee_pct = 0.072 * entry_ask * (1.0 - entry_ask) * 100.0
        net_edge_pct = edge_pct - taker_fee_pct
        if net_edge_pct < self._min_edge_pct:
            return _pass(
                f"gate5c_net_edge: {net_edge_pct:.2f}% < min={self._min_edge_pct:.1f}% "
                f"(gross={edge_pct:.2f}% fee={taker_fee_pct:.2f}%)"
            )

        # 6. Kelly sizing — computed last; floor $5, hard ceiling from config
        # Clamp up to floor rather than reject: a $4.88 Kelly is still positive edge.
        # Only reject at zero (f_star <= 0 means no edge by the model).
        size_usd = self._kelly_size(score, snapshot)
        if size_usd <= 0:
            return _pass(
                f"gate6_kelly: f_star<=0 "
                f"(signal={score.signal} conf={score.confidence:.3f} mid={snapshot.mid_price:.3f})"
            )
        size_usd = max(MIN_SIZE_USD, min(size_usd, self._max_live_stake_usd))

        # 7. Always taker — deterministic fill at the ask
        strategy = "taker"

        # Target entry price: ask for both sides
        target_price = snapshot.yes_ask if score.signal == "YES" else snapshot.no_ask

        log.info(
            "TRADE signal=%s edge=%.2f%% size=%.2f strategy=%s",
            score.signal,
            edge_pct,
            size_usd,
            strategy,
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
        """f* = (p_win * b - p_lose) / b, where b = net odds (payout - 1).

        score.confidence is ml_cal_prob = P(YES wins).
        For a YES bet: p_win = cal_prob, market mid = yes_mid.
        For a NO bet:  p_win = 1 - cal_prob, market mid = 1 - yes_mid (NO token implied prob).
        """
        is_yes = score.signal == "YES"
        p = score.confidence if is_yes else 1.0 - score.confidence
        q = 1.0 - p

        mid_yes = snapshot.mid_price
        if mid_yes <= 0 or mid_yes >= 1:
            return self._default_size_usd

        # Implied probability for the side being bet on
        mid = mid_yes if is_yes else 1.0 - mid_yes
        b = (1.0 / mid) - 1.0  # net odds
        if b <= 0:
            return self._default_size_usd

        f_star = (p * b - q) / b
        log.debug(
            "Kelly: signal=%s conf=%.3f p_win=%.3f mid=%.3f b=%.3f f*=%.3f",
            score.signal, score.confidence, p, mid, b, f_star,
        )
        if f_star <= 0:
            return 0.0

        raw = f_star * KELLY_FRACTION * self._bankroll
        return max(0.0, min(raw, self._max_position_usd))
