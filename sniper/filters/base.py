"""Filter framework: shared context (memoized chain fetches) + base class."""

from __future__ import annotations

import abc
import logging
import time
from typing import TYPE_CHECKING, Optional

from ..config import Config
from ..models import FilterResult, LaunchEvent, MintInfo, PoolState

if TYPE_CHECKING:
    from ..chain.helius import HeliusClient
    from ..chain.provider import ChainDataProvider

log = logging.getLogger(__name__)


class FilterContext:
    """Per-launch memoization so ten filters don't refetch the same accounts."""

    def __init__(self, cfg: Config, provider: "ChainDataProvider",
                 helius: Optional["HeliusClient"] = None):
        self.cfg = cfg
        self.provider = provider
        self.helius = helius
        self._mint_info: Optional[MintInfo] = None
        self._mint_fetched = False
        self._pool_state: Optional[PoolState] = None
        self._pool_fetched = False

    async def mint_info(self, event: LaunchEvent) -> Optional[MintInfo]:
        if not self._mint_fetched:
            self._mint_info = await self.provider.get_mint_info(event.mint)
            self._mint_fetched = True
        return self._mint_info

    async def pool_state(self, event: LaunchEvent) -> Optional[PoolState]:
        if not self._pool_fetched:
            self._pool_state = await self.provider.get_pool_state(event)
            self._pool_fetched = True
        return self._pool_state


class Filter(abc.ABC):
    """One check. `hard` failures zero the confidence score; soft failures only
    lower it. `fast` filters are the only ones allowed to gate block_0 entries."""

    name: str = "filter"
    hard: bool = True
    fast: bool = True

    @abc.abstractmethod
    async def check(self, event: LaunchEvent, ctx: FilterContext) -> FilterResult: ...

    async def run(self, event: LaunchEvent, ctx: FilterContext) -> FilterResult:
        start = time.monotonic()
        try:
            result = await self.check(event, ctx)
        except Exception as exc:  # noqa: BLE001 - a crashed filter must fail closed
            log.exception("filter %s crashed on %s", self.name, event.mint)
            result = self._fail(f"filter error: {exc}")
        result.elapsed_ms = (time.monotonic() - start) * 1000.0
        return result

    # helpers ---------------------------------------------------------------
    def _pass(self, score: float = 1.0, reason: str = "", **data) -> FilterResult:
        return FilterResult(name=self.name, passed=True, hard=self.hard,
                            score=max(0.0, min(1.0, score)), reason=reason, data=data)

    def _fail(self, reason: str, score: float = 0.0, **data) -> FilterResult:
        return FilterResult(name=self.name, passed=False, hard=self.hard,
                            score=max(0.0, min(1.0, score)), reason=reason, data=data)

    def _skip(self, reason: str) -> FilterResult:
        return FilterResult(name=self.name, passed=True, hard=False, skipped=True,
                            score=0.5, reason=reason)
