"""Real-time LP monitoring: accountSubscribe on the pool's SOL vault.

The exit engine's poll loop already checks reserves every poll_interval_s;
this monitor is the fast path — when the quote vault balance collapses, it
pokes the engine immediately instead of waiting out the poll interval.
Detection latency is the difference between selling into the last of the
liquidity and holding a worthless bag.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from ..detection.ws import ResilientWebSocket
from ..models import Position

log = logging.getLogger(__name__)


class LpMonitor:
    def __init__(self, ws_url: str, drop_pct: float,
                 on_lp_pull: Callable[[int], None]):
        self.ws = ResilientWebSocket(ws_url, stall_timeout_s=60.0)
        self.drop_pct = drop_pct
        self.on_lp_pull = on_lp_pull
        self._baselines: dict[str, int] = {}      # vault -> baseline lamports
        self._vault_to_position: dict[str, int] = {}

    def watch(self, position: Position, quote_vault: Optional[str],
              baseline_lamports: int) -> None:
        if not quote_vault or position.id is None:
            return
        self._baselines[quote_vault] = baseline_lamports
        self._vault_to_position[quote_vault] = position.id

        async def handler(result: dict[str, Any], vault: str = quote_vault) -> None:
            value = result.get("value") or {}
            lamports = int(value.get("lamports") or 0)
            baseline = self._baselines.get(vault, 0)
            if baseline > 0 and lamports < baseline * (1 - self.drop_pct / 100.0):
                pos_id = self._vault_to_position.get(vault)
                log.warning("LP PULL detected on vault %s: %d -> %d lamports",
                            vault[:8], baseline, lamports)
                if pos_id is not None:
                    self.on_lp_pull(pos_id)

        self.ws.subscribe(
            "accountSubscribe",
            [quote_vault, {"encoding": "base64", "commitment": "processed"}],
            handler, name=f"lp:{quote_vault[:8]}")

    async def run(self) -> None:
        await self.ws.run()

    def stop(self) -> None:
        self.ws.stop()
