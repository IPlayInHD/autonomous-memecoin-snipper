"""Cost-model math: the paper P&L is only as honest as these functions."""

import pytest

from sniper import constants as C
from sniper.execution import costs

SOL = C.LAMPORTS_PER_SOL


class TestConstantProduct:
    def test_buy_has_price_impact(self):
        # spot: 1e12 tokens / 100 SOL => 10 tokens per lamport
        out = costs.constant_product_buy(1 * SOL, 100 * SOL, 10 ** 12, amm_fee_bps=0)
        spot = 10 ** 12 * (1 * SOL) // (100 * SOL)
        assert out < spot                      # always worse than spot
        assert out == pytest.approx(spot / 1.01, rel=0.001)  # 1% of pool => ~1% impact

    def test_fee_reduces_output(self):
        no_fee = costs.constant_product_buy(1 * SOL, 100 * SOL, 10 ** 12, 0)
        with_fee = costs.constant_product_buy(1 * SOL, 100 * SOL, 10 ** 12, 25)
        assert with_fee < no_fee

    def test_k_never_decreases_on_buy(self):
        sol_r, tok_r = 100 * SOL, 10 ** 12
        out = costs.constant_product_buy(1 * SOL, sol_r, tok_r, 25)
        k_before = sol_r * tok_r
        k_after = (sol_r + 1 * SOL) * (tok_r - out)
        assert k_after >= k_before

    def test_round_trip_loses_money(self):
        """Buy then immediately sell must always be negative (impact + fees)."""
        sol_r, tok_r = 100 * SOL, 10 ** 12
        tokens = costs.constant_product_buy(1 * SOL, sol_r, tok_r, 25)
        sol_back = costs.constant_product_sell(
            tokens, sol_r + 1 * SOL, tok_r - tokens, 25)
        assert sol_back < 1 * SOL

    def test_zero_and_negative_inputs(self):
        assert costs.constant_product_buy(0, 100, 100, 25) == 0
        assert costs.constant_product_buy(10, 0, 100, 25) == 0
        assert costs.constant_product_sell(-5, 100, 100, 25) == 0

    def test_sell_drained_pool_returns_zero(self):
        assert costs.constant_product_sell(10 ** 9, 0, 10 ** 12, 25) == 0


class TestFeesAndSlippage:
    def test_priority_fee_conversion(self):
        # 100_000 micro-lamports/CU * 120_000 CU = 12_000_000_000 micro = 12_000 lamports
        assert costs.priority_fee_lamports(100_000, 120_000) == 12_000

    def test_route_fee_split(self):
        net, fee = costs.take_route_fee(1 * SOL, 100)  # 1%
        assert fee == SOL // 100
        assert net + fee == 1 * SOL

    def test_slippage_sign_convention(self):
        assert costs.slippage_bps(100.0, 101.0, "buy") > 0     # paid more: bad
        assert costs.slippage_bps(100.0, 99.0, "sell") > 0     # got less: bad
        assert costs.slippage_bps(100.0, 100.0, "buy") == 0

    def test_adverse_selection_penalty(self):
        assert costs.apply_bps_penalty(10_000, 75) == 9_925
