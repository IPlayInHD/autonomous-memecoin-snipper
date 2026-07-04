"""Mint-level checks: authority revocation and Token-2022 extension traps."""

from __future__ import annotations

from ..models import FilterResult, LaunchEvent
from .base import Filter, FilterContext


class AuthoritiesFilter(Filter):
    """Mint authority and freeze authority must both be revoked.

    A live mint authority can print supply into your position; a live freeze
    authority can freeze your token account so you can never sell.
    Exception: pump.fun bonding-curve tokens keep authorities with the curve
    program until graduation — for PUMPFUN_LAUNCH events the curve itself is
    the accepted authority holder.
    """

    name = "authorities"
    hard = True
    fast = True

    async def check(self, event: LaunchEvent, ctx: FilterContext) -> FilterResult:
        mint = await ctx.mint_info(event)
        if mint is None:
            return self._fail("mint account not found")
        allowed = {event.pool} if event.source.value == "pumpfun_launch" else set()
        problems = []
        if mint.mint_authority is not None and mint.mint_authority not in allowed:
            problems.append(f"mint authority live: {mint.mint_authority}")
        if mint.freeze_authority is not None and mint.freeze_authority not in allowed:
            problems.append(f"freeze authority live: {mint.freeze_authority}")
        if problems:
            return self._fail("; ".join(problems),
                              mint_authority=mint.mint_authority,
                              freeze_authority=mint.freeze_authority)
        return self._pass(reason="authorities revoked")


class Token2022Filter(Filter):
    """Token-2022 extension inspection — the modern honeypot vector.

    A mint can look clean on classic checks (authorities revoked) while a
    Token-2022 extension still traps buyers:
      - transfer hook: arbitrary program invoked on every transfer; can block sells
      - permanent delegate: a wallet that can move/burn YOUR tokens forever
      - default frozen accounts: your ATA starts frozen
      - transfer fee: silent tax on every transfer
      - non-transferable / unknown extensions: obviously unsellable / unaudited
    """

    name = "token2022"
    hard = True
    fast = True

    async def check(self, event: LaunchEvent, ctx: FilterContext) -> FilterResult:
        mint = await ctx.mint_info(event)
        if mint is None:
            return self._fail("mint account not found")
        ext = mint.extensions
        if not ext.is_token_2022:
            return self._pass(reason="classic SPL token")
        problems = []
        if ext.has_transfer_hook:
            problems.append(f"transfer hook -> {ext.transfer_hook_program}")
        if ext.has_permanent_delegate:
            problems.append(f"permanent delegate -> {ext.permanent_delegate}")
        if ext.default_state_frozen:
            problems.append("default account state = frozen")
        if ext.non_transferable:
            problems.append("non-transferable")
        if ext.transfer_fee_bps > ctx.cfg.filters.max_transfer_fee_bps:
            problems.append(f"transfer fee {ext.transfer_fee_bps} bps "
                            f"(max {ctx.cfg.filters.max_transfer_fee_bps})")
        if ext.unknown_extensions:
            problems.append(f"unknown extensions {ext.unknown_extensions}")
        if problems:
            return self._fail("; ".join(problems), extensions=vars(ext))
        # clean Token-2022 exists but is rare in memecoins; pass with a note
        return self._pass(score=0.9, reason="token-2022 with clean extensions",
                          extensions=vars(ext))
