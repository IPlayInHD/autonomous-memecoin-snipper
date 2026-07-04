"""Filter behavior against a fake provider, incl. honeypot/Token-2022 cases."""

import pytest

from conftest import (
    FakeProvider, make_mint_bytes, make_t22_mint_bytes, pk, pk_bytes,
    transfer_fee_payload, transfer_hook_payload,
)
from sniper import constants as C
from sniper.chain.provider import parse_mint
from sniper.filters.base import FilterContext
from sniper.filters.honeypot import HoneypotFilter
from sniper.filters.lp_filters import LiquidityFilter, LpStatusFilter
from sniper.filters.mint_filters import AuthoritiesFilter, Token2022Filter
from sniper.filters.wallet_filters import (
    BundleFilter, DeployerFilter, HolderConcentrationFilter,
)
from sniper.models import PoolState, SimulateResult, TokenHolding


def ctx_for(cfg, provider):
    return FilterContext(cfg, provider)


# ---------------------------------------------------------------- authorities

async def test_authorities_pass_when_revoked(cfg, provider, event):
    provider.add_clean_mint(event.mint)
    result = await AuthoritiesFilter().run(event, ctx_for(cfg, provider))
    assert result.passed


async def test_authorities_fail_on_live_mint_authority(cfg, provider, event):
    data = make_mint_bytes(mint_authority=pk_bytes(9))
    provider.mints[event.mint] = parse_mint(data, C.TOKEN_PROGRAM, event.mint)
    result = await AuthoritiesFilter().run(event, ctx_for(cfg, provider))
    assert not result.passed and result.hard
    assert "mint authority" in result.reason


async def test_authorities_fail_on_live_freeze_authority(cfg, provider, event):
    data = make_mint_bytes(freeze_authority=pk_bytes(9))
    provider.mints[event.mint] = parse_mint(data, C.TOKEN_PROGRAM, event.mint)
    result = await AuthoritiesFilter().run(event, ctx_for(cfg, provider))
    assert not result.passed
    assert "freeze authority" in result.reason


async def test_missing_mint_fails_closed(cfg, provider, event):
    result = await AuthoritiesFilter().run(event, ctx_for(cfg, provider))
    assert not result.passed


# ---------------------------------------------------------------- token-2022

async def test_token2022_transfer_hook_is_hard_fail(cfg, provider, event):
    data = make_t22_mint_bytes(
        [(C.EXT_TRANSFER_HOOK, transfer_hook_payload(pk_bytes(11)))])
    provider.mints[event.mint] = parse_mint(data, C.TOKEN_2022_PROGRAM, event.mint)
    result = await Token2022Filter().run(event, ctx_for(cfg, provider))
    assert not result.passed and result.hard
    assert "transfer hook" in result.reason


async def test_token2022_transfer_fee_rejected(cfg, provider, event):
    data = make_t22_mint_bytes(
        [(C.EXT_TRANSFER_FEE_CONFIG, transfer_fee_payload(bps=300))])
    provider.mints[event.mint] = parse_mint(data, C.TOKEN_2022_PROGRAM, event.mint)
    result = await Token2022Filter().run(event, ctx_for(cfg, provider))
    assert not result.passed
    assert "transfer fee" in result.reason


async def test_token2022_permanent_delegate_rejected(cfg, provider, event):
    data = make_t22_mint_bytes([(C.EXT_PERMANENT_DELEGATE, pk_bytes(12))])
    provider.mints[event.mint] = parse_mint(data, C.TOKEN_2022_PROGRAM, event.mint)
    result = await Token2022Filter().run(event, ctx_for(cfg, provider))
    assert not result.passed


async def test_token2022_default_frozen_rejected(cfg, provider, event):
    data = make_t22_mint_bytes([(C.EXT_DEFAULT_ACCOUNT_STATE, bytes([2]))])
    provider.mints[event.mint] = parse_mint(data, C.TOKEN_2022_PROGRAM, event.mint)
    result = await Token2022Filter().run(event, ctx_for(cfg, provider))
    assert not result.passed


