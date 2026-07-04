"""Bootstrap confidence intervals for per-trade expectancy.

Memecoin P&L is violently heavy-tailed (many -100% rugs, rare 10x) — naive
t-tests assume away exactly the tail that kills you. The percentile bootstrap
makes no distributional assumption: resample trades with replacement, recompute
the mean, read the CI off the resampled distribution.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass
from typing import Sequence


@dataclass
class BootstrapResult:
    n: int
    mean: float
    median: float
    ci_low: float
    ci_high: float
    ci_level: float
    p5: float          # left tail — where the rugs live
    worst: float

    @property
    def significantly_positive(self) -> bool:
        return self.ci_low > 0.0


def percentile(sorted_data: Sequence[float], pct: float) -> float:
    if not sorted_data:
        raise ValueError("empty data")
    k = (len(sorted_data) - 1) * pct / 100.0
    lo, hi = int(k), min(int(k) + 1, len(sorted_data) - 1)
    frac = k - lo
    return sorted_data[lo] * (1 - frac) + sorted_data[hi] * frac


def bootstrap_ci(samples: Sequence[float], n_boot: int = 10_000,
                 ci_level: float = 0.95, seed: int | None = 42) -> BootstrapResult:
    if len(samples) < 2:
        raise ValueError("need at least 2 samples to bootstrap")
    rng = random.Random(seed)
    n = len(samples)
    means = []
    for _ in range(n_boot):
        resample = [samples[rng.randrange(n)] for _ in range(n)]
        means.append(sum(resample) / n)
    means.sort()
    alpha = (1.0 - ci_level) / 2.0
    data_sorted = sorted(samples)
    return BootstrapResult(
        n=n,
        mean=sum(samples) / n,
        median=statistics.median(samples),
        ci_low=percentile(means, alpha * 100),
        ci_high=percentile(means, (1 - alpha) * 100),
        ci_level=ci_level,
        p5=percentile(data_sorted, 5),
        worst=data_sorted[0],
    )
