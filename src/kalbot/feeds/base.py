from __future__ import annotations

import asyncio
import logging
import socket
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

log = logging.getLogger(__name__)


class FeedStatus(str, Enum):
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    STALE = "STALE"
    DISCONNECTED = "DISCONNECTED"


@dataclass
class PriceUpdate:
    price: float
    timestamp: datetime
    source: str


class BaseFeed(ABC):
    _BACKOFF_BASE: float = 1.0
    _BACKOFF_MAX: float = 30.0

    def __init__(self, stale_threshold_s: int = 10) -> None:
        self._stale_threshold_s = stale_threshold_s
        self._last_update_time: datetime | None = None
        self._status = FeedStatus.DISCONNECTED
        self._running = False
        self._task: asyncio.Task | None = None
        self._network_error: bool = False

    @property
    def status(self) -> FeedStatus:
        return self._status

    @property
    def last_update_time(self) -> datetime | None:
        return self._last_update_time

    @property
    def network_error(self) -> bool:
        return self._network_error

    @property
    def is_stale(self) -> bool:
        if self._last_update_time is None:
            return True
        age = (datetime.now(timezone.utc) - self._last_update_time).total_seconds()
        return age > self._stale_threshold_s

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def on_update(self, update: PriceUpdate) -> None: ...

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_with_reconnect(), name=f"{self.__class__.__name__}")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.disconnect()
        self._status = FeedStatus.DISCONNECTED

    async def _run_with_reconnect(self) -> None:
        backoff = self._BACKOFF_BASE
        while self._running:
            try:
                self._status = FeedStatus.CONNECTING
                await self.connect()
                self._network_error = False  # clear on clean reconnect
                backoff = self._BACKOFF_BASE
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._status = FeedStatus.DISCONNECTED
                if isinstance(exc, socket.gaierror):
                    self._network_error = True
                    log.error("%s DNS failure: %s — retry in %.0fs", self.__class__.__name__, exc, backoff)
                else:
                    log.warning("%s disconnected: %s — retry in %.0fs", self.__class__.__name__, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._BACKOFF_MAX)
