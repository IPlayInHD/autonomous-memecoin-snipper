"""Bootstrap CI + go-live gate behavior."""

import random
import time

import pytest

from sniper import constants as C
from sniper.evaluation.bootstrap import bootstrap_ci, percentile
from sniper.evaluation.protocol import evaluate_go_live
from sniper.models import RunMode
from sniper.storage.db import Database

SOL = C.LAMPORTS_PER_SOL


class TestBootstrap:
    def test_clearly_positive_sample(self):
        rng = random.Random(1)
        data = [rng.gauss(0.01, 0.002) for _ in range(300)]
        res = bootstrap_ci(data)
        assert res.significantly_positive
        assert res.ci_low < res.mean < res.ci_high

    def test_zero_mean_not_significant(self):
        rng = random.Random(2)
        data = [rng.gauss(0.0, 0.01) for _ in range(300)]
        assert not bootstrap_ci(data).significantly_positive

    def test_heavy_tail_widens_ci(self):
        """A few -100% rugs must widen the CI — this is why we bootstrap."""
        rng = random.Random(3)
        clean = [rng.gauss(0.005, 0.001) for _ in range(200)]
        rugged = clean[:190] + [-0.02] * 10       # 5% rug tail
        w_clean = bootstrap_ci(clean)
        w_rug = bootstrap_ci(rugged)
        assert (w_rug.ci_high - w_rug.ci_low) > (w_clean.ci_high - w_clean.ci_low)
        assert w_rug.p5 <= w_clean.p5

    def test_left_tail_reported(self):
        data = [-1.0] * 6 + [0.01] * 94       # 6% rug tail
        res = bootstrap_ci(data)
        assert res.worst == -1.0
        assert res.p5 < 0                      # p5 sits inside the rug tail

    def test_needs_two_samples(self):
        with pytest.raises(ValueError):
            bootstrap_ci([1.0])

    def test_percentile_interpolation(self):
        assert percentile([0.0, 10.0], 50) == 5.0


def _seed_positions(db: Database, pnls_sol, span_days=20.0, mode="paper"):
    now = time.time()
    t0 = now - span_days * 86400
    step = (span_days * 86400) / max(1, len(pnls_sol))
    for i, pnl in enumerate(pnls_sol):
        spent = int(0.015 * SOL)
        db.execute(
            "INSERT INTO positions(launch_id, mint, pool, mode, trigger_mode,"
            " sol_spent, sol_received, opened_at, closed_at, state, outcome,"
            " entry_price, entry_sol_reserve, decimals, tiers_filled,"
            " tokens_total, tokens_remaining)"
            " VALUES(1,?,'p',?,'filter_edge',?,?,?,?,'closed','take_profit',"
            " 0,0,6,0,0,0)",
            (f"mint{i}", mode, spent, spent + int(pnl * SOL),
             t0 + i * step, t0 + i * step + 60))


class TestGoLiveGate:
    def test_empty_db_is_no_go(self):
        report = evaluate_go_live(Database(":memory:"))
        assert not report.go

    def test_small_sample_fails_sample_size(self):
        db = Database(":memory:")
        _seed_positions(db, [0.01] * 20)
        report = evaluate_go_live(db, min_trades=300)
        crit = {c.name: c.passed for c in report.criteria}
        assert not crit["sample_size"]
        assert not report.go

    def test_losing_strategy_fails_expectancy(self):
        db = Database(":memory:")
        db.set_meta("prereg_sha256", "abc")
        rng = random.Random(4)
        pnls = [rng.gauss(-0.002, 0.003) for _ in range(400)]
        _seed_positions(db, pnls)
        report = evaluate_go_live(db, min_trades=300)
        crit = {c.name: c.passed for c in report.criteria}
        assert crit["sample_size"] and crit["duration"]
        assert not crit["expectancy_ci"]
        assert not report.go

    def test_winning_strategy_still_gated_on_shadow(self):
        """Even genuinely positive paper results must NOT pass without shadow
        calibration — paper alone can never justify live."""
        db = Database(":memory:")
        db.set_meta("prereg_sha256", "abc")
        rng = random.Random(5)
        pnls = [rng.gauss(0.004, 0.002) for _ in range(400)]
        _seed_positions(db, pnls)
        report = evaluate_go_live(db, min_trades=300)
        crit = {c.name: c.passed for c in report.criteria}
        assert crit["expectancy_ci"]
        assert crit["walk_forward"]
        assert not crit["shadow_calibration"]
        assert not report.go

    def test_prereg_change_mid_sample_voids_results(self):
        db = Database(":memory:")
        db.set_meta("prereg_sha256", "original")
        db.set_meta("prereg_current_sha256", "TAMPERED")
        _seed_positions(db, [0.01] * 400)
        report = evaluate_go_live(db, min_trades=300)
        crit = {c.name: c.passed for c in report.criteria}
        assert not crit["preregistration"]
        assert not report.go

    def test_regime_warning_on_short_window(self):
        db = Database(":memory:")
        _seed_positions(db, [0.01] * 50, span_days=10)
        report = evaluate_go_live(db)
        assert any("REGIME" in w for w in report.warnings)

    def test_could_not_sell_reported_distinctly(self):
        db = Database(":memory:")
        _seed_positions(db, [0.01] * 10)
        db.execute("UPDATE positions SET outcome='could_not_sell'"
                   " WHERE mint IN ('mint0','mint1')")
        report = evaluate_go_live(db)
        assert report.outcome_counts.get("could_not_sell") == 2
        assert any("COULD_NOT_SELL" in w for w in report.warnings)

    def test_shadow_calibration_pass(self):
        db = Database(":memory:")
        db.set_meta("prereg_sha256", "abc")
        rng = random.Random(6)
        _seed_positions(db, [rng.gauss(0.004, 0.002) for _ in range(400)],
                        span_days=20)
        for i in range(30):
            db.insert_calibration(i, predicted_fill_prob=0.85,
                                  actually_filled=(i % 10) != 0,   # 90% actual
                                  predicted_slippage_bps=120.0,
                                  actual_slippage_bps=150.0,
                                  predicted_tokens=1000, actual_tokens=980)
        report = evaluate_go_live(db, min_trades=300)
        crit = {c.name: c.passed for c in report.criteria}
        assert crit["shadow_calibration"]
        assert report.go   # every gate green => GO is reachable, just hard