async def test_token2022_unknown_extension_rejected(cfg, provider, event):
    data = make_t22_mint_bytes([(99, b"\x00" * 8)])
    provider.mints[event.mint] = parse_mint(data, C.TOKEN_2022_PROGRAM, event.mint)
    result = await Token2022Filter().run(event, ctx_for(cfg, provider))
    assert not result.passed


async def test_clean_classic_token_passes(cfg, provider, event):
    provider.add_clean_mint(event.mint)
    result = await Token2022Filter().run(event, ctx_for(cfg, provider))
    assert result.passed


# ---------------------------------------------------------------- liquidity

async def test_liquidity_below_minimum_rejected(cfg, provider, event, pool):
    pool.sol_reserve = int(0.5 * C.LAMPORTS_PER_SOL)
    provider.pool = pool
    result = await LiquidityFilter().run(event, ctx_for(cfg, provider))
    assert not result.passed


async def test_liquidity_scales_score(cfg, provider, event, pool):
    provider.pool = pool  # 50 SOL vs min 5 => saturated score
    result = await LiquidityFilter().run(event, ctx_for(cfg, provider))
    assert result.passed
    assert result.score > 0.9


# ---------------------------------------------------------------- LP status

def lp_mint_info(event, supply=10 ** 9, authority_gone=True):
    data = make_mint_bytes(None if authority_gone else pk_bytes(3), None,
                           supply=supply)
    return parse_mint(data, C.TOKEN_PROGRAM, event.lp_mint)


async def test_lp_burned_scores_full(cfg, provider, event, pool):
    provider.pool = pool
    provider.mints[event.lp_mint] = lp_mint_info(event)
    provider.holders[event.lp_mint] = [
        TokenHolding(address=pk(30), owner=C.INCINERATOR, amount=10 ** 9)]
    result = await LpStatusFilter().run(event, ctx_for(cfg, provider))
    assert result.passed and result.score == 1.0
    assert result.data["burned"] and not result.data["locked"]


async def test_lp_locked_is_distinct_and_weaker(cfg, provider, event, pool):
    locker = next(iter(C.KNOWN_LOCKER_PROGRAMS))
    provider.pool = pool
    provider.mints[event.lp_mint] = lp_mint_info(event)
    provider.holders[event.lp_mint] = [
        TokenHolding(address=pk(30), owner=locker, amount=10 ** 9)]
    result = await LpStatusFilter().run(event, ctx_for(cfg, provider))
    assert result.passed
    assert result.data["locked"] and not result.data["burned"]
    assert result.score < 1.0                       # locked < burned, never equal


async def test_lp_held_by_random_wallet_fails(cfg, provider, event, pool):
    provider.pool = pool
    provider.mints[event.lp_mint] = lp_mint_info(event)
    provider.holders[event.lp_mint] = [
        TokenHolding(address=pk(30), owner=pk(31), amount=10 ** 9)]
    result = await LpStatusFilter().run(event, ctx_for(cfg, provider))
    assert not result.passed
    assert "rug-capable" in result.reason


async def test_lp_skipped_for_bonding_curve(cfg, provider, event, pool):
    pool.is_bonding_curve = True
    provider.pool = pool
    result = await LpStatusFilter().run(event, ctx_for(cfg, provider))
    assert result.skipped


# ---------------------------------------------------------------- holders

async def test_top_holder_over_cap_rejected(cfg, provider, event):
    provider.supplies[event.mint] = 10 ** 12
    provider.holders[event.mint] = [
        TokenHolding(address=pk(40), owner=pk(41), amount=3 * 10 ** 11)]  # 30%
    result = await HolderConcentrationFilter().run(event, ctx_for(cfg, provider))
    assert not result.passed
    assert "top holder" in result.reason


async def test_deployer_concentration_rejected(cfg, provider, event):
    provider.supplies[event.mint] = 10 ** 12
    provider.holders[event.mint] = [
        TokenHolding(address=pk(40), owner=event.creator, amount=15 * 10 ** 10)]  # 15%
    result = await HolderConcentrationFilter().run(event, ctx_for(cfg, provider))
    assert not result.passed
    assert "deployer" in result.reason


