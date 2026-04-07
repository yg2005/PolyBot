from __future__ import annotations

"""Adaptive order execution: maker → improve price → taker escalation.

State machine:
  1. Place limit at (mid - spread/4) as PENDING maker.
  2. After FIRST_IMPROVE_S seconds: if not filled, amend price by +PRICE_STEP.
  3. Every SUBSEQUENT_IMPROVE_S after that: amend again.
  4. If remaining_seconds < TAKER_THRESHOLD and edge > taker_fee → cancel + taker.
  5. If cancel_on_reversal and BTC direction flips → cancel.
"""

import asyncio
import logging
from typing import Callable

from ..config import ExecutionConfig
from ..types import OrderState
from .order_manager import OrderManager, TAKER_FEE

log = logging.getLogger(__name__)

FIRST_IMPROVE_S = 30      # wait before first price improvement
SUBSEQUENT_IMPROVE_S = 10  # interval between subsequent improvements
PRICE_STEP = 0.01          # cents improvement per amendment
TAKER_THRESHOLD_S = 60     # convert to taker when remaining < this


class AdaptiveExecutor:
    """Wraps OrderManager with the maker→taker escalation loop.

    Usage:
        order_id, fill = await adaptive.execute(
            window_id, side, entry_price, size_usd, edge,
            get_remaining=lambda: lifecycle.remaining_seconds,
            get_btc_direction=lambda: tracker.get_features(cl_price).direction,
            initial_direction=snapshot.direction,
        )

    Returns (order_id, fill_price | None) immediately. If fill_price is None,
    the order is PENDING — the escalation loop runs as a background task and
    updates the DB when the order fills, cancels, or converts to taker.
    """

    def __init__(self, order_mgr: OrderManager, cfg: ExecutionConfig) -> None:
        self._mgr = order_mgr
        self._cancel_on_reversal = cfg.cancel_on_reversal
        self._taker_threshold_s = cfg.taker_threshold_seconds
        self._active_tasks: dict[str, asyncio.Task] = {}

    async def execute(
        self,
        window_id: str,
        side: str,
        limit_price: float,
        size_usd: float,
        edge: float,
        get_remaining: Callable[[], int],
        get_btc_direction: Callable[[], int],
        initial_direction: int,
    ) -> tuple[str, float | None]:
        """Place initial maker order and start escalation loop in background.

        limit_price must be pre-computed as (mid - spread/4) for YES, or
        equivalent for NO. The adaptive loop then improves it by PRICE_STEP
        every SUBSEQUENT_IMPROVE_S until filled, cancelled, or converted to taker.
        """
        order_id, fill_price = await self._mgr.place_order(
            window_id, side, "maker", limit_price, size_usd
        )

        if fill_price is not None:
            # Already filled — no escalation needed
            log.info("AdaptiveExecutor %s | immediate fill @ %.4f", order_id, fill_price)
            return order_id, fill_price

        # Start escalation loop as background task
        task = asyncio.create_task(
            self._escalation_loop(
                order_id, window_id, side, limit_price, size_usd,
                edge, get_remaining, get_btc_direction, initial_direction,
            ),
            name=f"AdaptiveEscalation-{order_id}",
        )
        self._active_tasks[order_id] = task
        task.add_done_callback(lambda t: self._active_tasks.pop(order_id, None))
        return order_id, None

    def cancel_all(self) -> None:
        """Cancel all pending escalation loops (call on shutdown)."""
        for task in list(self._active_tasks.values()):
            task.cancel()
        self._active_tasks.clear()

    # ------------------------------------------------------------------ #
    # Escalation state machine                                             #
    # ------------------------------------------------------------------ #

    async def _escalation_loop(
        self,
        order_id: str,
        window_id: str,
        side: str,
        initial_price: float,
        size_usd: float,
        edge: float,
        get_remaining: Callable[[], int],
        get_btc_direction: Callable[[], int],
        initial_direction: int,
    ) -> None:
        current_price = initial_price
        amendment_count = 0

        try:
            # Phase 1: wait FIRST_IMPROVE_S, then improve
            await asyncio.sleep(FIRST_IMPROVE_S)

            while True:
                status = self._mgr.get_order_status(order_id)
                if status is None:
                    log.warning("AdaptiveEscalation %s | order not found", order_id)
                    break
                # In paper mode get_order_status returns a PaperOrder
                state = getattr(status, "state", None)
                if state in (OrderState.FILLED, OrderState.CANCELLED):
                    log.info(
                        "AdaptiveEscalation %s | loop exit state=%s",
                        order_id, state.value if state else "unknown",
                    )
                    break

                remaining = get_remaining()
                current_direction = get_btc_direction()

                # --- Cancel on reversal ---
                if self._cancel_on_reversal and initial_direction != 0:
                    if current_direction != 0 and current_direction != initial_direction:
                        log.info(
                            "AdaptiveEscalation %s | BTC reversed (%+d→%+d) — cancelling",
                            order_id, initial_direction, current_direction,
                        )
                        await self._mgr.cancel_order(order_id, "btc_reversal")
                        break

                # --- Convert to taker if time running out ---
                taker_thresh = max(self._taker_threshold_s, TAKER_THRESHOLD_S)
                if remaining < taker_thresh and edge > TAKER_FEE:
                    log.info(
                        "AdaptiveEscalation %s | remaining=%ds < %ds + edge=%.3f > fee — taker",
                        order_id, remaining, taker_thresh, edge,
                    )
                    cancelled = await self._mgr.cancel_order(order_id, "convert_to_taker")
                    if cancelled:
                        taker_price = current_price + PRICE_STEP * (amendment_count + 1)
                        new_order_id, fill_price = await self._mgr.place_order(
                            window_id, side, "taker", taker_price, size_usd
                        )
                        log.info(
                            "AdaptiveEscalation %s | converted to taker %s fill=%.4f",
                            order_id, new_order_id, fill_price or 0.0,
                        )
                    break

                # --- Improve price ---
                current_price = round(current_price + PRICE_STEP, 4)
                amendment_count += 1
                log.info(
                    "AdaptiveEscalation %s | improve #%d → %.4f remaining=%ds",
                    order_id, amendment_count, current_price, remaining,
                )
                amended = await self._mgr.amend_order(order_id, current_price)
                if not amended:
                    log.info("AdaptiveEscalation %s | amend failed (order may have filled)", order_id)
                    break

                # Check if filled after amendment
                status_after = self._mgr.get_order_status(order_id)
                state_after = getattr(status_after, "state", None)
                if state_after == OrderState.FILLED:
                    log.info(
                        "AdaptiveEscalation %s | filled after amendment #%d",
                        order_id, amendment_count,
                    )
                    break

                await asyncio.sleep(SUBSEQUENT_IMPROVE_S)

        except asyncio.CancelledError:
            log.info("AdaptiveEscalation %s | cancelled", order_id)
            await self._mgr.cancel_order(order_id, "shutdown")
        except Exception as exc:
            log.error("AdaptiveEscalation %s | unexpected error: %s", order_id, exc)
