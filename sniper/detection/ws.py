"""Resilient Solana WebSocket subscription client.

Shared-RPC websocket subscriptions drop constantly, and worse, they stall
silently: the TCP connection stays up but notifications stop. Both failure
modes are handled:

- hard disconnects  -> reconnect with exponential backoff + jitter
- silent stalls     -> watchdog tracks last-message time; past the stall
                       timeout the connection is torn down and rebuilt
- all subscriptions are re-established on every reconnect

The documented upgrade path for lower latency is gRPC/Geyser (e.g. Yellowstone
gRPC) — nothing here depends on it, but the detector interface (an asyncio
queue of LaunchEvents) was chosen so a Geyser-backed detector can drop in.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import aiohttp

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class Subscription:
    method: str                  # e.g. "logsSubscribe"
    params: list[Any]
    handler: Handler
    name: str = ""
    # populated per-connection
    request_id: Optional[int] = None
    sub_id: Optional[int] = None
    stats_received: int = field(default=0)


class ResilientWebSocket:
    def __init__(self, url: str, stall_timeout_s: float = 30.0,
                 max_backoff_s: float = 60.0, ping_interval_s: float = 10.0):
        self.url = url
        self.stall_timeout_s = stall_timeout_s
        self.max_backoff_s = max_backoff_s
        self.ping_interval_s = ping_interval_s
        self.subscriptions: list[Subscription] = []
        self._ids = itertools.count(1)
        self._last_msg = time.monotonic()
        self._running = False
        self.reconnect_count = 0
        self.on_reconnect: Optional[Callable[[int], Awaitable[None]]] = None

    def subscribe(self, method: str, params: list[Any], handler: Handler,
                  name: str = "") -> None:
        self.subscriptions.append(
            Subscription(method=method, params=params, handler=handler, name=name))

    async def run(self) -> None:
        """Run forever (until cancelled), reconnecting as needed."""
        self._running = True
        backoff = 1.0
        while self._running:
            try:
                connected_at = time.monotonic()
                await self._run_once()
                # normal closure — treat like a drop
                if time.monotonic() - connected_at > 60:
                    backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                log.warning("ws connection error (%s); reconnecting in %.1fs",
                            exc, backoff)
            if not self._running:
                break
            self.reconnect_count += 1
            if self.on_reconnect:
                try:
                    await self.on_reconnect(self.reconnect_count)
                except Exception:  # noqa: BLE001
                    log.exception("on_reconnect callback failed")
            await asyncio.sleep(backoff * (0.5 + random.random()))
            backoff = min(backoff * 2, self.max_backoff_s)

    def stop(self) -> None:
        self._running = False

    async def _run_once(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                    self.url, heartbeat=self.ping_interval_s, max_msg_size=0) as ws:
                log.info("ws connected to %s (%d subscriptions)",
                         self.url.split("?")[0], len(self.subscriptions))
                self._last_msg = time.monotonic()
                await self._send_subscribes(ws)
                watchdog = asyncio.create_task(self._watchdog(ws))
                try:
                    async for msg in ws:
                        self._last_msg = time.monotonic()
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._dispatch(json.loads(msg.data))
                        elif msg.type in (aiohttp.WSMsgType.ERROR,
                                          aiohttp.WSMsgType.CLOSED):
                            break
                finally:
                    watchdog.cancel()

    async def _send_subscribes(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        for sub in self.subscriptions:
            sub.request_id = next(self._ids)
            sub.sub_id = None
            await ws.send_json({"jsonrpc": "2.0", "id": sub.request_id,
                                "method": sub.method, "params": sub.params})

    async def _watchdog(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Detect silent stalls: connection is open but nothing arrives."""
        while True:
            await asyncio.sleep(self.stall_timeout_s / 3)
            idle = time.monotonic() - self._last_msg
            if idle > self.stall_timeout_s:
                log.warning("ws stalled (%.0fs without any message); forcing reconnect",
                            idle)
                await ws.close()
                return

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        # subscription confirmations: {"id": N, "result": <sub_id>}
        if "id" in msg and "result" in msg:
            for sub in self.subscriptions:
                if sub.request_id == msg["id"]:
                    sub.sub_id = msg["result"]
                    log.info("subscribed %s (sub_id=%s)", sub.name or sub.method,
                             sub.sub_id)
                    return
            return
        # notifications: {"method": "...Notification", "params": {"subscription": id, ...}}
        params = msg.get("params")
        if not isinstance(params, dict):
            return
        sub_id = params.get("subscription")
        for sub in self.subscriptions:
            if sub.sub_id == sub_id:
                sub.stats_received += 1
                try:
                    await sub.handler(params.get("result") or {})
                except Exception:  # noqa: BLE001 - one bad event must not kill the stream
                    log.exception("handler %s failed on notification",
                                  sub.name or sub.method)
                return
