"""Pipeline ordering/gating + score combination + threshold behavior."""

from conftest import FakeProvider, pk
from sniper.config import ScoreWeights
from sniper.filters.base import Filter, FilterContext
from sniper.filters.pipeline import FilterPipeline
from sniper.models import FilterResult, TriggerMode
from sniper.scoring import compute_score


class StubFilter(Filter):
    def __init__(self, name, passed=True, hard=True, fast=True, score=1.0):
        self.name, self.hard, self.fast = name, hard, fast
        self._passed, self._score = passed, score
        self.ran = False

    async def check(self, event, ctx):
        self.ran = True
        if self._passed:
            return self._pass(score=self._score)
        return self._fail("stub fail", score=self._score)


def fr(name, passed=True, hard=True, score=1.0, skipped=False):
    return FilterResult(name=name, passed=passed, hard=hard, score=score,
                        skipped=skipped)


class TestScoring:
    def test_hard_fail_zeroes_score(self):
        results = [fr("liquidity", score=1.0), fr("honeypot", passed=False, hard=True)]
        assert compute_score(results, ScoreWeights()) == 0.0

    def test_soft_fail_lowers_score(self):
        results = [fr("liquidity", score=1.0),
                   fr("lp_status", passed=False, hard=False, score=0.0)]
        s = compute_score(results, ScoreWeights())
        assert 0.0 < s < 1.0

    def test_weights_matter(self):
        # lp_status (weight 2) at 0 pulls harder than liquidity (weight 1) at 0
        heavy = [fr("liquidity", score=1.0), fr("lp_status", score=0.0, hard=False)]
        light = [fr("liquidity", score=0.0, hard=False), fr("lp_status", score=1.0)]
        w = ScoreWeights()
        assert compute_score(heavy, w) < compute_score(light, w)

    def test_skipped_excluded(self):
        results = [fr("liquidity", score=0.8), fr("deployer", skipped=True, score=0.0)]
        assert compute_score(results, ScoreWeights()) == 0.8

    def test_all_skipped_scores_zero(self):
        results = [fr("deployer", skipped=True)]
        assert compute_score(results, ScoreWeights()) == 0.0

    def test_bounded_zero_one(self):
        results = [fr("liquidity", score=1.0), fr("holders", score=1.0)]
        assert compute_score(results, ScoreWeights()) <= 1.0


class TestPipeline:
    async def test_hard_fail_stops_expensive_filters(self, cfg, event):
        cheap = StubFilter("cheap", passed=False, hard=True)
        expensive = StubFilter("expensive")
        pipe = FilterPipeline(cfg, [cheap, expensive], threshold=0.5)
        out = await pipe.run(event, FilterContext(cfg, FakeProvider()),
                             TriggerMode.FILTER_EDGE)
        assert out.rejected_by == "cheap"
        assert not out.accepted
        assert not expensive.ran                     # never paid for

    async def test_soft_fail_continues(self, cfg, event):
        soft = StubFilter("soft", passed=False, hard=False, score=0.0)
        after = StubFilter("after")
        pipe = FilterPipeline(cfg, [soft, after], threshold=0.1)
        out = await pipe.run(event, FilterContext(cfg, FakeProvider()),
                             TriggerMode.FILTER_EDGE)
        assert after.ran
        assert out.rejected_by is None

    async def test_block0_skips_slow_filters(self, cfg, event):
        fast = StubFilter("fast", fast=True)
        slow = StubFilter("slow", fast=False)
        pipe = FilterPipeline(cfg, [fast, slow], threshold=0.0)
        out = await pipe.run(event, FilterContext(cfg, FakeProvider()),
                             TriggerMode.BLOCK_0)
        assert not slow.ran
        skipped = [r for r in out.results if r.skipped]
        assert len(skipped) == 1 and skipped[0].name == "slow"

    async def test_threshold_gates_acceptance(self, cfg, event):
        mediocre = StubFilter("m", score=0.6)
        pipe_hi = FilterPipeline(cfg, [mediocre], threshold=0.9)
        out = await pipe_hi.run(event, FilterContext(cfg, FakeProvider()),
                                TriggerMode.FILTER_EDGE)
        assert not out.accepted                      # 0.6 < 0.9
        pipe_lo = FilterPipeline(cfg, [StubFilter("m", score=0.6)], threshold=0.5)
        out = await pipe_lo.run(event, FilterContext(cfg, FakeProvider()),
                                TriggerMode.FILTER_EDGE)
        assert out.accepted

    async def test_rejection_reasons_logged(self, cfg, event):
        pipe = FilterPipeline(cfg, [StubFilter("bad", passed=False)], threshold=0.5)
        out = await pipe.run(event, FilterContext(cfg, FakeProvider()),
                             TriggerMode.FILTER_EDGE)
        assert out.rejection_reasons() == ["bad: stub fail"]

    async def test_filters_done_latency_stamped(self, cfg, event):
        pipe = FilterPipeline(cfg, [StubFilter("f")], threshold=0.0)
        await pipe.run(event, FilterContext(cfg, FakeProvider()),
                       TriggerMode.FILTER_EDGE)
        assert event.latency.filters_done is not None
