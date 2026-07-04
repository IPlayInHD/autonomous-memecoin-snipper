"""Paper fill model: every cost must show up in the simulated P&L."""

from sniper import constants as C
from sniper.execution.paper import PaperExecutor
from sniper.models import TriggerMode

SOL = C.LAMPORTS_PER_SOL


def test_filled_buy_charges_everything(cfg, pool):
    ex = PaperExecutor(cfg)
    fill = ex.buy(pool, SOL // 100, TriggerMode.FILTER_EDGE,
                  fill_probability_override=1.0)
    assert fill.filled
    assert fill.tokens_delta > 0
    # wallet outflow = principal + base + priority + rent (route fee inside principal)
    expected_out = SOL // 100 + fill.fees.base_fee + fill.fees.priority_fee \
        + fill.fees.ata_rent
    assert -fill.sol_delta == expected_out
    assert fill.fees.route_fee == (SOL // 100) * 100 // 10_000  # 1% route fee
    assert fill.fees.ata_rent == cfg.execution.paper.ata_rent_lamports
    assert fill.effective_price > fill.mid_price      # buys never beat mid
    assert fill.slippage_bps > 0


def test_paper_never_beats_theoretical_no_cost_fill(cfg, pool):
    from sniper.execution import costs
    ex = PaperExecutor(cfg)
    fill = ex.buy(pool, SOL // 100, TriggerMode.FILTER_EDGE,
                  fill_probability_override=1.0)
    frictionless = costs.constant_product_buy(
        SOL // 100, pool.sol_reserve, pool.token_reserve, 0)
    assert fill.tokens_delta < frictionless


def test_unfilled_buy_still_burns_fees(cfg, pool):
    ex = PaperExecutor(cfg)
    fill = ex.buy(pool, SOL // 100, TriggerMode.BLOCK_0,
                  fill_probability_override=0.0)
    assert not fill.filled
    assert fill.tokens_delta == 0
    assert fill.sol_delta < 0                      # fees burned on the attempt
    assert -fill.sol_delta == fill.fees.base_fee + fill.fees.priority_fee
    assert fill.fees.jito_tip == 0                 # atomic bundles don't tip on miss


def test_fill_probability_by_trigger_mode(cfg, pool):
    """block_0 on shared RPC must be modeled as mostly losing the race."""
    cfg.execution.paper.rng_seed = 1234
    ex = PaperExecutor(cfg)
    fills = [ex.buy(pool, SOL // 100, TriggerMode.BLOCK_0) for _ in range(400)]
    rate = sum(1 for f in fills if f.filled) / len(fills)
    assert 0.2 < rate < 0.4                        # configured 0.30


def test_sell_returns_less_than_frictionless(cfg, pool):
    from sniper.execution import costs
    ex = PaperExecutor(cfg)
    tokens = 10 ** 9
    fill = ex.sell(pool, tokens)
    assert fill.filled
    assert fill.tokens_delta == -tokens
    frictionless = costs.constant_product_sell(
        tokens, pool.sol_reserve, pool.token_reserve, 0)
    assert 0 < fill.sol_delta < frictionless


def test_sell_priority_escalation_costs_more(cfg, pool):
    ex = PaperExecutor(cfg)
    normal = ex.sell(pool, 10 ** 9, priority_fee_multiplier=1.0)
    panic = ex.sell(pool, 10 ** 9, priority_fee_multiplier=10.0)
    assert panic.fees.priority_fee == 10 * normal.fees.priority_fee
    assert panic.sol_delta < normal.sol_delta


def test_sell_into_rugged_pool_is_could_not_sell(cfg, pool):
    ex = PaperExecutor(cfg)
    pool.sol_reserve = 0                            # LP pulled
    fill = ex.sell(pool, 10 ** 9)
    assert not fill.filled
    assert "could_not_sell" in fill.reason
    assert fill.sol_delta == 0


def test_dust_proceeds_below_fees_is_could_not_sell(cfg, pool):
    ex = PaperExecutor(cfg)
    pool.sol_reserve = 10_000                       # pool nearly drained
    fill = ex.sell(pool, 10 ** 6)
    assert not fill.filled
    assert "could_not_sell" in fill.reason


def test_round_trip_paper_pnl_is_negative(cfg, pool):
    """Flat market: entry + immediate exit must lose ~fees+impact. If this ever
    passes with a profit the cost model is broken (optimistic)."""
    ex = PaperExecutor(cfg)
    buy = ex.buy(pool, SOL // 100, TriggerMode.FILTER_EDGE,
                 fill_probability_override=1.0)
    pool.sol_reserve += SOL // 100
    pool.token_reserve -= buy.tokens_delta
    sell = ex.sell(pool, buy.tokens_delta)
    net = buy.sol_delta + sell.sol_delta
    assert net < 0
    # and the loss is material: worse than -2% on a 0.01 SOL clip
    assert net < -(SOL // 100) * 2 // 100
