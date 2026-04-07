from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

_START_FILE = Path("data/live_start.json")


class SizeRamp:
    """Gradual position-size ramp for live trading.

    Day 1-3  → 25% of base size
    Day 4-7  → 50%
    Day 8+   → 100%

    Auto de-ramp: if daily_loss >= 50% of max_daily_loss_usd → back to 25%
    regardless of day number.

    Persists the live-start date in data/live_start.json so ramp survives
    restarts.
    """

    def __init__(self, live_start_date: date | None = None) -> None:
        if live_start_date is not None:
            self._start = live_start_date
        else:
            self._start = self._load_or_init_start()

    # ------------------------------------------------------------------ #
    # Public                                                               #
    # ------------------------------------------------------------------ #

    @property
    def live_start_date(self) -> date:
        return self._start

    @property
    def day_number(self) -> int:
        return (date.today() - self._start).days + 1

    def multiplier(self, daily_pnl: float, max_daily_loss_usd: float) -> float:
        """Return size multiplier (0.0–1.0) given current daily P&L."""
        # De-ramp if daily loss >= 50% of the configured limit
        if max_daily_loss_usd > 0 and daily_pnl <= -(max_daily_loss_usd * 0.5):
            log.warning(
                "SizeRamp de-ramp: daily_pnl=%.2f >= 50%% of limit=%.2f → 25%%",
                daily_pnl, max_daily_loss_usd,
            )
            return 0.25

        day = self.day_number
        if day <= 3:
            return 0.25
        if day <= 7:
            return 0.50
        return 1.0

    def apply(self, base_size: float, daily_pnl: float, max_daily_loss_usd: float) -> float:
        """Scale base_size by the current ramp multiplier."""
        m = self.multiplier(daily_pnl, max_daily_loss_usd)
        scaled = round(base_size * m, 2)
        if m < 1.0:
            log.info(
                "SizeRamp day=%d mult=%.2f base=%.2f → %.2f",
                self.day_number, m, base_size, scaled,
            )
        return scaled

    # ------------------------------------------------------------------ #
    # Persistence                                                          #
    # ------------------------------------------------------------------ #

    def _load_or_init_start(self) -> date:
        if _START_FILE.exists():
            try:
                raw = json.loads(_START_FILE.read_text())
                start = date.fromisoformat(raw["start"])
                log.info("SizeRamp: live start date loaded = %s (day %d)", start, (date.today() - start).days + 1)
                return start
            except Exception as exc:
                log.warning("SizeRamp: failed to load start file (%s), resetting", exc)

        today = date.today()
        _START_FILE.parent.mkdir(parents=True, exist_ok=True)
        _START_FILE.write_text(json.dumps({"start": today.isoformat()}))
        log.info("SizeRamp: initialised live start = %s", today)
        return today
