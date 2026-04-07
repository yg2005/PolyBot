"""Kill switch for live trading.

Usage (manual, from command line):
    python -m kalbot.kill_switch

This cancels all open CLOB orders and writes a kill_switch.flag file.
The flag prevents any new orders until manually cleared:
    rm data/kill_switch.flag

Auto-triggers (when embedded in the bot):
- Internet drop > 60 s
- 5 consecutive API 5xx errors
- Uncaught exception passed via engage()
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .execution.order_manager import OrderManager
    from .monitoring.alerts import AlertManager

log = logging.getLogger(__name__)

FLAG_FILE = Path("data/kill_switch.flag")
CONSECUTIVE_5XX_LIMIT: int = 5
INTERNET_DROP_SECONDS: float = 60.0
_INTERNET_CHECK_HOST = "8.8.8.8"
_INTERNET_CHECK_PORT = 53
_INTERNET_CHECK_TIMEOUT = 5.0


class KillSwitch:
    """Shared kill-switch state. Injected into OrderManager and main.

    Thread-safe engagement via asyncio (single-threaded event loop).
    """

    def __init__(self) -> None:
        # Load persisted state on startup
        if FLAG_FILE.exists():
            self._engaged = True
            try:
                self._reason = FLAG_FILE.read_text().strip()
            except Exception:
                self._reason = "unknown (flag file present)"
            log.critical("KillSwitch: flag file found — trading DISABLED. Reason: %s", self._reason)
        else:
            self._engaged = False
            self._reason = ""

        self._consecutive_5xx: int = 0
        self._last_internet_ok: float = time.monotonic()
        self._order_mgr: OrderManager | None = None
        self._alerts: AlertManager | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._running: bool = False

    # ------------------------------------------------------------------ #
    # Wiring                                                               #
    # ------------------------------------------------------------------ #

    def set_order_manager(self, om: OrderManager) -> None:
        self._order_mgr = om

    def set_alerts(self, a: AlertManager) -> None:
        self._alerts = a

    # ------------------------------------------------------------------ #
    # State                                                                #
    # ------------------------------------------------------------------ #

    def is_engaged(self) -> bool:
        return self._engaged

    @property
    def reason(self) -> str:
        return self._reason

    # ------------------------------------------------------------------ #
    # Engage                                                               #
    # ------------------------------------------------------------------ #

    async def engage(self, reason: str) -> None:
        """Engage kill switch: cancel all open orders, disable trading, alert.

        Note on "close all": Polymarket binary positions cannot be force-closed
        by the bot — they settle at expiry. The kill switch cancels all pending
        (unfilled) CLOB orders. Any already-filled positions will settle normally
        at window close. This is the correct behaviour for prediction markets.
        """
        if self._engaged:
            return  # Already engaged — idempotent
        self._engaged = True
        self._reason = reason
        log.critical("KILL SWITCH ENGAGED: %s", reason)

        # Persist flag so bot cannot restart without manual clear
        try:
            FLAG_FILE.parent.mkdir(parents=True, exist_ok=True)
            FLAG_FILE.write_text(reason)
        except Exception as exc:
            log.error("KillSwitch: failed to write flag file: %s", exc)

        # Cancel all open live orders
        if self._order_mgr is not None:
            try:
                cancelled = await self._order_mgr.cancel_all_live()
                log.critical(
                    "KillSwitch: cancelled %d open orders. "
                    "Filled positions will settle at window expiry (cannot be force-closed on Polymarket).",
                    cancelled,
                )
            except Exception as exc:
                log.error("KillSwitch: cancel_all_live failed: %s", exc)
        else:
            log.critical("KillSwitch: no OrderManager set — open orders NOT cancelled")

        # Alert
        if self._alerts is not None:
            try:
                await self._alerts.circuit_breaker(f"KILL SWITCH: {reason}")
            except Exception as exc:
                log.error("KillSwitch: alert failed: %s", exc)

    def reset(self) -> None:
        """Clear the kill switch (manual operator action only)."""
        self._engaged = False
        self._reason = ""
        self._consecutive_5xx = 0
        if FLAG_FILE.exists():
            FLAG_FILE.unlink()
        log.warning("KillSwitch RESET — trading re-enabled")

    # ------------------------------------------------------------------ #
    # Auto-triggers                                                        #
    # ------------------------------------------------------------------ #

    def record_api_response(self, status_code: int) -> None:
        """Call after every live CLOB API call with its HTTP status code."""
        if status_code >= 500:
            self._consecutive_5xx += 1
            log.warning(
                "KillSwitch: API %d — consecutive_5xx=%d/%d",
                status_code, self._consecutive_5xx, CONSECUTIVE_5XX_LIMIT,
            )
            if self._consecutive_5xx >= CONSECUTIVE_5XX_LIMIT:
                asyncio.get_event_loop().call_soon_threadsafe(
                    lambda: asyncio.ensure_future(
                        self.engage(
                            f"{self._consecutive_5xx} consecutive API {status_code} errors"
                        )
                    )
                )
        else:
            self._consecutive_5xx = 0

    def record_internet_ok(self) -> None:
        """Call whenever a successful outbound network request completes."""
        self._last_internet_ok = time.monotonic()

    # ------------------------------------------------------------------ #
    # Internet monitor                                                     #
    # ------------------------------------------------------------------ #

    def start_monitor(self) -> None:
        """Start the background internet-drop monitor (call from async context)."""
        self._running = True
        self._monitor_task = asyncio.create_task(
            self._internet_monitor(), name="KillSwitchMonitor"
        )

    async def stop_monitor(self) -> None:
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

    async def _internet_monitor(self) -> None:
        while self._running:
            await asyncio.sleep(10)
            if self._engaged:
                continue
            ok = await asyncio.get_event_loop().run_in_executor(
                None, _check_internet
            )
            if ok:
                self.record_internet_ok()
            else:
                elapsed = time.monotonic() - self._last_internet_ok
                log.warning("KillSwitch: internet check failed (down %.0fs)", elapsed)
                if elapsed > INTERNET_DROP_SECONDS:
                    await self.engage(
                        f"internet drop detected: no connectivity for {elapsed:.0f}s"
                    )


def _check_internet() -> bool:
    try:
        socket.setdefaulttimeout(_INTERNET_CHECK_TIMEOUT)
        with socket.create_connection((_INTERNET_CHECK_HOST, _INTERNET_CHECK_PORT)):
            return True
    except OSError:
        return False


# ------------------------------------------------------------------ #
# CLI entry point                                                     #
# ------------------------------------------------------------------ #

async def _cli_main() -> None:
    """Cancel all open CLOB orders and engage the kill switch."""
    import os
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from .config import load_config

    cfg = load_config()
    if cfg.execution.mode != "live":
        log.warning("Not in live mode — nothing to cancel. Flag will still be written.")

    ks = KillSwitch()

    # Cancel via CLOB directly (no OrderManager needed for CLI)
    api_key = cfg.polymarket_api_key
    clob_url = cfg.feeds.clob_api_url
    if api_key and cfg.execution.mode == "live":
        cancelled = await _cancel_all_clob_orders(clob_url, api_key)
        log.info("Cancelled %d live orders", cancelled)
    else:
        log.info("No API key or not in live mode — skipping CLOB cancellation")

    await ks.engage("manual kill switch from CLI")
    log.info("Kill switch engaged. Remove data/kill_switch.flag to re-enable.")
    sys.exit(0)


async def _cancel_all_clob_orders(clob_url: str, api_key: str) -> int:
    """Best-effort cancel all open orders on CLOB. Returns count cancelled."""
    import httpx

    headers = {"Authorization": f"Bearer {api_key}"}
    cancelled = 0
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Fetch open orders
            resp = await client.get(f"{clob_url}/orders", headers=headers,
                                    params={"status": "OPEN"})
            resp.raise_for_status()
            orders: list[dict[str, Any]] = resp.json()
            log.info("Found %d open orders", len(orders))
            for order in orders:
                oid = order.get("id") or order.get("orderID", "")
                if not oid:
                    continue
                try:
                    r = await client.delete(f"{clob_url}/order/{oid}", headers=headers)
                    r.raise_for_status()
                    log.info("Cancelled order %s", oid)
                    cancelled += 1
                except Exception as exc:
                    log.error("Failed to cancel %s: %s", oid, exc)
    except Exception as exc:
        log.error("_cancel_all_clob_orders failed: %s", exc)
    return cancelled


def main() -> None:
    asyncio.run(_cli_main())


if __name__ == "__main__":
    main()
