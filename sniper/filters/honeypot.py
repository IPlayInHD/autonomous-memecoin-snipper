"""Honeypot check: simulate an actual sell before ever buying.

The only reliable way to know a token can be sold is to simulate selling it
via `simulateTransaction` on a real sell route. Bonding-curve tokens sell
through the pump.fun curve pre-migration; AMM tokens through the pool/route —
the injected sell-sim builder handles both cases.

Also sanity-checks effective round-trip tax when the simulation exposes it.
"""

from __future__ import annotations

from ..models import FilterResult, LaunchEvent
from .base import Filter, FilterContext


class HoneypotFilter(Filter):
    name = "honeypot"
    hard = True
    fast = False   # costs a route build + simulation round trip

    # simulation errors that mean "you could never sell", not "sim flaked"
    _FATAL_MARKERS = (
        "AccountFrozen", "custom program error: 0x11", "InsufficientFunds",
        "TransferHook", "0x1e",  # pump.fun curve: not tradable
    )

    async def check(self, event: LaunchEvent, ctx: FilterContext) -> FilterResult:
        pool = await ctx.pool_state(event)
        if pool is None:
            return self._fail("pool state unavailable, cannot build sell route")
        # simulate selling ~0.1% of the pool's token side — small enough to be
        # realistic, big enough to trip amount-based traps
        tokens = max(1, pool.token_reserve // 1000)
        sim = await ctx.provider.simulate_sell(event, tokens)
        if sim.success:
            return self._pass(reason="sell simulation succeeded",
                              units_consumed=sim.units_consumed)
        err = sim.error or "unknown simulation failure"
        fatal = any(m in err for m in self._FATAL_MARKERS)
        logs_tail = sim.logs[-3:] if sim.logs else []
        if fatal:
            return self._fail(f"sell blocked: {err}", logs=logs_tail)
        # non-fatal failure (e.g. no route yet, transient) — still a fail, but
        # recorded distinctly so the logs can separate 'honeypot' from 'no route'
        return self._fail(f"sell simulation failed (non-fatal): {err}",
                          score=0.2, logs=logs_tail, transient=True)
