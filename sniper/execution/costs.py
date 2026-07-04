"""Pure swap/cost math. Everything the paper model charges lives here so it
can be unit-tested to the lamport.

Paper P&L is an optimistic UPPER BOUND even with all of this subtracted —
these functions make sure it is at least not a fantasy:
  - constant-product price impact against actual pool depth (both directions)
  - AMM pool fee (amm_fee_bps) inside the curve math
  - route/aggregator fee (route_fee_bps, ~1%) on the input
  - adverse selection: price drift between quote time and landing
  - base + priority fees, optional Jito tip, ATA rent (tracked as recoverable)
"""

from __future__ import annotations

from ..models import FeeBreakdown

BPS = 10_000


def constant_product_buy(sol_in: int, sol_reserve: int, token_reserve: int,
                         amm_fee_bps: int) -> int:
    """Tokens received for `sol_in` lamports against x*y=k, fee on input."""
    if sol_in <= 0 or sol_reserve <= 0 or token_reserve <= 0:
        return 0
    sol_in_after_fee = sol_in * (BPS - amm_fee_bps) // BPS
    return token_reserve * sol_in_after_fee // (sol_reserve + sol_in_after_fee)


def constant_product_sell(tokens_in: int, sol_reserve: int, token_reserve: int,
                          amm_fee_bps: int) -> int:
    """Lamports received for `tokens_in` raw tokens, fee on input."""
    if tokens_in <= 0 or sol_reserve <= 0 or token_reserve <= 0:
        return 0
    tokens_after_fee = tokens_in * (BPS - amm_fee_bps) // BPS
    return sol_reserve * tokens_after_fee // (token_reserve + tokens_after_fee)


def mid_price(sol_reserve: int, token_reserve: int) -> float:
    """Marginal pool price in lamports per raw token unit."""
    if token_reserve <= 0:
        return 0.0
    return sol_reserve / token_reserve


def slippage_bps(mid: float, effective: float, side: str) -> float:
    """Execution shortfall vs mid, positive = worse than mid.
    Buys are worse when effective > mid; sells when effective < mid."""
    if mid <= 0:
        return 0.0
    if side == "buy":
        return (effective - mid) / mid * BPS
    return (mid - effective) / mid * BPS


def priority_fee_lamports(cu_price_microlamports: int, compute_units: int) -> int:
    """Priority fee = CU price (micro-lamports) * CU limit, in lamports."""
    return cu_price_microlamports * compute_units // 1_000_000


def apply_bps_penalty(amount: int, penalty_bps: int) -> int:
    """Shave `penalty_bps` off an output amount (adverse selection model)."""
    return amount * (BPS - penalty_bps) // BPS


def take_route_fee(amount: int, route_fee_bps: int) -> tuple[int, int]:
    """Split an amount into (net_after_fee, fee)."""
    fee = amount * route_fee_bps // BPS
    return amount - fee, fee


def entry_fees(cfg_priority_fee_microlamports: int, compute_units: int,
               jito_tip: int, needs_ata: bool, base_fee: int,
               ata_rent: int) -> FeeBreakdown:
    return FeeBreakdown(
        base_fee=base_fee,
        priority_fee=priority_fee_lamports(cfg_priority_fee_microlamports, compute_units),
        jito_tip=jito_tip,
        ata_rent=ata_rent if needs_ata else 0,
    )