async def test_pool_accounts_excluded_from_concentration(cfg, provider, event):
    provider.supplies[event.mint] = 10 ** 12
    provider.holders[event.mint] = [
        TokenHolding(address=event.base_vault, owner=event.pool, amount=9 * 10 ** 11),
        TokenHolding(address=pk(40), owner=pk(41), amount=5 * 10 ** 10)]  # 5%
    result = await HolderConcentrationFilter().run(event, ctx_for(cfg, provider))
    assert result.passed


# ---------------------------------------------------------------- bundles

async def test_bundled_supply_rejected(cfg, provider, event):
    provider.supplies[event.mint] = 10 ** 12
    provider.launch_buys = {pk(50): 2 * 10 ** 11, pk(51): 1 * 10 ** 11}  # 30%
    result = await BundleFilter().run(event, ctx_for(cfg, provider))
    assert not result.passed
    assert "launch block" in result.reason


async def test_small_bundle_passes_with_score(cfg, provider, event):
    provider.supplies[event.mint] = 10 ** 12
    provider.launch_buys = {pk(50): 5 * 10 ** 10}  # 5%
    result = await BundleFilter().run(event, ctx_for(cfg, provider))
    assert result.passed
    assert result.score < 1.0


# ---------------------------------------------------------------- deployer

class FakeHelius:
    def __init__(self, info):
        self.info = info
        self.enabled = True

    async def deployer_info(self, wallet):
        return self.info


async def test_deployer_prior_rugs_rejected(cfg, provider, event):
    from sniper.models import DeployerInfo
    import time
    helius = FakeHelius(DeployerInfo(wallet=event.creator, prior_rugs=2,
                                     first_tx_time=time.time() - 90 * 86400))
    result = await DeployerFilter().run(event, FilterContext(cfg, provider, helius))
    assert not result.passed
    assert "rug" in result.reason


async def test_fresh_deployer_scores_low(cfg, provider, event):
    from sniper.models import DeployerInfo
    import time
    helius = FakeHelius(DeployerInfo(wallet=event.creator,
                                     first_tx_time=time.time() - 3600))
    result = await DeployerFilter().run(event, FilterContext(cfg, provider, helius))
    assert not result.passed        # 1h-old wallet < min 1 day
    assert not result.hard          # but soft: it lowers the score, not a veto


async def test_deployer_skipped_without_api(cfg, provider, event):
    result = await DeployerFilter().run(event, FilterContext(cfg, provider, None))
    assert result.skipped


# ---------------------------------------------------------------- honeypot

async def test_honeypot_sell_sim_success_passes(cfg, provider, event, pool):
    provider.pool = pool
    provider.sim_result = SimulateResult(success=True, units_consumed=90_000)
    result = await HoneypotFilter().run(event, ctx_for(cfg, provider))
    assert result.passed


async def test_honeypot_frozen_account_is_fatal(cfg, provider, event, pool):
    provider.pool = pool
    provider.sim_result = SimulateResult(
        success=False, error="Error: AccountFrozen",
        logs=["Program log: Error: AccountFrozen"])
    result = await HoneypotFilter().run(event, ctx_for(cfg, provider))
    assert not result.passed and result.hard
    assert "sell blocked" in result.reason


async def test_honeypot_transient_failure_distinct(cfg, provider, event, pool):
    provider.pool = pool
    provider.sim_result = SimulateResult(success=False, error="no sell route available")
    result = await HoneypotFilter().run(event, ctx_for(cfg, provider))
    assert not result.passed
    assert result.data.get("transient")


async def test_crashed_filter_fails_closed(cfg, event):
    class ExplodingProvider(FakeProvider):
        async def get_mint_info(self, mint):
            raise RuntimeError("rpc exploded")
    result = await AuthoritiesFilter().run(event, ctx_for(cfg, ExplodingProvider()))
    assert not result.passed
    assert "filter error" in result.reason
