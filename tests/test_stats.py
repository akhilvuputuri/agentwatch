"""Statistics checks, including seeded simulations of interval coverage and test error rates."""

from __future__ import annotations

import random

import pytest

from agentwatch import stats


def test_wilson_basics() -> None:
    assert stats.wilson(0, 0) is None
    low, high = stats.wilson(0, 10) or (1, 1)
    assert low == 0.0
    assert 0.25 < high < 0.35
    low, high = stats.wilson(10, 10) or (0, 0)
    assert high == pytest.approx(1.0)


def test_mcnemar_exact_values() -> None:
    assert stats.mcnemar_exact(0, 0) == 1.0
    # All 6 discordant pairs one way: 2 * (1/2)^6.
    assert stats.mcnemar_exact(0, 6) == pytest.approx(2 / 64)
    assert stats.mcnemar_exact(3, 3) == 1.0
    assert stats.mcnemar_exact(1, 9) == stats.mcnemar_exact(9, 1)


def test_wilson_covers_the_true_rate_about_95_percent_of_the_time() -> None:
    rng = random.Random(1)
    for rate, n in ((0.5, 40), (0.9, 60), (0.1, 100)):
        covered = 0
        trials = 2000
        for _ in range(trials):
            successes = sum(rng.random() < rate for _ in range(n))
            low, high = stats.wilson(successes, n) or (0.0, 0.0)
            covered += low <= rate <= high
        assert 0.92 <= covered / trials <= 0.98, (rate, n, covered / trials)


def test_mcnemar_rejects_about_5_percent_or_less_when_runs_do_not_differ() -> None:
    rng = random.Random(2)
    trials = 2000
    rejections = 0
    for _ in range(trials):
        # Both runs pass each item with the same item-specific probability: no real difference.
        pairs: list[tuple[bool, bool]] = []
        for _ in range(60):
            p = rng.random()
            pairs.append((rng.random() < p, rng.random() < p))
        b = sum(1 for x, y in pairs if x and not y)
        c = sum(1 for x, y in pairs if y and not x)
        rejections += stats.mcnemar_exact(b, c) < stats.ALPHA
    # The exact test is conservative: at most about 5%.
    assert rejections / trials <= 0.06


def test_mcnemar_detects_a_real_improvement() -> None:
    rng = random.Random(3)
    detected = 0
    trials = 500
    for _ in range(trials):
        pairs = [(rng.random() < 0.6, rng.random() < 0.85) for _ in range(120)]
        result = stats.compare_binary(pairs)
        detected += result.verdict == stats.BETTER
    assert detected / trials > 0.9


def test_bootstrap_interval_covers_the_mean_about_95_percent_of_the_time() -> None:
    rng = random.Random(4)
    trials = 300
    covered = 0
    for seed in range(trials):
        values = [rng.gauss(1.0, 2.0) for _ in range(80)]
        low, high = stats.bootstrap(values, stats.mean, resamples=1000, seed=seed) or (0.0, 0.0)
        covered += low <= 1.0 <= high
    assert 0.90 <= covered / trials <= 0.98


def test_verdicts() -> None:
    # Many identical outcomes: nothing changed, and that is well established.
    same = [(True, True)] * 50 + [(False, False)] * 50
    assert stats.compare_binary(same).verdict == stats.NO_DIFFERENCE
    # Too few pairs: never "no difference", whatever they show.
    assert stats.compare_binary([(True, True)] * 5).verdict == stats.UNDERPOWERED
    # Balanced flips on a small set: inconclusive.
    mixed = [(True, False), (False, True)] * 6 + [(True, True)] * 4
    assert stats.compare_binary(mixed).verdict == stats.UNDERPOWERED
    worse = [(True, False)] * 12 + [(True, True)] * 20
    result = stats.compare_binary(worse)
    assert result.verdict == stats.WORSE
    assert (result.only_baseline, result.only_candidate) == (12, 0)
    assert result.difference == pytest.approx(-12 / 32)


def test_numeric_comparisons() -> None:
    rng = random.Random(5)
    better = [(x, x + 0.5 + rng.gauss(0, 0.1)) for x in (rng.uniform(0, 1) for _ in range(40))]
    assert stats.compare_numeric(better).verdict == stats.BETTER
    slower = [(100.0, 150.0 + rng.gauss(0, 5)) for _ in range(30)]
    result = stats.compare_numeric(slower, higher_is_better=False)
    assert result.verdict == stats.WORSE
    assert result.regressed == 30
    steady = [(1.0, 1.0)] * 30
    assert stats.compare_numeric(steady).verdict == stats.NO_DIFFERENCE


def test_results_are_reproducible() -> None:
    pairs = [
        (random.Random(i).random() < 0.5, random.Random(i + 99).random() < 0.6) for i in range(50)
    ]
    assert stats.compare_binary(pairs, seed=3) == stats.compare_binary(pairs, seed=3)
