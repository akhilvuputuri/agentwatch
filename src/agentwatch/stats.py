"""Paired statistics for comparing two experiment runs on the same dataset items.

Dependency-free and deterministic given a seed, so a comparison can be reproduced exactly.
Ported from Chief's evals/decisions/stats.py, which this replaces there. One deliberate change:
Wilson intervals use the exact 97.5th normal percentile (1.959964) rather than 1.96.

A comparison ends in a verdict, never a bare difference:

- `better` / `worse`: the difference is unlikely to be noise (exact McNemar test for pass/fail
  scores, a paired t interval excluding zero for numeric ones, from MIN_PAIRS pairs);
- `no detectable difference`: not significant, and the comparison could have detected a
  difference as large as the margin that matters (its minimum detectable effect is within it);
- `underpowered`: not significant, but too few pairs to rule out a difference that matters.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Hashable, Sequence
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


# Two-sided 95% critical values of Student's t for 1 to 30 degrees of freedom.
_T_975 = (
    12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
    2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
    2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
)  # fmt: skip


def t_975(df: int) -> float:
    """The 97.5th percentile of Student's t: exact table to 30 df, a series expansion above."""
    if df < 1:
        return math.inf
    if df <= len(_T_975):
        return _T_975[df - 1]
    z = Z_95
    return z + (z**3 + z) / (4 * df) + (5 * z**5 + 16 * z**3 + 3 * z) / (96 * df**2)


def wilson(successes: int, total: int, z: float = Z_95) -> tuple[float, float] | None:
    """95% Wilson score interval for a proportion; stable at 0 and 1. None for no trials."""
    if total <= 0:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    # At 0 or total successes the bound is exactly 0 or 1; computed, it can be off by 1e-17.
    low = 0.0 if successes == 0 else max(0.0, centre - half)
    high = 1.0 if successes == total else min(1.0, centre + half)
    return (low, high)


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


def percentile(values: Sequence[float], share: float) -> float | None:
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(len(ordered) * share)) - 1]


def cluster_bootstrap[T](
    items: Sequence[T],
    cluster: Callable[[T], Hashable],
    statistic: Callable[[Sequence[T]], float | None],
    *,
    resamples: int = 4000,
    seed: int = 7,
) -> tuple[float, float] | None:
    """95% percentile bootstrap that resamples whole clusters.

    Calls of the same case, and cases of the same conversation, are not independent, so
    resampling clusters (not single items) keeps the interval honest. A resample whose
    statistic is undefined (for example, no positives) is skipped.
    """
    groups: dict[Hashable, list[T]] = {}
    for item in items:
        groups.setdefault(cluster(item), []).append(item)
    keys = list(groups)
    if not keys:
        return None
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(resamples):
        sample = [x for _ in keys for x in groups[keys[rng.randrange(len(keys))]]]
        value = statistic(sample)
        if value is not None:
            draws.append(value)
    if not draws:
        return None
    draws.sort()
    return (draws[int(0.025 * len(draws))], draws[max(0, int(0.975 * len(draws)) - 1)])


def cluster_interval[T](
    items: Sequence[T],
    cluster: Callable[[T], Hashable],
    statistic: Callable[[Sequence[T]], float | None],
    *,
    proportion: bool = True,
) -> tuple[float, float] | None:
    """Cluster bootstrap interval, or a Wilson interval over clusters when it is degenerate.

    With no failures (or no successes) every resample gives the same value, so the bootstrap
    would claim certainty. A Wilson interval with n = number of clusters is used instead,
    treating each cluster as one independent observation. Only for proportions.
    """
    interval = cluster_bootstrap(items, cluster, statistic)
    value = statistic(list(items))
    if not proportion or interval is None or value is None or interval[0] != interval[1]:
        return interval
    n = len({cluster(x) for x in items})
    return wilson(round(value * n), n)


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
    margin: float
    # True when the difference is significant but smaller than the margin that matters.
    below_margin: bool
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
    mde = _binary_minimum_detectable(only_a + only_b, n)
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
        margin=margin,
        below_margin=p < ALPHA and n > 0 and abs(rate_b - rate_a) < margin,
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
    below_margin: bool
    verdict: str


def compare_numeric(
    pairs: Sequence[tuple[float, float]],
    *,
    margin: float | None = None,
    relative_margin: float = 0.05,
    margin_floor: float = 0.01,
    higher_is_better: bool = True,
) -> NumericComparison:
    """Compare paired numbers with a paired t interval for the mean difference.

    `margin` is the smallest mean change that matters; by default `relative_margin` of the
    baseline's mean magnitude, but never below `margin_floor`. Below MIN_PAIRS pairs no
    verdict other than underpowered is given: the interval is not trustworthy that small.
    """
    n = len(pairs)
    diffs = [b - a for a, b in pairs]
    mean_a = mean([a for a, _ in pairs])
    mean_b = mean([b for _, b in pairs])
    if margin is None:
        margin = max(relative_margin * abs(mean_a) if n else 0.0, margin_floor)
    difference = mean(diffs)
    interval = _t_interval(diffs)
    significant = n >= MIN_PAIRS and interval is not None and (interval[0] > 0 or interval[1] < 0)
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
        below_margin=significant and abs(difference) < margin,
        verdict=_verdict(
            significant=significant, direction=direction, mde=mde, margin=margin, pairs=n
        ),
    )


def _t_interval(diffs: Sequence[float]) -> tuple[float, float] | None:
    """95% paired t interval for the mean difference; None below two pairs."""
    n = len(diffs)
    if n < 2:
        return None
    centre = mean(diffs)
    half = t_975(n - 1) * stdev(diffs) / math.sqrt(n)
    return (centre - half, centre + half)


def _binary_minimum_detectable(discordant: int, n: int) -> float | None:
    """Smallest true change in pass rate detected 80% of the time (5% level).

    The variance of a paired pass/fail difference is at most the share of discordant pairs,
    so this uses the upper Wilson bound of that share rather than the observed spread: with no
    flips observed, it still reports what the comparison could have missed.
    """
    bounds = wilson(discordant, n)
    if bounds is None:
        return None
    return Z_POWER * math.sqrt(bounds[1]) / math.sqrt(n)


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
