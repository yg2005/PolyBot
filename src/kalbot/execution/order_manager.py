from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from ..config import KalbotConfig
from ..data.db import Database
from .paper import PaperExecutor

if TYPE_CHECKING:
    from ..kill_switch import KillSwitch
    from ..execution.ramp import SizeRamp
    from ..risk.risk_manager import RiskManager

log = logging.getLogger(__name__)

# Taker fee for edge-vs-fee comparisons
TAKER_FEE = 0.005


class OrderManager:
    """Routes orders to paper executor (default) or live Polymarket CLOB.

    Paper mode: all state lives in PaperExecutor (in-memory + SQLite).
    Live mode: POST /order to CLOB API; paper executor is never called.
    """

    def __init__(self, cfg: KalbotConfig, db: Database) -> None:
        self._mode = cfg.execution.mode
        self._paper = PaperExecutor(cfg.execution, db)
        self._clob_url = cfg.feeds.clob_api_url
        self._api_key = cfg.polymarket_api_key
        self._private_key = cfg.polymarket_private_key
        self._nonce = 0
        # Live mode: old_order_id → new_order_id after cancel+replace amend
        self._live_redirects: dict[str, str] = {}
        # Live mode: order_id → (window_id, side, size_usd) for cancel+replace
        self._live_order_meta: dict[str, tuple[str, str, float]] = {}
        # Optional injected dependencies (set after construction)
        self._kill_switch: KillSwitch | None = None
        self._ramp: SizeRamp | None = None
        self._risk: RiskManager | None = None
        self._max_daily_loss_usd: float = cfg.risk.max_daily_loss_usd

    # ------------------------------------------------------------------ #
    # Dependency injection                                                 #
    # ------------------------------------------------------------------ #

    def set_kill_switch(self, ks: KillSwitch) -> None:
        self._kill_switch = ks

    def set_ramp(self, ramp: SizeRamp) -> None:
        self._ramp = ramp

    def set_risk_manager(self, risk: RiskManager) -> None:
        self._risk = risk

    # ------------------------------------------------------------------ #
    # Public interface                                                     #
    # ------------------------------------------------------------------ #

    async def place_order(
        self,
        window_id: str,
        side: str,
        strategy: str,
        price: float,
        size_usd: float,
    ) -> tuple[str, float | None]:
        """Place an order. Returns (order_id, fill_price | None).

        fill_price is None for pending maker orders — poll get_order_status.
        """
        if self._mode == "live":
            return await self._live_place(window_id, side, strategy, price, size_usd)
        return await self._paper.place_order(window_id, side, strategy, price, size_usd)

    async def amend_order(self, order_id: str, new_price: float) -> bool:
        """Improve limit price on a pending order."""
        if self._mode == "live":
            return await self._live_amend(order_id, new_price)
        return await self._paper.amend_order(order_id, new_price)

    async def cancel_order(self, order_id: str, reason: str = "") -> bool:
        """Cancel a pending order."""
        if self._mode == "live":
            return await self._live_cancel(order_id)
        return await self._paper.cancel_order(order_id, reason)

    def get_order_status(self, order_id: str) -> Any:
        """Returns PaperOrder in paper mode.

        In live mode: follows the redirect chain from cancel+replace amends,
        returns a minimal status dict with state="OPEN" if the order is
        active (no better info without a live status poll).
        """
        if self._mode == "live":
            # Follow redirect chain if cancel+replace happened
            current_id = order_id
            seen = set()
            while current_id in self._live_redirects and current_id not in seen:
                seen.add(current_id)
                current_id = self._live_redirects[current_id]
            if current_id in self._live_order_meta:
                return {"order_id": current_id, "state": "OPEN"}
            return None
        return self._paper.get_order_status(order_id)

    def get_fill(self, window_id: str) -> dict | None:
        """Return fill dict for window if a filled order exists."""
        if self._mode == "live":
            return None  # live fills tracked externally
        return self._paper.get_fill(window_id)

    async def settle_positions(self, window_id: str, outcome: str) -> float | None:
        """Compute and log P&L for a settled window."""
        if self._mode == "live":
            return None  # live P&L computed from actual fills
        return await self._paper.settle_positions(window_id, outcome)

    # ------------------------------------------------------------------ #
    # Live CLOB integration (gated behind mode == "live")                 #
    # ------------------------------------------------------------------ #

    async def cancel_all_live(self) -> int:
        """Cancel every tracked live order. Returns count of successful cancels."""
        if self._mode != "live":
            return 0
        count = 0
        for order_id in list(self._live_order_meta.keys()):
            if await self._live_cancel(order_id):
                count += 1
        log.info("cancel_all_live: cancelled %d orders", count)
        return count

    async def _live_place(
        self,
        window_id: str,
        side: str,
        strategy: str,
        price: float,
        size_usd: float,
    ) -> tuple[str, float | None]:
        """POST /order to Polymarket CLOB. Raises if not live-ready."""
        # Kill switch guard
        if self._kill_switch is not None and self._kill_switch.is_engaged():
            raise RuntimeError(
                f"Kill switch is engaged: {self._kill_switch.reason}"
            )

        if not self._api_key or not self._private_key:
            raise RuntimeError(
                "Live mode requires POLYMARKET_API_KEY and POLYMARKET_PRIVATE_KEY env vars."
            )

        # Apply size ramp for live orders
        if self._ramp is not None and self._risk is not None:
            size_usd = self._ramp.apply(
                size_usd,
                self._risk.daily_pnl,
                self._max_daily_loss_usd,
            )

        order_type = "GTC" if strategy in ("maker", "adaptive") else "FOK"
        self._nonce += 1
        payload = {
            "orderType": order_type,
            "tokenID": window_id,  # callers must pass token_id as window_id for live
            "side": side,
            "price": str(round(price, 4)),
            "size": str(round(size_usd, 2)),
            "nonce": self._nonce,
            "feeRateBps": "50",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self._clob_url}/order",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                )
                if self._kill_switch is not None:
                    self._kill_switch.record_api_response(resp.status_code)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            log.error("CLOB order rejected: %s — %s", exc.response.status_code, exc.response.text)
            raise
        except Exception as exc:
            log.error("CLOB place_order failed: %s", exc)
            raise

        if self._kill_switch is not None:
            self._kill_switch.record_internet_ok()

        order_id = data.get("orderID", "") or data.get("id", "")
        status = data.get("status", "unknown")
        fill_price: float | None = None
        if status in ("matched", "filled"):
            fill_price = float(data.get("price", price))
        elif status == "partially_matched":
            # Partial fill: treat as pending for now; full fill will come via event
            fill_price = None
        if order_id:
            self._live_order_meta[order_id] = (window_id, side, size_usd)
        log.info(
            "LiveOrder %s | %s %s @ %.4f size=%.2f status=%s",
            order_id, side, strategy, price, size_usd, status,
        )
        return order_id, fill_price

    async def _live_amend(self, order_id: str, new_price: float) -> bool:
        """Cancel + replace for live CLOB (Polymarket has no native amend).

        Cancels the existing order, places a new one at new_price, and registers
        a redirect so get_order_status(old_id) transparently follows to the new order.
        """
        meta = self._live_order_meta.get(order_id)
        if meta is None:
            log.warning("LiveAmend %s — no metadata found, cannot replace", order_id)
            return False
        window_id, side, size_usd = meta

        cancelled = await self._live_cancel(order_id)
        if not cancelled:
            return False

        try:
            new_id, _ = await self._live_place(window_id, side, "maker", new_price, size_usd)
        except Exception as exc:
            log.error("LiveAmend %s — replacement place_order failed: %s", order_id, exc)
            return False

        # Register redirect: old_id → new_id so adaptive loop status checks follow through
        self._live_redirects[order_id] = new_id
        log.info("LiveAmend %s → %s @ %.4f", order_id, new_id, new_price)
        return True

    async def _live_cancel(self, order_id: str) -> bool:
        if not self._api_key:
            return False
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.delete(
                    f"{self._clob_url}/order/{order_id}",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                if self._kill_switch is not None:
                    self._kill_switch.record_api_response(resp.status_code)
                resp.raise_for_status()
            if self._kill_switch is not None:
                self._kill_switch.record_internet_ok()
            # Remove from meta so cancel_all_live doesn't retry
            self._live_order_meta.pop(order_id, None)
            log.info("LiveCancel %s OK", order_id)
            return True
        except Exception as exc:
            log.error("LiveCancel %s failed: %s", order_id, exc)
            return False
