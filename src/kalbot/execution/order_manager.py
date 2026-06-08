from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ..config import KalbotConfig
from ..data.db import Database
from .paper import PaperExecutor, _compute_pnl

if TYPE_CHECKING:
    from ..kill_switch import KillSwitch
    from ..execution.ramp import SizeRamp
    from ..risk.risk_manager import RiskManager

log = logging.getLogger(__name__)

TAKER_FEE = 0.005


class OrderManager:
    """Routes orders to paper executor (default) or live Polymarket CLOB.

    Paper mode: all state lives in PaperExecutor (in-memory + SQLite).
    Live mode: uses polymarket-client for order placement and cancellation.
    """

    def __init__(self, cfg: KalbotConfig, db: Database) -> None:
        self._mode         = cfg.execution.mode
        self._paper        = PaperExecutor(cfg.execution, db)
        self._private_key  = cfg.polymarket_private_key
        self._proxy_wallet = cfg.polymarket_proxy_wallet or None  # None → SDK auto-derives deposit wallet
        self._live_redirects:  dict[str, str]                    = {}
        self._live_order_meta: dict[str, tuple[str, str, float]] = {}
        # window_id → fill dict (live mode P&L tracking)
        self._live_fills: dict[str, dict] = {}
        self._kill_switch: KillSwitch | None = None
        self._ramp:        SizeRamp | None   = None
        self._risk:        RiskManager | None = None
        self._max_daily_loss_usd = cfg.risk.max_daily_loss_usd
        self._clob_client = None  # lazily initialised in live mode

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
        token_id: str | None = None,
    ) -> tuple[str, float | None]:
        if self._mode == "live":
            return await self._live_place(window_id, side, strategy, price, size_usd, token_id)
        return await self._paper.place_order(window_id, side, strategy, price, size_usd)

    async def amend_order(self, order_id: str, new_price: float) -> bool:
        if self._mode == "live":
            return await self._live_amend(order_id, new_price)
        return await self._paper.amend_order(order_id, new_price)

    async def cancel_order(self, order_id: str, reason: str = "") -> bool:
        if self._mode == "live":
            return await self._live_cancel(order_id)
        return await self._paper.cancel_order(order_id, reason)

    def get_order_status(self, order_id: str) -> Any:
        if self._mode == "live":
            current_id = order_id
            seen: set[str] = set()
            while current_id in self._live_redirects and current_id not in seen:
                seen.add(current_id)
                current_id = self._live_redirects[current_id]
            if current_id in self._live_order_meta:
                return {"order_id": current_id, "state": "OPEN"}
            return None
        return self._paper.get_order_status(order_id)

    def get_fill(self, window_id: str) -> dict | None:
        if self._mode == "live":
            return self._live_fills.get(window_id)
        return self._paper.get_fill(window_id)

    async def settle_positions(self, window_id: str, outcome: str) -> float | None:
        if self._mode == "live":
            pos = self._live_fills.get(window_id)
            if pos is None or pos.get("fill_price") is None:
                return None
            return _compute_pnl(pos["side"], pos["fill_price"], outcome, pos["size_usd"])
        return await self._paper.settle_positions(window_id, outcome)

    # ------------------------------------------------------------------ #
    # Live CLOB integration                                               #
    # ------------------------------------------------------------------ #

    def _get_clob_client(self):
        """Return a cached SecureClient (lazy init)."""
        if self._clob_client is not None:
            return self._clob_client

        from polymarket import SecureClient

        if not self._private_key:
            raise RuntimeError("Live mode requires POLYMARKET_PRIVATE_KEY")

        client = SecureClient.create(
            private_key=self._private_key,
            wallet=self._proxy_wallet,
        )
        log.info(
            "SecureClient ready: wallet=%s  (proxy_wallet=%s)",
            client.wallet, self._proxy_wallet or "auto-derived",
        )
        self._clob_client = client
        return client

    async def cancel_all_live(self) -> int:
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
        token_id: str | None = None,
    ) -> tuple[str, float | None]:
        if self._kill_switch is not None and self._kill_switch.is_engaged():
            raise RuntimeError(f"Kill switch engaged: {self._kill_switch.reason}")

        if self._ramp is not None and self._risk is not None:
            size_usd = self._ramp.apply(
                size_usd, self._risk.daily_pnl, self._max_daily_loss_usd
            )

        from polymarket import AcceptedOrder

        if token_id is None:
            raise RuntimeError(
                f"token_id is required for live orders (window_id={window_id}). "
                "Pass market.yes_token_id or market.no_token_id at the call site."
            )

        # CLOB requires price on 0.01 tick grid (max 2 decimal places)
        price = round(price, 2)
        # size is ConditionalTokens (shares), not USDC
        shares = size_usd / price
        clob_side = "BUY" if side == "YES" else "SELL"
        clob = self._get_clob_client()

        try:
            resp = await asyncio.to_thread(
                clob.place_limit_order,
                token_id=token_id,
                price=price,
                size=shares,
                side=clob_side,
            )
        except Exception as exc:
            log.error("CLOB place_order failed: %s", exc)
            if self._kill_switch is not None:
                self._kill_switch.record_api_response(500)
            raise

        if self._kill_switch is not None:
            self._kill_switch.record_internet_ok()
            self._kill_switch.record_api_response(200)

        if not isinstance(resp, AcceptedOrder):
            log.error("Order rejected: code=%s  msg=%s", resp.code, resp.message)
            raise RuntimeError(f"Order rejected by CLOB: {resp.code} — {resp.message}")

        order_id   = resp.order_id
        fill_price: float | None = None
        if resp.status == "matched":
            fill_price = price

        self._live_order_meta[order_id] = (window_id, side, size_usd)
        # Record fill for P&L tracking; use limit price as expected fill
        self._live_fills[window_id] = {
            "side": side,
            "fill_price": fill_price if fill_price is not None else price,
            "size_usd": size_usd,
            "order_id": order_id,
            "token_id": token_id,
            "status": resp.status,
        }
        log.info(
            "LiveOrder %s | %s %s @ %.4f size=%.2f status=%s token=%s...",
            order_id, side, strategy, price, size_usd, resp.status, token_id[:12],
        )
        return order_id, fill_price

    async def _live_amend(self, order_id: str, new_price: float) -> bool:
        meta = self._live_order_meta.get(order_id)
        if meta is None:
            log.warning("LiveAmend %s — no metadata found, cannot replace", order_id)
            return False
        window_id, side, size_usd = meta

        if not await self._live_cancel(order_id):
            return False

        try:
            new_id, _ = await self._live_place(window_id, side, "maker", new_price, size_usd)
        except Exception as exc:
            log.error("LiveAmend %s — replacement failed: %s", order_id, exc)
            return False

        self._live_redirects[order_id] = new_id
        log.info("LiveAmend %s → %s @ %.4f", order_id, new_id, new_price)
        return True

    async def _live_cancel(self, order_id: str) -> bool:
        clob = self._get_clob_client()
        try:
            resp = await asyncio.to_thread(clob.cancel_order, order_id=order_id)
        except Exception as exc:
            log.error("LiveCancel %s failed: %s", order_id, exc)
            if self._kill_switch is not None:
                self._kill_switch.record_api_response(500)
            return False

        if order_id not in resp.canceled:
            reason = resp.not_canceled.get(order_id, "unknown")
            log.warning("LiveCancel %s not confirmed: %s", order_id, reason)

        if self._kill_switch is not None:
            self._kill_switch.record_internet_ok()
        self._live_order_meta.pop(order_id, None)
        log.info("LiveCancel %s OK", order_id)
        return True
