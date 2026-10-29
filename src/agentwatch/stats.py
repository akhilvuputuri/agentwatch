"""Paired statistics for comparing two experiment runs on the same dataset items.

Dependency-free and deterministic given a seed, so a comparison can be reproduced exactly.
Ported from Chief's evals/decisions/stats.py, which this replaces there.

A comparison ends in a verdict, never a bare difference:

- `better` / `worse`: the difference is unlikely to be noise (exact McNemar test for pass/fail
  scores, a paired bootstrap interval excluding zero for numeric ones);
- `no detectable difference`: not significant, and the comparison could have detected a
  difference as large as the margin that matters (its minimum detectable effect is within it);
- `underpowered`: not significant, but too few pairs to rule out a difference that matters.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass

BETTER = "better"
WORSE = "worse"
NO_DIFFERENCE = "no detectable difference"
UNDERPOWERED = "underpowered"

ALPHA = 0.05
# Below this many pairs, a non-significant result is always reported as underpowered: a
# standard deviation from so few pairs is too unreliable to claim there is no difference.
MIN_PAIRS = 10
Z_95 = 1.959963984540054
# z for 80% power at a two-sided 5% level: 1.96 + 0.84.
Z_POWER = Z_95 + 0.8416212335729143


def wilson(successes: int, total: int, z: float = Z_95) -> tuple[float, float] | None:
    """95% Wilson score interval for a proportion; stable at 0 and 1. None for no trials."""
    if total <= 0:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value.

    b: pairs only the first run passed; c: pairs only the second passed. Pairs both runs
    passed or failed carry no information about the difference, so they are ignored.
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def bootstrap(
    values: Sequence[float],
    statistic: Callable[[Sequence[float]], float],
    *,
    resamples: int = 4000,
    seed: int = 7,
) -> tuple[float, float] | None:
    """95% percentile bootstrap interval for statistic(values); None for no values."""
    if not values:
        return None
    rng = random.Random(seed)
    n = len(values)
    draws = sorted(
        statistic([values[rng.randrange(n)] for _ in range(n)]) for _ in range(resamples)
    )
    return (draws[int(0.025 * resamples)], draws[int(0.975 * resamples) - 1])


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else math.nan


def stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return math.nan
    centre = mean(values)
    return math.sqrt(sum((v - centre) ** 2 for v in values) / (len(values) - 1))


@dataclass(frozen=True, slots=True)
class BinaryComparison:
    """Pass/fail scores on paired items: baseline A against candidate B."""

    pairs: int
    both: int
    only_baseline: int  # b: A passed, B failed (regressions)
    only_candidate: int  # c: B passed, A failed (improvements)
    neither: int
    baseline_rate: float
    candidate_rate: float
    baseline_interval: tuple[float, float] | None
    candidate_interval: tuple[float, float] | None
    difference: float
    difference_interval: tuple[float, float] | None
    p_value: float
    minimum_detectable: float | None
    verdict: str


def compare_binary(
    pairs: Sequence[tuple[bool, bool]], *, margin: float = 0.05, seed: int = 7
) -> BinaryComparison:
    """Compare paired pass/fail outcomes with an exact McNemar test.

    `margin` is the smallest change in pass rate that matters (absolute, 0.05 = 5 points).
    """
    n = len(pairs)
    both = sum(1 for a, b in pairs if a and b)
    only_a = sum(1 for a, b in pairs if a and not b)
    only_b = sum(1 for a, b in pairs if b and not a)
    neither = n - both - only_a - only_b
    rate_a = (both + only_a) / n if n else math.nan
    rate_b = (both + only_b) / n if n else math.nan
    diffs = [float(b) - float(a) for a, b in pairs]
    p = mcnemar_exact(only_a, only_b)
    mde = _minimum_detectable(diffs)
    return BinaryComparison(
        pairs=n,
        both=both,
        only_baseline=only_a,
        only_candidate=only_b,
        neither=neither,
        baseline_rate=rate_a,
        candidate_rate=rate_b,
        baseline_interval=wilson(both + only_a, n),
        candidate_interval=wilson(both + only_b, n),
        difference=rate_b - rate_a if n else math.nan,
        difference_interval=bootstrap(diffs, mean, seed=seed),
        p_value=p,
        minimum_detectable=mde,
        verdict=_verdict(
            significant=p < ALPHA, direction=only_b - only_a, mde=mde, margin=margin, pairs=n
        ),
    )


@dataclass(frozen=True, slots=True)
class NumericComparison:
    """Numeric scores on paired items: candidate B minus baseline A."""

    pairs: int
    baseline_mean: float
    candidate_mean: float
    difference: float
    difference_interval: tuple[float, float] | None
    improved: int
    regressed: int
    unchanged: int
    minimum_detectable: float | None
    margin: float
    verdict: str


def compare_numeric(
    pairs: Sequence[tuple[float, float]],
    *,
    margin: float | None = None,
    higher_is_better: bool = True,
    seed: int = 7,
) -> NumericComparison:
    """Compare paired numbers with a seeded bootstrap interval for the mean difference.

    `margin` is the smallest mean change that matters; by default 5% of the baseline's mean
    magnitude (or 0.05 when that is zero).
    """
    n = len(pairs)
    diffs = [b - a for a, b in pairs]
    mean_a = mean([a for a, _ in pairs])
    mean_b = mean([b for _, b in pairs])
    if margin is None:
        margin = 0.05 * abs(mean_a) if n and mean_a else 0.05
    interval = bootstrap(diffs, mean, seed=seed)
    significant = interval is not None and (interval[0] > 0 or interval[1] < 0)
    difference = mean(diffs)
    mde = _minimum_detectable(diffs)
    direction = difference if higher_is_better else -difference
    return NumericComparison(
        pairs=n,
        baseline_mean=mean_a,
        candidate_mean=mean_b,
        difference=difference,
        difference_interval=interval,
        improved=sum(1 for d in diffs if (d > 0) == higher_is_better and d != 0),
        regressed=sum(1 for d in diffs if (d < 0) == higher_is_better and d != 0),
        unchanged=sum(1 for d in diffs if d == 0),
        minimum_detectable=mde,
        margin=margin,
        verdict=_verdict(
            significant=significant, direction=direction, mde=mde, margin=margin, pairs=n
        ),
    )


def _minimum_detectable(diffs: Sequence[float]) -> float | None:
    """Smallest true mean difference this many pairs would detect 80% of the time (5% level).

    From the paired differences' standard deviation: (z_0.975 + z_0.8) * sd / sqrt(n). With
    no variation at all (every pair identical) the standard deviation is zero, and any real
    difference would have shown, so the detectable effect is zero.
    """
    n = len(diffs)
    if n < 2:
        return None
    sd = stdev(diffs)
    return Z_POWER * sd / math.sqrt(n)


def _verdict(
    *, significant: bool, direction: float, mde: float | None, margin: float, pairs: int
) -> str:
    if significant and direction > 0:
        return BETTER
    if significant and direction < 0:
        return WORSE
    if pairs >= MIN_PAIRS and mde is not None and mde <= margin:
        return NO_DIFFERENCE
    return UNDERPOWERED
