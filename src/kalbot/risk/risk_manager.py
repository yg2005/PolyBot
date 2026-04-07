from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from ..config import KalbotConfig

log = logging.getLogger(__name__)

# Hard limits — cannot be overridden by config
MAX_POSITION_USD_HARD: float = 50.0
MAX_DAILY_LOSS_HARD: float = 30.0
MAX_DRAWDOWN_HARD_PCT: float = 25.0
MAX_CONSECUTIVE_LOSSES: int = 10


@dataclass
class RiskState:
    daily_pnl: float = 0.0
    peak_bankroll: float = 0.0
    current_bankroll: float = 0.0
    open_positions: int = 0
    consecutive_losses: int = 0
    circuit_breaker_active: bool = False
    circuit_breaker_reason: str = ""
    trade_date: date = field(default_factory=date.today)


class RiskManager:
    def __init__(self, cfg: KalbotConfig) -> None:
        risk = cfg.risk

        # Clamp configurable limits to hard limits
        self._max_position_usd = min(risk.max_position_usd, MAX_POSITION_USD_HARD)
        self._max_daily_loss_usd = min(risk.max_daily_loss_usd, MAX_DAILY_LOSS_HARD)
        self._max_drawdown_pct = min(risk.max_drawdown_pct, MAX_DRAWDOWN_HARD_PCT)
        self._max_concurrent = risk.max_concurrent_positions
        self._starting_bankroll = risk.starting_bankroll_usd

        self._state = RiskState(
            peak_bankroll=self._starting_bankroll,
            current_bankroll=self._starting_bankroll,
        )

        if risk.max_position_usd > MAX_POSITION_USD_HARD:
            log.warning(
                "max_position_usd clamped %s → %s (hard limit)",
                risk.max_position_usd,
                MAX_POSITION_USD_HARD,
            )
        if risk.max_daily_loss_usd > MAX_DAILY_LOSS_HARD:
            log.warning(
                "max_daily_loss_usd clamped %s → %s (hard limit)",
                risk.max_daily_loss_usd,
                MAX_DAILY_LOSS_HARD,
            )
        if risk.max_drawdown_pct > MAX_DRAWDOWN_HARD_PCT:
            log.warning(
                "max_drawdown_pct clamped %s → %s (hard limit)",
                risk.max_drawdown_pct,
                MAX_DRAWDOWN_HARD_PCT,
            )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def can_trade(self, size_usd: float) -> tuple[bool, str]:
        """Check all risk gates. Returns (allowed, reason)."""
        self._maybe_reset_daily()

        if self._state.circuit_breaker_active:
            return False, f"circuit_breaker: {self._state.circuit_breaker_reason}"

        if self._state.open_positions >= self._max_concurrent:
            return False, f"max_concurrent={self._max_concurrent} reached"

        if size_usd > self._max_position_usd:
            return False, f"size={size_usd:.2f} > max_position={self._max_position_usd:.2f}"

        if self._state.daily_pnl <= -self._max_daily_loss_usd:
            return False, f"daily_loss={self._state.daily_pnl:.2f} hit limit={self._max_daily_loss_usd:.2f}"

        drawdown = self._current_drawdown_pct()
        if drawdown >= self._max_drawdown_pct:
            return False, f"drawdown={drawdown:.1f}% >= limit={self._max_drawdown_pct:.1f}%"

        return True, "ok"

    def register_trade(self, size_usd: float) -> None:
        """Call when a new position is opened."""
        self._state.open_positions += 1
        log.debug("position opened size=%.2f open=%d", size_usd, self._state.open_positions)

    def register_settlement(self, pnl: float) -> None:
        """Call when a position settles. pnl can be negative."""
        self._state.open_positions = max(0, self._state.open_positions - 1)
        self._state.daily_pnl += pnl
        self._state.current_bankroll += pnl

        if pnl < 0:
            self._state.consecutive_losses += 1
        else:
            self._state.consecutive_losses = 0

        if self._state.current_bankroll > self._state.peak_bankroll:
            self._state.peak_bankroll = self._state.current_bankroll

        log.info(
            "settlement pnl=%.2f daily=%.2f bankroll=%.2f consecutive_losses=%d",
            pnl,
            self._state.daily_pnl,
            self._state.current_bankroll,
            self._state.consecutive_losses,
        )

        self._check_circuit_breaker()

    def is_circuit_breaker_active(self) -> bool:
        return self._state.circuit_breaker_active

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _current_drawdown_pct(self) -> float:
        if self._state.peak_bankroll <= 0:
            return 0.0
        return (
            (self._state.peak_bankroll - self._state.current_bankroll)
            / self._state.peak_bankroll
            * 100.0
        )

    def _check_circuit_breaker(self) -> None:
        reason: str | None = None

        if self._state.daily_pnl <= -self._max_daily_loss_usd:
            reason = f"daily_loss={self._state.daily_pnl:.2f} >= limit={self._max_daily_loss_usd:.2f}"
        elif self._current_drawdown_pct() >= self._max_drawdown_pct:
            reason = f"drawdown={self._current_drawdown_pct():.1f}% >= limit={self._max_drawdown_pct:.1f}%"
        elif self._state.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            reason = f"consecutive_losses={self._state.consecutive_losses}"

        if reason and not self._state.circuit_breaker_active:
            self._state.circuit_breaker_active = True
            self._state.circuit_breaker_reason = reason
            log.critical("CIRCUIT BREAKER TRIPPED: %s — trading halted, logging continues", reason)

    def _maybe_reset_daily(self) -> None:
        today = date.today()
        if self._state.trade_date != today:
            log.info(
                "new day %s — resetting daily_pnl=%.2f → 0, deactivating circuit breaker",
                today,
                self._state.daily_pnl,
            )
            self._state.daily_pnl = 0.0
            self._state.trade_date = today
            # Reset circuit breaker only if it was triggered by daily loss / drawdown, not consecutive losses
            if self._state.circuit_breaker_active:
                self._state.circuit_breaker_active = False
                self._state.circuit_breaker_reason = ""
            self._state.consecutive_losses = 0

    @property
    def state(self) -> RiskState:
        return self._state

    @property
    def daily_pnl(self) -> float:
        return self._state.daily_pnl
