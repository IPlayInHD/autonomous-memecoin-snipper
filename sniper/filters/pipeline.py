"""Filter pipeline: cheap-to-expensive ordering, mode-aware gating, full logging.

- Filters run in declared order (cheapest chain access first).
- A hard failure stops the pipeline — no point paying for expensive lookups on
  a token that already failed a structural check. The failure is logged with
  its specific reason.
- In block_0 trigger mode, slow filters (fast=False) are SKIPPED and recorded
  as skipped: there is no time for indexer lookups inside one slot. This is by
  design — block_0 is the latency control arm.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from ..config import Config
from ..models import FilterResult, LaunchEvent, PipelineOutcome, TriggerMode
from ..scoring import compute_score
from .base import Filter, FilterContext
from .honeypot import HoneypotFilter
from .lp_filters import LiquidityFilter, LpStatusFilter
from .mint_filters import AuthoritiesFilter, Token2022Filter
from .wallet_filters import BundleFilter, DeployerFilter, HolderConcentrationFilter

log = logging.getLogger(__name__)


def build_default_pipeline() -> list[Filter]:
    """Cheap -> expensive. First four ride on two memoized account fetches;
    the rest each cost additional RPC / indexer / simulation round trips."""
    return [
        AuthoritiesFilter(),        # 1 account fetch (memoized mint)
        Token2022Filter(),          # same fetch
        LiquidityFilter(),          # 1-2 account fetches (memoized pool state)
        LpStatusFilter(),           # LP mint + holders
        HolderConcentrationFilter(),# largest accounts + owners
        BundleFilter(),             # launch tx fetch
        DeployerFilter(),           # enhanced-tx API (cached)
        HoneypotFilter(),           # route build + simulateTransaction
    ]


class FilterPipeline:
    def __init__(self, cfg: Config, filters: Optional[list[Filter]] = None,
                 threshold: float = 1.0):
        self.cfg = cfg
        self.filters = filters if filters is not None else build_default_pipeline()
        self.threshold = threshold

    async def run(self, event: LaunchEvent, ctx: FilterContext,
                  trigger_mode: TriggerMode) -> PipelineOutcome:
        results: list[FilterResult] = []
        rejected_by: Optional[str] = None
        start = time.monotonic()

        for flt in self.filters:
            if trigger_mode == TriggerMode.BLOCK_0 and not flt.fast:
                results.append(FilterResult(
                    name=flt.name, passed=True, hard=False, skipped=True,
                    score=0.5, reason="skipped: too slow for block_0"))
                continue
            result = await flt.run(event, ctx)
            results.append(result)
            if not result.passed and result.hard:
                rejected_by = flt.name
                log.info("REJECT %s [%s] %s: %s", event.mint, event.source.value,
                         flt.name, result.reason)
                break  # don't pay for expensive checks on a dead candidate

        score = compute_score(results, self.cfg.score_weights)
        accepted = rejected_by is None and score >= self.threshold
        event.latency.filters_done = time.time()

        outcome = PipelineOutcome(launch=event, results=results, score=score,
                                  accepted=accepted, rejected_by=rejected_by,
                                  threshold=self.threshold)
        log.info("pipeline %s: score=%.3f accepted=%s (%.0f ms, %d filters)",
                 event.mint[:8], score, accepted,
                 (time.monotonic() - start) * 1000, len(results))
        return outcome
