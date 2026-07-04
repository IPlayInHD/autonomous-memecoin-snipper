"""DB guards (dedupe, double-buy), preregistration record, config validation."""

import json

import pytest

from conftest import pk
from sniper.config import Config, Preregistration, validate_config
from sniper.models import LaunchEvent, LaunchSource, Position, RunMode, TriggerMode
from sniper.storage.db import Database


@pytest.fixture
def db():
    return Database(":memory:")


def make_event(mint=None, source=LaunchSource.PUMPFUN_LAUNCH):
    return LaunchEvent(mint=mint or pk(1), source=source, signature="s1",
                       slot=5, block_time=1_700_000_000.0)


class TestLaunchDedupe:
    def test_duplicate_launch_rejected(self, db):
        assert db.insert_launch(make_event()) is not None
        assert db.insert_launch(make_event()) is None          # same mint+source

    def test_same_mint_different_source_allowed(self, db):
        assert db.insert_launch(make_event()) is not None
        assert db.insert_launch(
            make_event(source=LaunchSource.PUMPFUN_MIGRATION)) is not None


class TestDoubleBuyGuard:
    def make_position(self, mint, state="open"):
        return Position(id=None, launch_id=1, mint=mint, pool="p",
                        mode=RunMode.PAPER, trigger_mode=TriggerMode.FILTER_EDGE,
                        state=state)

    def test_second_open_position_same_mint_rejected(self, db):
        assert db.insert_position(self.make_position(pk(1))) is not None
        assert db.insert_position(self.make_position(pk(1))) is None

    def test_reopen_after_close_allowed(self, db):
        pos = self.make_position(pk(1))
        pos.id = db.insert_position(pos)
        pos.state = "closed"
        db.update_position(pos)
        assert db.insert_position(self.make_position(pk(1))) is not None

    def test_has_open_position(self, db):
        db.insert_position(self.make_position(pk(1)))
        assert db.has_open_position(pk(1), RunMode.PAPER)
        assert not db.has_open_position(pk(1), RunMode.LIVE)


class TestPrereg:
    def test_first_record_wins(self, db):
        assert db.record_preregistration("hash1", "payload")
        assert db.record_preregistration("hash1", "payload")   # same is fine
        assert not db.record_preregistration("hash2", "payload")  # change detected

    def test_load_validates(self, tmp_path):
        f = tmp_path / "prereg.json"
        f.write_text(json.dumps({"score_threshold": 0.7,
                                 "trigger_mode": "filter_edge",
                                 "registered_at": "2026-01-01"}))
        pre = Preregistration.load(str(f))
        assert pre.score_threshold == 0.7
        assert len(pre.sha256) == 64

    def test_bad_threshold_rejected(self, tmp_path):
        f = tmp_path / "prereg.json"
        f.write_text(json.dumps({"score_threshold": 1.5,
                                 "trigger_mode": "filter_edge",
                                 "registered_at": "x"}))
        with pytest.raises(ValueError):
            Preregistration.load(str(f))


class TestConfigValidation:
    def test_live_requires_flag_and_ack(self):
        cfg = Config()
        cfg.execution.run_mode = "live"
        with pytest.raises(ValueError):                       # flag off
            validate_config(cfg, {})
        cfg.execution.live_enabled = True
        with pytest.raises(ValueError):                       # ack missing
            validate_config(cfg, {})
        with pytest.raises(ValueError):                       # wrong ack
            validate_config(cfg, {"SNIPER_LIVE_ACK": "yes"})
        validate_config(cfg, {"SNIPER_LIVE_ACK":
                              cfg.execution.live_ack_value})  # both => ok

    def test_paper_needs_no_ack(self):
        validate_config(Config(), {})

    def test_dashboard_refuses_public_bind(self):
        cfg = Config()
        cfg.dashboard.host = "0.0.0.0"
        with pytest.raises(ValueError, match="localhost"):
            validate_config(cfg, {})

    def test_exit_tiers_over_100pct_rejected(self):
        from sniper.config import ExitTier
        cfg = Config()
        cfg.exits.tiers = [ExitTier(2.0, 60.0), ExitTier(5.0, 60.0)]
        with pytest.raises(ValueError, match="100"):
            validate_config(cfg, {})


class TestFeeAccounting:
    def test_fee_spend_aggregation(self, db):
        from sniper.models import FeeBreakdown, FillResult
        fill = FillResult(filled=True, side="buy", sol_delta=-100,
                          fees=FeeBreakdown(base_fee=5000, priority_fee=12000,
                                            route_fee=1000, ata_rent=2039280))
        db.insert_trade(fill, RunMode.PAPER, None, None)
        # ata rent is recoverable: excluded from the fee-bleed number
        assert db.fees_spent_since(0, ("paper",)) == 5000 + 12000 + 1000
