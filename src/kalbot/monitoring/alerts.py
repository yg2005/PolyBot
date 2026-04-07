from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

_TIMEOUT = 10.0

# Event types and their emoji prefixes
_ICONS: dict[str, str] = {
    "trade": "📈",
    "settlement": "🏁",
    "error": "🚨",
    "circuit_breaker": "🛑",
    "edge_decay": "📉",
    "model_retrain": "🤖",
}


class AlertManager:
    """Sends alerts to Discord (webhook) and/or Telegram (bot token + chat_id).

    Both channels are optional — only fires if credentials are set.
    All sends are fire-and-forget (errors are logged, not raised).
    """

    def __init__(
        self,
        discord_webhook_url: str = "",
        telegram_bot_token: str = "",
        telegram_chat_id: str = "",
    ) -> None:
        self._discord_url = discord_webhook_url
        self._tg_token = telegram_bot_token
        self._tg_chat = telegram_chat_id

    async def send(self, event_type: str, payload: dict[str, Any]) -> None:
        """Send alert for event_type. payload must contain at least 'message'."""
        icon = _ICONS.get(event_type, "ℹ️")
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        message = payload.get("message", str(payload))
        text = f"{icon} [{ts}] **{event_type.upper()}** — {message}"

        await self._send_discord(text)
        await self._send_telegram(text)

    # ------------------------------------------------------------------ #
    # Convenience helpers                                                  #
    # ------------------------------------------------------------------ #

    async def trade(
        self, side: str, entry: float, size_usd: float, market: str
    ) -> None:
        await self.send(
            "trade",
            {
                "message": (
                    f"{side} @ {entry:.4f} | size=${size_usd:.2f} | {market}"
                )
            },
        )

    async def settlement(
        self, outcome: str, pnl: float, market: str
    ) -> None:
        await self.send(
            "settlement",
            {
                "message": (
                    f"Outcome={outcome} | P&L={pnl:+.4f} | {market}"
                )
            },
        )

    async def error(self, msg: str) -> None:
        await self.send("error", {"message": msg})

    async def circuit_breaker(self, reason: str) -> None:
        await self.send("circuit_breaker", {"message": reason})

    async def edge_decay(self, message: str) -> None:
        await self.send("edge_decay", {"message": message})

    async def model_retrain(self, model_id: str, auc: float) -> None:
        await self.send(
            "model_retrain",
            {"message": f"New model {model_id} | AUC={auc:.4f}"},
        )

    # ------------------------------------------------------------------ #
    # Transport                                                            #
    # ------------------------------------------------------------------ #

    async def _send_discord(self, text: str) -> None:
        if not self._discord_url:
            return
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(
                    self._discord_url,
                    json={"content": text},
                )
                if resp.status_code not in (200, 204):
                    log.warning("Discord alert failed: %s %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            log.error("Discord send error: %s", exc)

    async def _send_telegram(self, text: str) -> None:
        if not self._tg_token or not self._tg_chat:
            return
        # Strip markdown bold markers for Telegram HTML mode
        html_text = text.replace("**", "<b>", 1).replace("**", "</b>", 1)
        url = f"https://api.telegram.org/bot{self._tg_token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(
                    url,
                    json={
                        "chat_id": self._tg_chat,
                        "text": html_text,
                        "parse_mode": "HTML",
                    },
                )
                if resp.status_code != 200:
                    log.warning("Telegram alert failed: %s %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            log.error("Telegram send error: %s", exc)
