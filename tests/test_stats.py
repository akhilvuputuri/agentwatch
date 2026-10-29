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
    # Many identical outcomes: nothing changed, and with enough pairs that is established.
    assert stats.compare_binary([(True, True)] * 150 + [(False, False)] * 150).verdict == (
        stats.NO_DIFFERENCE
    )
    # Ten agreeing pairs could not have detected a 5-point change: not "no difference".
    assert stats.compare_binary([(True, True)] * 10).verdict == stats.UNDERPOWERED
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
    # Below MIN_PAIRS, even consistent moves are not called.
    assert stats.compare_numeric([(0.5, 0.6)] * 3).verdict == stats.UNDERPOWERED
    assert stats.compare_numeric([(0.5, 0.6)]).difference_interval is None
    # Significant but smaller than the margin is flagged.
    tiny = [(1.0, 1.001 + 0.0001 * (k % 3)) for k in range(200)]
    result = stats.compare_numeric(tiny, margin=0.01)
    assert result.verdict == stats.BETTER
    assert result.below_margin
    # A near-zero baseline still gets a usable margin.
    assert stats.compare_numeric([(0.001, 0.001)] * 20).margin == 0.01


def test_results_are_reproducible() -> None:
    pairs = [
        (random.Random(i).random() < 0.5, random.Random(i + 99).random() < 0.6) for i in range(50)
    ]
    assert stats.compare_binary(pairs, seed=3) == stats.compare_binary(pairs, seed=3)


def test_t_quantiles() -> None:
    assert stats.t_975(1) == pytest.approx(12.706)
    assert stats.t_975(30) == pytest.approx(2.042)
    assert stats.t_975(60) == pytest.approx(2.000, abs=0.002)
    assert stats.t_975(1000) == pytest.approx(1.962, abs=0.002)


@pytest.mark.parametrize("n", [10, 20, 40])
def test_numeric_false_positives_stay_near_5_percent(n: int) -> None:
    rng = random.Random(10 + n)
    trials = 1500
    wrong = 0
    for _ in range(trials):
        pairs = [(x, x + rng.gauss(0, 1)) for x in (rng.gauss(0, 1) for _ in range(n))]
        wrong += stats.compare_numeric(pairs).verdict in (stats.BETTER, stats.WORSE)
    assert wrong / trials <= 0.07, (n, wrong / trials)


@pytest.mark.parametrize("n", [10, 20])
def test_a_real_regression_is_rarely_called_no_difference(n: int) -> None:
    rng = random.Random(20 + n)
    trials = 1000
    reassured = 0
    for _ in range(trials):
        # The candidate passes 10 points less often than the baseline.
        pairs = [(rng.random() < 0.8, rng.random() < 0.7) for _ in range(n)]
        reassured += stats.compare_binary(pairs).verdict == stats.NO_DIFFERENCE
    assert reassured / trials <= 0.02, (n, reassured / trials)
