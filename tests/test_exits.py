"""Exit decision logic + engine retry/escalation behavior."""

import time

import pytest

from sniper import constants as C
from sniper.exits.engine import ExitEngine, SellOrder, evaluate_exit, realizable_multiple
from sniper.models import (
    ExitOutcome, FeeBreakdown, FillResult, PoolState, Position, RunMode, TriggerMode,
)

SOL = C.LAMPORTS_PER_SOL


def make_position(entry_price=1e-6, tokens=10 ** 9, entry_reserve=50 * SOL,
                  opened_ago=10.0) -> Position:
    return Position(
        id=1, launch_id=1, mint="M" * 32, pool="P" * 32, mode=RunMode.PAPER,
        trigger_mode=TriggerMode.FILTER_EDGE, tokens_total=tokens,
        tokens_remaining=tokens, sol_spent=int(entry_price * tokens),
        entry_price=entry_price, entry_sol_reserve=entry_reserve,
        opened_at=time.time() - opened_ago)


def pool_at_multiple(position: Position, multiple: float) -> PoolState:
    """Pool where realizable value/token ~= multiple * entry. The SOL reserve
    is pinned to the entry baseline so the LP-pull check stays quiet; the
    price moves via the token side."""
    price = position.entry_price * multiple * 1.01  # headroom for the amm fee
    sol_reserve = position.entry_sol_reserve
    return PoolState(pool=position.pool, token_mint=position.mint,
                     sol_reserve=sol_reserve,
                     token_reserve=int(sol_reserve / price))


