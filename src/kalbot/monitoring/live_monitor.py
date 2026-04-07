from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

_TIMEOUT = 10.0
_BALANCE_POLL_S = 60


class LiveMonitor:
    """Tracks real USDC balance, actual fill prices vs expected, and fees.

    Runs a background loop that polls CLOB for balance every 60 s and
    accumulates reconciliation data per order.

    Only active when mode == "live". Silently no-ops otherwise.
    """

    def __init__(
        self,
        clob_url: str,
        api_key: str,
        mode: str = "paper",
    ) -> None:
        self._url = clob_url.rstrip("/")
        self._key = api_key
        self._mode = mode
        self._running = False
        self._task: asyncio.Task[None] | None = None

        # State
        self._usdc_balance: float | None = None
        self._balance_last_checked: datetime | None = None
        # order_id → reconciliation dict
        self._fills: dict[str, dict[str, Any]] = {}
        # cumulative actual fees paid
        self._total_fees: float = 0.0

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        if self._mode != "live":
            return
        if not self._key:
            log.warning("LiveMonitor: no API key — balance polling disabled")
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="LiveMonitor")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------ #
    # Balance                                                              #
    # ------------------------------------------------------------------ #

    async def get_usdc_balance(self) -> float | None:
        """Fetch current USDC balance from CLOB. Returns None on error."""
        if self._mode != "live" or not self._key:
            return None
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    f"{self._url}/balance",
                    headers={"Authorization": f"Bearer {self._key}"},
                )
                resp.raise_for_status()
                data = resp.json()
            # CLOB returns {"balance": "123.45"} or {"usdc": "123.45"}
            balance = float(data.get("balance") or data.get("usdc") or 0)
            self._usdc_balance = balance
            self._balance_last_checked = datetime.now(timezone.utc)
            return balance
        except Exception as exc:
            log.error("LiveMonitor balance fetch failed: %s", exc)
            return None

    @property
    def cached_balance(self) -> float | None:
        return self._usdc_balance

    # ------------------------------------------------------------------ #
    # Order status / reconciliation                                        #
    # ------------------------------------------------------------------ #

    async def poll_order(self, order_id: str) -> dict[str, Any] | None:
        """Fetch live order status from CLOB."""
        if self._mode != "live" or not self._key:
            return None
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    f"{self._url}/order/{order_id}",
                    headers={"Authorization": f"Bearer {self._key}"},
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            log.error("LiveMonitor poll_order %s failed: %s", order_id, exc)
            return None

    async def reconcile_fill(
        self,
        order_id: str,
        expected_price: float,
        expected_size_usd: float,
    ) -> dict[str, Any]:
        """Compare actual CLOB fill vs expected. Returns reconciliation dict."""
        raw = await self.poll_order(order_id)
        if raw is None:
            return {"order_id": order_id, "status": "unknown", "reconciled": False}

        actual_price = float(raw.get("price") or raw.get("avgPrice") or expected_price)
        actual_size = float(raw.get("size") or raw.get("sizeMatched") or expected_size_usd)
        actual_fees = float(raw.get("fees") or raw.get("feeAmount") or 0.0)
        status = raw.get("status", "unknown")

        price_slip = actual_price - expected_price
        size_slip = actual_size - expected_size_usd

        rec = {
            "order_id": order_id,
            "status": status,
            "expected_price": expected_price,
            "actual_price": actual_price,
            "price_slippage": price_slip,
            "expected_size_usd": expected_size_usd,
            "actual_size_usd": actual_size,
            "size_slippage": size_slip,
            "actual_fees_usd": actual_fees,
            "reconciled": True,
        }
        self._fills[order_id] = rec
        self._total_fees += actual_fees

        log.info(
            "Reconcile %s | status=%s price_slip=%+.4f size_slip=%+.2f fees=%.4f",
            order_id, status, price_slip, size_slip, actual_fees,
        )

        if abs(price_slip) > 0.01:
            log.warning(
                "LiveMonitor: large price slippage on %s: expected=%.4f actual=%.4f",
                order_id, expected_price, actual_price,
            )

        return rec

    @property
    def total_fees_paid(self) -> float:
        return self._total_fees

    def reconciliation_report(self) -> dict[str, Any]:
        """Summary of all reconciled fills for logging / dashboard."""
        if not self._fills:
            return {"fills": 0, "total_fees": 0.0, "avg_price_slip": 0.0}
        slips = [r["price_slippage"] for r in self._fills.values()]
        return {
            "fills": len(self._fills),
            "total_fees_usd": round(self._total_fees, 4),
            "avg_price_slip": round(sum(slips) / len(slips), 6),
            "max_price_slip": round(max(slips), 6),
            "balance_usdc": self._usdc_balance,
            "balance_checked_at": (
                self._balance_last_checked.isoformat() if self._balance_last_checked else None
            ),
        }

    # ------------------------------------------------------------------ #
    # Background loop                                                      #
    # ------------------------------------------------------------------ #

    async def _poll_loop(self) -> None:
        while self._running:
            await asyncio.sleep(_BALANCE_POLL_S)
            bal = await self.get_usdc_balance()
            if bal is not None:
                log.info("LiveMonitor USDC balance: %.2f", bal)
