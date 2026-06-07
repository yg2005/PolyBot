from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ..config import KalbotConfig
from ..data.db import Database
from .paper import PaperExecutor

if TYPE_CHECKING:
    from ..kill_switch import KillSwitch
    from ..execution.ramp import SizeRamp
    from ..risk.risk_manager import RiskManager

log = logging.getLogger(__name__)

TAKER_FEE = 0.005


class OrderManager:
    """Routes orders to paper executor (default) or live Polymarket CLOB.

    Paper mode: all state lives in PaperExecutor (in-memory + SQLite).
    Live mode: uses py-clob-client for order placement and cancellation.
    """

    def __init__(self, cfg: KalbotConfig, db: Database) -> None:
        self._mode          = cfg.execution.mode
        self._paper         = PaperExecutor(cfg.execution, db)
        self._private_key   = cfg.polymarket_private_key
        self._proxy_wallet  = cfg.polymarket_proxy_wallet
        self._nonce         = 0
        self._live_redirects:  dict[str, str]                 = {}
        self._live_order_meta: dict[str, tuple[str, str, float]] = {}
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
    ) -> tuple[str, float | None]:
        if self._mode == "live":
            return await self._live_place(window_id, side, strategy, price, size_usd)
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
            return None
        return self._paper.get_fill(window_id)

    async def settle_positions(self, window_id: str, outcome: str) -> float | None:
        if self._mode == "live":
            return None
        return await self._paper.settle_positions(window_id, outcome)

    # ------------------------------------------------------------------ #
    # Live CLOB integration                                               #
    # ------------------------------------------------------------------ #

    def _get_clob_client(self):
        """Return a cached ClobClient with Level 2 auth (lazy init)."""
        if self._clob_client is not None:
            return self._clob_client

        from py_clob_client.client import ClobClient
        from py_order_utils.model import POLY_PROXY

        if not self._private_key or not self._proxy_wallet:
            raise RuntimeError(
                "Live mode requires POLYMARKET_PRIVATE_KEY and POLYMARKET_PROXY_WALLET"
            )

        client = ClobClient(
            host="https://clob.polymarket.com",
            key=self._private_key,
            chain_id=137,
            signature_type=POLY_PROXY,
            funder=self._proxy_wallet,
        )
        creds = client.derive_api_key()
        if creds is None:
            raise RuntimeError("Failed to derive Polymarket API credentials")
        client.set_api_creds(creds)
        log.info("ClobClient ready: address=%s  api_key=%s...", client.get_address(), creds.api_key[:8])
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
    ) -> tuple[str, float | None]:
        if self._kill_switch is not None and self._kill_switch.is_engaged():
            raise RuntimeError(f"Kill switch engaged: {self._kill_switch.reason}")

        if self._ramp is not None and self._risk is not None:
            size_usd = self._ramp.apply(
                size_usd, self._risk.daily_pnl, self._max_daily_loss_usd
            )

        from py_clob_client.clob_types import OrderArgs

        # window_id is the CTF token_id in live mode
        # size in OrderArgs is ConditionalTokens (shares), not USDC
        shares = size_usd / price
        order_args = OrderArgs(
            token_id=window_id,
            price=price,
            size=shares,
            side=side,
        )
        self._nonce += 1
        order_args.nonce = self._nonce

        clob = self._get_clob_client()

        try:
            data = await asyncio.to_thread(clob.create_and_post_order, order_args)
        except Exception as exc:
            log.error("CLOB place_order failed: %s", exc)
            if self._kill_switch is not None:
                self._kill_switch.record_api_response(500)
            raise

        if self._kill_switch is not None:
            self._kill_switch.record_internet_ok()
            self._kill_switch.record_api_response(200)

        order_id = (data or {}).get("orderID", "") or (data or {}).get("id", "")
        status   = (data or {}).get("status", "unknown")
        fill_price: float | None = None
        if status in ("matched", "filled"):
            fill_price = float((data or {}).get("price", price))

        if order_id:
            self._live_order_meta[order_id] = (window_id, side, size_usd)
        log.info(
            "LiveOrder %s | %s %s @ %.4f size=%.2f status=%s",
            order_id, side, strategy, price, size_usd, status,
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
            await asyncio.to_thread(clob.cancel, order_id)
        except Exception as exc:
            log.error("LiveCancel %s failed: %s", order_id, exc)
            if self._kill_switch is not None:
                self._kill_switch.record_api_response(500)
            return False

        if self._kill_switch is not None:
            self._kill_switch.record_internet_ok()
        self._live_order_meta.pop(order_id, None)
        log.info("LiveCancel %s OK", order_id)
        return True
