"""Combine filter outputs into one 0-1 confidence score.

Rules:
- Any hard failure -> 0. There is no score high enough to buy a token whose
  freeze authority is live.
- Otherwise: weighted mean of per-filter scores. Weights live in config
  (`score_weights`); filters without a configured weight get 1.0.
- Skipped filters are excluded from the mean, but total skipped weight is
  tracked: a score computed with half the signals missing is *reported* the
  same but the pipeline logs coverage so the analysis can slice on it.

The acceptance threshold is NOT defined here — it comes from the
pre-registration file and must be frozen before evaluation data is collected.
"""

from __future__ import annotations

from .config import ScoreWeights
from .models import FilterResult

# structural pass/fail filters contribute via hard-fail, not the weighted mean
_WEIGHT_BY_FILTER = {
    "liquidity": "liquidity",
    "holders": "holders",
    "deployer": "deployer",
    "bundle": "bundle",
    "lp_status": "lp_status",
    "honeypot": "honeypot",
}


def compute_score(results: list[FilterResult], weights: ScoreWeights) -> float:
    if any((not r.passed) and r.hard for r in results):
        return 0.0
    total_weight = 0.0
    acc = 0.0
    for r in results:
        if r.skipped:
            continue
        attr = _WEIGHT_BY_FILTER.get(r.name)
        w = getattr(weights, attr, 1.0) if attr else 1.0
        acc += w * r.score
        total_weight += w
    if total_weight == 0.0:
        return 0.0
    return max(0.0, min(1.0, acc / total_weight))


def coverage(results: list[FilterResult]) -> float:
    """Fraction of scoring weight that actually ran (vs skipped)."""
    ran = sum(1 for r in results if not r.skipped)
    return ran / len(results) if results else 0.0
