"""Liquidity-level checks: minimum depth and LP burned-vs-locked status."""

from __future__ import annotations

from .. import constants as C
from ..models import FilterResult, LaunchEvent
from .base import Filter, FilterContext


class LiquidityFilter(Filter):
    """Minimum initial SOL-side liquidity. Thin pools mean catastrophic
    slippage both ways and are the easiest rug to pull."""

    name = "liquidity"
    hard = True
    fast = True

    async def check(self, event: LaunchEvent, ctx: FilterContext) -> FilterResult:
        pool = await ctx.pool_state(event)
        if pool is None:
            return self._fail("pool state unavailable")
        sol = pool.sol_reserve / C.LAMPORTS_PER_SOL
        min_sol = ctx.cfg.filters.min_initial_liquidity_sol
        if sol < min_sol:
            return self._fail(f"liquidity {sol:.2f} SOL < min {min_sol}",
                              sol_reserve=pool.sol_reserve)
        # score saturates at 4x the minimum
        score = min(1.0, sol / (4.0 * min_sol))
        return self._pass(score=score, reason=f"{sol:.2f} SOL liquidity",
                          sol_reserve=pool.sol_reserve)


class LpStatusFilter(Filter):
    """LP tokens burned vs locked — kept as DISTINCT checks, not one flag.

    burned: LP supply verifiably destroyed (sent to the incinerator or supply
            ~0 with mint authority gone). Nobody can pull the pool.
    locked: LP supply held by a SPECIFIC known locker program. Trust shifts to
            the locker (and its unlock schedule) — strictly weaker than burned.
    Any other large LP holder = rug-capable, scores 0.

    Bonding-curve launches have no LP token yet; the check is skipped and the
    scoring layer treats it as neutral.
    """

    name = "lp_status"
    hard = False   # soft: heavily weighted in the score instead of a hard gate
    fast = False

    async def check(self, event: LaunchEvent, ctx: FilterContext) -> FilterResult:
        pool = await ctx.pool_state(event)
        if pool is not None and pool.is_bonding_curve:
            return self._skip("bonding curve: no LP token pre-migration")
        if not event.lp_mint:
            return self._fail("no LP mint known for pool", burned=False, locked=False)

        lp_mint_info = await ctx.provider.get_mint_info(event.lp_mint)
        holders = await ctx.provider.get_lp_holders(event.lp_mint)
        supply = lp_mint_info.supply if lp_mint_info else None
        if supply in (None, 0):
            burned = lp_mint_info is not None and lp_mint_info.mint_authority is None
            if burned:
                return self._pass(score=1.0, reason="LP supply zero, mint authority gone",
                                  burned=True, locked=False)
            return self._fail("LP supply unreadable", burned=False, locked=False)

        lockers = dict(C.KNOWN_LOCKER_PROGRAMS)
        lockers.update(ctx.cfg.filters.locker_programs)

        burned_amount = locked_amount = free_amount = 0
        locker_label = None
        for h in holders:
            if h.owner == C.INCINERATOR or h.address == C.INCINERATOR:
                burned_amount += h.amount
            elif h.owner in lockers:
                locked_amount += h.amount
                locker_label = lockers[h.owner]
            else:
                owner_program = None
                if h.owner:
                    owner_program = await ctx.provider.get_account_owner_program(h.owner)
                if owner_program in lockers:
                    locked_amount += h.amount
                    locker_label = lockers[owner_program]
                else:
                    free_amount += h.amount

        burned_pct = 100.0 * burned_amount / supply
        locked_pct = 100.0 * locked_amount / supply
        free_pct = 100.0 * free_amount / supply
        data = {"burned_pct": round(burned_pct, 2), "locked_pct": round(locked_pct, 2),
                "free_pct": round(free_pct, 2), "locker": locker_label,
                "burned": burned_pct >= 95.0, "locked": locked_pct >= 95.0}

        if burned_pct >= 95.0:
            return self._pass(score=1.0, reason=f"LP burned ({burned_pct:.0f}%)", **data)
        if burned_pct + locked_pct >= 95.0:
            return self._pass(score=0.8,
                              reason=f"LP locked via {locker_label} ({locked_pct:.0f}%)",
                              **data)
        return self._fail(f"LP {free_pct:.0f}% free (rug-capable)", score=0.0, **data)
