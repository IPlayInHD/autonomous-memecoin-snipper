"""Wallet-level checks: holder concentration, bundle detection, deployer history."""

from __future__ import annotations

from .. import constants as C
from ..models import FilterResult, LaunchEvent
from .base import Filter, FilterContext


class HolderConcentrationFilter(Filter):
    """Reject if the top holder or the deployer controls too much supply.
    Pool/curve accounts are excluded — they're supposed to hold the float."""

    name = "holders"
    hard = True
    fast = False

    async def check(self, event: LaunchEvent, ctx: FilterContext) -> FilterResult:
        supply = await ctx.provider.get_token_supply(event.mint)
        if not supply:
            return self._fail("token supply unreadable")
        holders = await ctx.provider.get_largest_holders(event.mint)
        if not holders:
            return self._fail("holder list unavailable")

        pool_owned = {event.pool, event.base_vault, C.RAYDIUM_AMM_AUTHORITY}
        top_pct = deployer_pct = others_pct = 0.0
        for h in holders[: ctx.cfg.filters.holder_top_n]:
            if h.owner in pool_owned or h.address in pool_owned:
                continue
            pct = 100.0 * h.amount / supply
            top_pct = max(top_pct, pct)
            if h.owner and h.owner == event.creator:
                deployer_pct += pct
            else:
                others_pct += pct

        f = ctx.cfg.filters
        data = {"top_holder_pct": round(top_pct, 2),
                "deployer_pct": round(deployer_pct, 2)}
        if top_pct > f.max_top_holder_pct:
            return self._fail(
                f"top holder {top_pct:.1f}% > max {f.max_top_holder_pct}%", **data)
        if deployer_pct > f.max_deployer_pct:
            return self._fail(
                f"deployer holds {deployer_pct:.1f}% > max {f.max_deployer_pct}%", **data)
        # score: linear penalty as the top holder approaches the cap
        score = 1.0 - (top_pct / f.max_top_holder_pct) * 0.5
        return self._pass(score=score, reason=f"top holder {top_pct:.1f}%", **data)


class BundleFilter(Filter):
    """Flag launches where a large share of supply was bought by wallets in the
    launch block itself (bundled snipers / deployer sock puppets). Those wallets
    dump as one and are the counterparty you least want to be behind."""

    name = "bundle"
    hard = True
    fast = False

    async def check(self, event: LaunchEvent, ctx: FilterContext) -> FilterResult:
        supply = await ctx.provider.get_token_supply(event.mint)
        if not supply:
            return self._fail("token supply unreadable")
        buys = await ctx.provider.get_launch_block_buys(event)
        if buys is None:
            return self._fail("launch-block buys unavailable", score=0.3)
        bundled = sum(a for owner, a in buys.items() if owner != event.creator)
        bundled_pct = 100.0 * bundled / supply
        max_pct = ctx.cfg.filters.max_bundle_supply_pct
        data = {"bundled_pct": round(bundled_pct, 2), "bundle_wallets": len(buys)}
        if bundled_pct > max_pct:
            return self._fail(
                f"bundled wallets took {bundled_pct:.1f}% in launch block "
                f"(max {max_pct}%)", **data)
        return self._pass(score=1.0 - bundled_pct / max(max_pct, 1e-9) * 0.5,
                          reason=f"launch-block acquisition {bundled_pct:.1f}%", **data)


class DeployerFilter(Filter):
    """Deployer wallet age, funding source and prior-token history via the
    enhanced-transaction API. Slow + rate-limited, therefore async-cached and
    NEVER allowed to gate block_0 (fast=False)."""

    name = "deployer"
    hard = False   # unknown data must not hard-block; it scores low instead
    fast = False

    async def check(self, event: LaunchEvent, ctx: FilterContext) -> FilterResult:
        if not event.creator:
            return self._fail("creator unknown", score=0.2)
        if ctx.helius is None or not ctx.helius.enabled:
            return self._skip("no enhanced-tx API configured")
        info = await ctx.helius.deployer_info(event.creator)
        if info is None:
            return self._fail("deployer lookup failed", score=0.3)

        f = ctx.cfg.filters
        data = {"age_days": round(info.age_days, 3) if info.age_days is not None else None,
                "tokens_created": info.tokens_created, "prior_rugs": info.prior_rugs,
                "funding_source": info.funding_source,
                "funding_flagged": info.funding_source_flagged}
        if info.prior_rugs > f.max_deployer_prior_rugs:
            return self._fail(f"deployer has {info.prior_rugs} suspected prior rugs",
                              **data)
        if info.tokens_created > f.max_deployer_prior_tokens:
            return self._fail(
                f"deployer created {info.tokens_created} prior tokens "
                f"(max {f.max_deployer_prior_tokens})", **data)
        score = 1.0
        reasons = []
        if info.age_days is not None and info.age_days < f.min_deployer_age_days:
            score -= 0.6
            reasons.append(f"wallet {info.age_days:.1f}d old "
                           f"(min {f.min_deployer_age_days}d)")
        if info.funding_source_flagged:
            score -= 0.3
            reasons.append("funding source flagged (fresh/churning chain)")
        if score < 0.5:
            return self._fail("; ".join(reasons), score=max(0.0, score), **data)
        return self._pass(score=score,
                          reason="; ".join(reasons) or "deployer history clean", **data)
