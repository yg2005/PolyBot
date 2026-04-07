from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import aiosqlite

from ..config import ExecutionConfig
from ..data.db import Database
from ..types import OrderState

log = logging.getLogger(__name__)

_MAKER_FILL_RATE = 0.70
_PARTIAL_FILL_CHANCE = 0.25      # 25% of maker fills are partial first
_PARTIAL_FILL_PCT_MIN = 0.40
_PARTIAL_FILL_PCT_MAX = 0.80
_MAKER_LATENCY_MS_MIN = 50
_MAKER_LATENCY_MS_MAX = 200

# Taker fee approximation for slippage tracking
_TAKER_FEE = 0.005


@dataclass
class PaperOrder:
    order_id: str
    window_id: str
    side: str
    order_type: str   # "maker", "taker", "adaptive"
    price: float
    size_usd: float
    state: OrderState
    fill_price: float | None = None
    fill_pct: float = 0.0   # 0.0–1.0 for partial fills
    placed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    filled_at: datetime | None = None
    amendments: int = 0
    cancel_reason: str | None = None
    slippage: float = 0.0


class PaperExecutor:
    """Simulates order execution without hitting live exchange.

    Supports:
    - taker: instant fill at entry price
    - maker: PENDING state, probabilistic fill, partial fills
    - amend_order: re-price a pending maker order
    - cancel_order: cancel a pending order
    - get_order_status: inspect any order by order_id
    """

    def __init__(self, cfg: ExecutionConfig, db: Database) -> None:
        self._cfg = cfg
        self._db = db
        # order_id → PaperOrder
        self._orders: dict[str, PaperOrder] = {}
        # window_id → order_id (most recent filled order for that window)
        self._filled: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # Place                                                                #
    # ------------------------------------------------------------------ #

    async def place_order(
        self,
        window_id: str,
        side: str,
        strategy: str,
        entry_price: float,
        size_usd: float,
    ) -> tuple[str, float | None]:
        """Place a paper order. Returns (order_id, fill_price | None).

        Taker fills immediately. Maker enters PENDING — caller must poll
        get_order_status or await simulate_maker_fill for the result.
        """
        order_id = uuid.uuid4().hex[:8]
        now = datetime.now(timezone.utc)

        if strategy == "taker":
            order = PaperOrder(
                order_id=order_id,
                window_id=window_id,
                side=side,
                order_type="taker",
                price=entry_price,
                size_usd=size_usd,
                state=OrderState.FILLED,
                fill_price=entry_price,
                fill_pct=1.0,
                placed_at=now,
                filled_at=now,
                slippage=_TAKER_FEE * entry_price,
            )
            self._orders[order_id] = order
            self._filled[window_id] = order_id
            await self._upsert_order_db(order)
            log.info(
                "PaperOrder %s | TAKER %s @ %.4f size=$%.2f FILLED",
                order_id, side, entry_price, size_usd,
            )
            return order_id, entry_price

        # maker / adaptive — PENDING, will fill probabilistically
        order = PaperOrder(
            order_id=order_id,
            window_id=window_id,
            side=side,
            order_type=strategy,
            price=entry_price,
            size_usd=size_usd,
            state=OrderState.PENDING,
            placed_at=now,
        )
        self._orders[order_id] = order
        await self._upsert_order_db(order)

        # Attempt immediate probabilistic fill for maker
        fill_price = await self._attempt_maker_fill(order)
        log.info(
            "PaperOrder %s | MAKER %s @ %.4f size=$%.2f status=%s latency=%dms",
            order_id, side, entry_price, size_usd,
            order.state.value,
            random.randint(_MAKER_LATENCY_MS_MIN, _MAKER_LATENCY_MS_MAX),
        )
        return order_id, fill_price

    # ------------------------------------------------------------------ #
    # Amend                                                                #
    # ------------------------------------------------------------------ #

    async def amend_order(self, order_id: str, new_price: float) -> bool:
        """Improve limit price on a PENDING order. Returns True if amended."""
        order = self._orders.get(order_id)
        if order is None or order.state != OrderState.PENDING:
            return False
        old_price = order.price
        order.price = new_price
        order.amendments += 1
        log.info(
            "PaperOrder %s | AMEND %.4f → %.4f (amendment #%d)",
            order_id, old_price, new_price, order.amendments,
        )
        await self._upsert_order_db(order)
        # Re-attempt fill with improved price
        await self._attempt_maker_fill(order)
        return True

    # ------------------------------------------------------------------ #
    # Cancel                                                               #
    # ------------------------------------------------------------------ #

    async def cancel_order(self, order_id: str, reason: str = "") -> bool:
        """Cancel a PENDING or OPEN order. Returns True if cancelled."""
        order = self._orders.get(order_id)
        if order is None or order.state not in (OrderState.PENDING, OrderState.OPEN):
            return False
        order.state = OrderState.CANCELLED
        order.cancel_reason = reason
        log.info("PaperOrder %s | CANCELLED reason=%r", order_id, reason)
        await self._upsert_order_db(order)
        return True

    # ------------------------------------------------------------------ #
    # Query                                                                #
    # ------------------------------------------------------------------ #

    def get_order_status(self, order_id: str) -> PaperOrder | None:
        return self._orders.get(order_id)

    def get_fill(self, window_id: str) -> dict | None:
        """Return fill dict for a window if it has a filled order, else None."""
        order_id = self._filled.get(window_id)
        if order_id is None:
            # Also check by scanning orders for this window (for legacy callers)
            for oid, o in self._orders.items():
                if o.window_id == window_id and o.state == OrderState.FILLED:
                    self._filled[window_id] = oid
                    return {
                        "side": o.side,
                        "fill_price": o.fill_price,
                        "size_usd": o.size_usd,
                        "status": o.state.value,
                        "order_id": oid,
                        "fill_pct": o.fill_pct,
                        "slippage": o.slippage,
                        "amendments": o.amendments,
                        "order_type": o.order_type,
                    }
            return None
        order = self._orders.get(order_id)
        if order and order.state == OrderState.FILLED:
            return {
                "side": order.side,
                "fill_price": order.fill_price,
                "size_usd": order.size_usd,
                "status": order.state.value,
                "order_id": order_id,
                "fill_pct": order.fill_pct,
                "slippage": order.slippage,
                "amendments": order.amendments,
                "order_type": order.order_type,
            }
        return None

    # ------------------------------------------------------------------ #
    # Settlement                                                           #
    # ------------------------------------------------------------------ #

    async def settle_positions(self, window_id: str, outcome: str) -> float | None:
        """Compute P&L for the filled position in this window."""
        pos = self.get_fill(window_id)
        if pos is None:
            return None
        fill_price = pos["fill_price"]
        if fill_price is None:
            return None
        pnl = _compute_pnl(pos["side"], fill_price, outcome, pos["size_usd"])
        log.info(
            "PaperSettle | window=%s side=%s fill=%.4f outcome=%s pnl=%+.4f",
            window_id, pos["side"], fill_price, outcome, pnl,
        )
        return pnl

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    async def _attempt_maker_fill(self, order: PaperOrder) -> float | None:
        """Try to fill a PENDING maker order. Returns fill_price if filled."""
        if order.state != OrderState.PENDING:
            return None
        # Fill probability increases with amendments (price improvement)
        base_rate = _MAKER_FILL_RATE
        amendment_bonus = min(order.amendments * 0.05, 0.25)
        fill_prob = base_rate + amendment_bonus

        if random.random() >= fill_prob:
            return None

        now = datetime.now(timezone.utc)
        # Partial fill check
        if random.random() < _PARTIAL_FILL_CHANCE:
            fill_pct = random.uniform(_PARTIAL_FILL_PCT_MIN, _PARTIAL_FILL_PCT_MAX)
            order.state = OrderState.PARTIAL
            order.fill_pct = fill_pct
            order.fill_price = order.price
            log.info(
                "PaperOrder %s | PARTIAL %.0f%% @ %.4f",
                order.order_id, fill_pct * 100, order.price,
            )
            # Log PARTIAL to DB, then immediately resolve the remainder
            await self._upsert_order_db(order)
            order.state = OrderState.FILLED
            order.fill_pct = 1.0
            order.filled_at = now
        else:
            order.state = OrderState.FILLED
            order.fill_price = order.price
            order.fill_pct = 1.0
            order.filled_at = now

        order.slippage = abs(order.fill_price - order.price)
        self._filled[order.window_id] = order.order_id
        await self._upsert_order_db(order)
        log.info(
            "PaperOrder %s | FILLED @ %.4f (amendments=%d)",
            order.order_id, order.fill_price, order.amendments,
        )
        return order.fill_price

    async def _upsert_order_db(self, order: PaperOrder) -> None:
        try:
            async with aiosqlite.connect(self._db._path) as db:
                await db.execute(
                    """INSERT OR REPLACE INTO orders
                       (order_id, window_id, side, order_type, price, size,
                        status, placed_at, filled_at, fill_price, fees, cancel_reason)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        order.order_id,
                        order.window_id,
                        order.side,
                        order.order_type,
                        order.price,
                        order.size_usd,
                        order.state.value,
                        order.placed_at.isoformat(),
                        order.filled_at.isoformat() if order.filled_at else None,
                        order.fill_price,
                        order.slippage,
                        order.cancel_reason,
                    ),
                )
                await db.commit()
        except Exception as exc:
            log.error("PaperExecutor DB write failed: %s", exc)


def _compute_pnl(side: str, entry_price: float, outcome: str, size_usd: float) -> float:
    if entry_price <= 0:
        return 0.0
    contracts = size_usd / entry_price
    if side == "YES":
        return (1.0 - entry_price) * contracts if outcome == "YES" else -entry_price * contracts
    else:
        return (1.0 - entry_price) * contracts if outcome == "NO" else -entry_price * contracts
