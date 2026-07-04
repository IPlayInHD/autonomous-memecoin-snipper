"""Paper execution: simulate fills from REAL pool state, pessimistically.

The paper model deliberately charges everything a live trade would pay:
fill-probability discount, price impact against actual reserves, AMM fee,
route fee, adverse-selection drift, base+priority fees, Jito tip, ATA rent.

Unfilled entries still burn base+priority fees (on Solana your failed snipe
usually LANDS and reverts on the slippage guard — you pay for the attempt).
Jito tips are only charged on filled trades (bundles are atomic).

Even so: paper P&L remains an upper bound. It cannot model being the exit
liquidity for a bundler who front-runs your same-slot entry. The shadow phase
exists to measure that gap.
"""

from __future__ import annotations

import logging
import random
from typing import Optional

from ..config import Config
from ..models import FeeBreakdown, FillResult, PoolState, TriggerMode
from . import costs

log = logging.getLogger(__name__)


class PaperExecutor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        seed = cfg.execution.paper.rng_seed
        self.rng = random.Random(seed) if seed is not None else random.Random()

    # -- entries -----------------------------------------------------------------
    def buy(self, pool: PoolState, sol_in: int, trigger_mode: TriggerMode,
            needs_ata: bool = True,
            fill_probability_override: Optional[float] = None) -> FillResult:
        p = self.cfg.execution.paper
        ex = self.cfg.execution
        fill_prob = (fill_probability_override if fill_probability_override is not None
                     else p.fill_probability.get(trigger_mode.value, 0.5))
        mid = costs.mid_price(pool.sol_reserve, pool.token_reserve)

        prio = costs.priority_fee_lamports(ex.priority_fee_microlamports,
                                           ex.compute_units)
        if self.rng.random() > fill_prob:
            # tx landed too late / reverted on the slippage guard: fee burned
            fees = FeeBreakdown(base_fee=p.base_fee_lamports, priority_fee=prio)
            return FillResult(
                filled=False, side="buy", sol_delta=-fees.total, tokens_delta=0,
                mid_price=mid, fees=fees, fill_probability=fill_prob,
                reason=f"paper_no_fill (p={fill_prob:.2f})")

        swap_in, route_fee = costs.take_route_fee(sol_in, p.route_fee_bps)
        tokens_gross = costs.constant_product_buy(
            swap_in, pool.sol_reserve, pool.token_reserve, p.amm_fee_bps)
        tokens_out = costs.apply_bps_penalty(tokens_gross, p.adverse_selection_bps)
        if tokens_out <= 0:
            fees = FeeBreakdown(base_fee=p.base_fee_lamports, priority_fee=prio)
            return FillResult(filled=False, side="buy", sol_delta=-fees.total,
                              tokens_delta=0, mid_price=mid, fees=fees,
                              fill_probability=fill_prob,
                              reason="pool too thin: zero tokens out")

        fees = FeeBreakdown(
            base_fee=p.base_fee_lamports, priority_fee=prio,
            jito_tip=ex.jito.tip_lamports if ex.jito.enabled else 0,
            ata_rent=p.ata_rent_lamports if needs_ata else 0,
            route_fee=route_fee)
        total_out = sol_in + fees.base_fee + fees.priority_fee + fees.jito_tip \
            + fees.ata_rent  # route fee already inside sol_in
        effective = total_out / tokens_out
        return FillResult(
            filled=True, side="buy", sol_delta=-total_out, tokens_delta=tokens_out,
            effective_price=effective, mid_price=mid,
            slippage_bps=costs.slippage_bps(mid, effective, "buy"),
            fees=fees, fill_probability=fill_prob, reason="paper_fill")

    # -- exits --------------------------------------------------------------------
    def sell(self, pool: PoolState, tokens_in: int,
             priority_fee_multiplier: float = 1.0) -> FillResult:
        """Sells always 'fill' if the pool still has liquidity — what actually
        kills exits is the pool being drained, modeled below, and fee spikes,
        modeled by the escalation multiplier the exit engine passes in."""
        p = self.cfg.execution.paper
        ex = self.cfg.execution
        mid = costs.mid_price(pool.sol_reserve, pool.token_reserve)
        prio = int(costs.priority_fee_lamports(
            ex.priority_fee_microlamports, ex.compute_units) * priority_fee_multiplier)
        fees = FeeBreakdown(base_fee=p.base_fee_lamports, priority_fee=prio)

        sol_gross = costs.constant_product_sell(
            tokens_in, pool.sol_reserve, pool.token_reserve, p.amm_fee_bps)
        if sol_gross <= fees.total or pool.sol_reserve <= 0:
            # liquidity gone (rug) or proceeds don't cover the tx fee:
            # the sell effectively cannot happen. Distinct outcome, not a fill.
            return FillResult(
                filled=False, side="sell", sol_delta=0, tokens_delta=0,
                mid_price=mid, fees=FeeBreakdown(), fill_probability=1.0,
                reason="could_not_sell: pool drained or proceeds below fees")

        sol_after_drift = costs.apply_bps_penalty(sol_gross, p.adverse_selection_bps)
        sol_net_swap, route_fee = costs.take_route_fee(sol_after_drift, p.route_fee_bps)
        fees.route_fee = route_fee
        sol_to_wallet = sol_net_swap - fees.base_fee - fees.priority_fee
        effective = sol_after_drift / tokens_in
        return FillResult(
            filled=True, side="sell", sol_delta=sol_to_wallet,
            tokens_delta=-tokens_in, effective_price=effective, mid_price=mid,
            slippage_bps=costs.slippage_bps(mid, effective, "sell"),
            fees=fees, fill_probability=1.0, reason="paper_fill")