class TestEvaluateExit:
    def test_hold_in_quiet_range(self, cfg):
        pos = make_position()
        order = evaluate_exit(pos, pool_at_multiple(pos, 1.2), time.time(), cfg)
        assert order is None

    def test_first_tier_at_2x(self, cfg):
        pos = make_position()
        order = evaluate_exit(pos, pool_at_multiple(pos, 2.1), time.time(), cfg)
        assert order is not None
        assert order.reason == ExitOutcome.TAKE_PROFIT
        assert order.tier_index == 0
        assert order.tokens == pos.tokens_total // 2          # sell 50%

    def test_second_tier_at_5x(self, cfg):
        pos = make_position()
        pos.tiers_filled = 1
        pos.tokens_remaining = pos.tokens_total // 2
        order = evaluate_exit(pos, pool_at_multiple(pos, 5.5), time.time(), cfg)
        assert order.reason == ExitOutcome.TAKE_PROFIT
        assert order.tier_index == 1
        assert order.tokens == pos.tokens_total // 4          # sell 25%

    def test_tier_not_refired(self, cfg):
        pos = make_position()
        pos.tiers_filled = 2
        order = evaluate_exit(pos, pool_at_multiple(pos, 3.0), time.time(), cfg)
        assert order is None                                   # both tiers done, hold

    def test_stop_loss(self, cfg):
        pos = make_position()
        order = evaluate_exit(pos, pool_at_multiple(pos, 0.3), time.time(), cfg)
        assert order.reason == ExitOutcome.STOP_LOSS
        assert order.tokens == pos.tokens_remaining

    def test_max_hold_force_exit(self, cfg):
        pos = make_position(opened_ago=cfg.exits.max_hold_s + 5)
        order = evaluate_exit(pos, pool_at_multiple(pos, 1.5), time.time(), cfg)
        assert order.reason == ExitOutcome.MAX_HOLD

    def test_lp_pull_is_emergency(self, cfg):
        pos = make_position(entry_reserve=50 * SOL)
        pool = pool_at_multiple(pos, 1.0)
        pool.sol_reserve = int(50 * SOL * 0.5)     # 50% drained > 40% trigger
        order = evaluate_exit(pos, pool, time.time(), cfg)
        assert order.reason == ExitOutcome.EMERGENCY_LP_PULL
        assert order.emergency

    def test_unreadable_pool_is_emergency(self, cfg):
        pos = make_position()
        order = evaluate_exit(pos, None, time.time(), cfg)
        assert order.reason == ExitOutcome.EMERGENCY_LP_PULL

    def test_lp_pull_beats_take_profit(self, cfg):
        pos = make_position()
        pool = pool_at_multiple(pos, 3.0)
        pool.sol_reserve = int(pos.entry_sol_reserve * 0.4)
        order = evaluate_exit(pos, pool, time.time(), cfg)
        assert order.reason == ExitOutcome.EMERGENCY_LP_PULL

    def test_realizable_multiple_accounts_for_depth(self, cfg):
        """A shallow pool can't pay chart price for your whole bag."""
        pos = make_position(tokens=10 ** 12)
        deep = pool_at_multiple(pos, 2.0)
        shallow = PoolState(pool=pos.pool, token_mint=pos.mint,
                            sol_reserve=deep.sol_reserve // 100,
                            token_reserve=deep.token_reserve // 100)
        assert realizable_multiple(pos, shallow, 25) < \
            realizable_multiple(pos, deep, 25)


class SellRecorder:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def __call__(self, position, pool, tokens, multiplier):
        self.calls.append(multiplier)
        result = self.outcomes.pop(0) if self.outcomes else self.outcomes_default()
        return result

    @staticmethod
    def outcomes_default():
        return FillResult(filled=True, side="sell", sol_delta=10 ** 7,
                          tokens_delta=-10 ** 9, fees=FeeBreakdown())


class TestEngineRetries:
    @pytest.fixture
    def updates(self):
        return []

    def make_engine(self, cfg, sell_fn, pool, updates):
        async def pool_fetch(_pos):
            return pool
        return ExitEngine(cfg, pool_fetch, sell_fn,
                          on_position_update=lambda p, f: updates.append((p, f)))

    async def test_fee_escalation_ladder_walked(self, cfg, pool, updates):
        fail = FillResult(filled=False, side="sell", reason="congested")
        ok = FillResult(filled=True, side="sell", sol_delta=10 ** 7,
                        tokens_delta=-10 ** 9, fees=FeeBreakdown())
        sell = SellRecorder([fail, fail, ok])
        cfg.exits.sell_retry_delay_s = 0.0
        engine = self.make_engine(cfg, sell, pool, updates)
        pos = make_position()
        engine.add_position(pos)
        await engine._execute_sell(pos, pool, SellOrder(
            tokens=10 ** 9, reason=ExitOutcome.STOP_LOSS))
        assert sell.calls == [1.0, 2.0, 5.0]        # ladder from config

    async def test_emergency_jumps_to_top_rung(self, cfg, pool, updates):
        ok = FillResult(filled=True, side="sell", sol_delta=10 ** 7,
                        tokens_delta=-10 ** 9, fees=FeeBreakdown())
        sell = SellRecorder([ok])
        engine = self.make_engine(cfg, sell, pool, updates)
        pos = make_position()
        engine.add_position(pos)
        await engine._execute_sell(pos, pool, SellOrder(
            tokens=10 ** 9, reason=ExitOutcome.EMERGENCY_LP_PULL, emergency=True))
        assert sell.calls == [cfg.exits.fee_escalation[-1]]

    async def test_ladder_exhausted_is_could_not_sell(self, cfg, pool, updates):
        fail = FillResult(filled=False, side="sell", reason="drained")
        sell = SellRecorder([fail] * 10)
        cfg.exits.sell_retry_delay_s = 0.0
        engine = self.make_engine(cfg, sell, pool, updates)
        pos = make_position()
        engine.add_position(pos)
        await engine._execute_sell(pos, pool, SellOrder(
            tokens=10 ** 9, reason=ExitOutcome.STOP_LOSS))
        assert pos.state == "closed"
        assert pos.outcome == ExitOutcome.COULD_NOT_SELL.value   # NOT stop_loss
        assert pos.net_pnl_lamports < 0

    async def test_tier_sell_updates_position(self, cfg, pool, updates):
        ok = FillResult(filled=True, side="sell", sol_delta=2 * 10 ** 7,
                        tokens_delta=-(10 ** 9 // 2), fees=FeeBreakdown())
        engine = self.make_engine(cfg, SellRecorder([ok]), pool, updates)
        pos = make_position()
        engine.add_position(pos)
        await engine._execute_sell(pos, pool, SellOrder(
            tokens=10 ** 9 // 2, reason=ExitOutcome.TAKE_PROFIT, tier_index=0))
        assert pos.tokens_remaining == 10 ** 9 // 2
        assert pos.tiers_filled == 1
        assert pos.state == "open"                   # partial exit keeps it open
        assert pos.sol_received == 2 * 10 ** 7

    async def test_terminal_sell_closes_position(self, cfg, pool, updates):
        ok = FillResult(filled=True, side="sell", sol_delta=10 ** 7,
                        tokens_delta=-10 ** 9, fees=FeeBreakdown())
        engine = self.make_engine(cfg, SellRecorder([ok]), pool, updates)
        pos = make_position()
        engine.add_position(pos)
        await engine._execute_sell(pos, pool, SellOrder(
            tokens=10 ** 9, reason=ExitOutcome.MAX_HOLD))
        assert pos.state == "closed"
        assert pos.outcome == ExitOutcome.MAX_HOLD.value
        assert engine.open_count == 0
