"""Risk layer: every brake must actually stop the machine."""

import time

import pytest

from sniper import constants as C
from sniper.models import FeeBreakdown, FillResult, RunMode
from sniper.risk.manager import RiskManager
from sniper.storage.db import Database

SOL = C.LAMPORTS_PER_SOL


@pytest.fixture
def db():
    return Database(":memory:")


@pytest.fixture
def risk(cfg, db, tmp_path):
    cfg.risk.kill_switch_file = str(tmp_path / "KILL")
    return RiskManager(cfg, db)   # sol price = fallback $150


def test_per_trade_cap_blocks_oversize(risk):
    # $2 cap at $150/SOL => 0.0133 SOL max; 0.02 SOL is over
    ok, reason = risk.check_entry(int(0.02 * SOL), 0, RunMode.PAPER)
    assert not ok and "per-trade cap" in reason


def test_normal_entry_allowed(risk):
    ok, _ = risk.check_entry(int(0.01 * SOL), 0, RunMode.PAPER)
    assert ok


def test_max_concurrent_positions(risk, cfg):
    ok, reason = risk.check_entry(int(0.01 * SOL),
                                  cfg.risk.max_concurrent_positions, RunMode.PAPER)
    assert not ok and "concurrent" in reason


def test_kill_switch_file(risk, cfg, tmp_path):
    (tmp_path / "KILL").touch()
    ok, _ = risk.check_entry(int(0.01 * SOL), 0, RunMode.PAPER)
    assert not ok
    assert risk.halted


def test_daily_loss_cap(risk, cfg, db):
    # insert a closed paper position losing $12 worth of SOL (cap $10)
    loss_lamports = int(12 / 150 * SOL)
    db.execute(
        "INSERT INTO positions(launch_id, mint, pool, mode, trigger_mode,"
        " sol_spent, sol_received, opened_at, closed_at, state, entry_price,"
        " entry_sol_reserve, decimals, tiers_filled, tokens_total, tokens_remaining)"
        " VALUES(1,'m','p','paper','filter_edge',?,0,?,?,'closed',0,0,6,0,0,0)",
        (loss_lamports, time.time() - 60, time.time() - 30))
    ok, reason = risk.check_entry(int(0.01 * SOL), 0, RunMode.PAPER)
    assert not ok and "daily loss" in reason
    assert risk.entries_halted


def test_daily_fee_cap(risk, cfg, db):
    fill = FillResult(filled=False, side="buy", sol_delta=-10 ** 8,
                      fees=FeeBreakdown(base_fee=5000,
                                        priority_fee=int(0.06 * SOL)))
    db.insert_trade(fill, RunMode.PAPER, None, None)
    ok, reason = risk.check_entry(int(0.01 * SOL), 0, RunMode.PAPER)
    assert not ok and "fee cap" in reason


def test_sol_floor_reserve(risk):
    balance = int(0.055 * SOL)      # floor is 0.05 SOL
    ok, reason = risk.check_entry(int(0.01 * SOL), 0, RunMode.LIVE, balance)
    assert not ok and "floor" in reason
    ok, _ = risk.check_entry(int(0.004 * SOL), 0, RunMode.LIVE, balance)
    assert ok


def test_hard_halt_blocks_everything(risk):
    risk.halt("test")
    ok, reason = risk.check_entry(1, 0, RunMode.PAPER)
    assert not ok and "halted" in reason


def test_unhandled_exception_halts(risk):
    risk.note_unhandled_exception("worker-1", RuntimeError("boom"))
    assert risk.halted


class TestKelly:
    def test_no_edge_no_position(self, risk):
        # 50% win rate, symmetric payoff => zero Kelly
        assert risk.kelly_size_lamports(10 * SOL, 0.5, 1.0, 1.0) == 0

    def test_negative_edge_no_position(self, risk):
        assert risk.kelly_size_lamports(10 * SOL, 0.3, 1.0, 1.0) == 0

    def test_positive_edge_discounted_and_capped(self, risk, cfg):
        # strong edge: 60% wins at 2:1 payoff => kelly = .6 - .4/2 = .4
        size = risk.kelly_size_lamports(10 * SOL, 0.6, 2.0, 1.0)
        full_kelly = int(10 * SOL * 0.4)
        assert size <= full_kelly * cfg.risk.kelly_discount
        cap = int(cfg.risk.per_trade_cap_usd / 150 * SOL)
        assert size <= cap

    def test_degenerate_inputs(self, risk):
        assert risk.kelly_size_lamports(10 * SOL, 1.0, 1.0, 0.0) == 0
        assert risk.kelly_size_lamports(10 * SOL, 0.0, 1.0, 1.0) == 0
