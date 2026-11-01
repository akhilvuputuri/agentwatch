"""Simulated traffic for the drift monitor's rule, with no store: the rule and the comparison are
the ones the monitor runs, and only the traces are made up.

`uv run python -m tests.drift_simulation 7 correlated 20 10` gives figures like those in
docs/design.md.
"""

from __future__ import annotations

import bisect
import random
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from itertools import repeat

from agentwatch import drift, monitors
from agentwatch.drift import Records, TraceRecord

HOUR = 3600 * 10**9
DAY = 24 * HOUR
MONTH_DAYS = 30


@dataclass(frozen=True, slots=True)
class Traffic:
    """5 to 20 traces a day, each of 1 to 8 calls, at a base error rate of `rate`.

    With `correlated`, failures bunch up within traces, as retries of one problem do: see
    `_trace_rate`.
    """

    seed: int
    days: int
    rate: float = 0.1
    shifted_rate: float | None = None
    shift_day: int = 0
    correlated: bool = False


def _trace_rate(rng: random.Random, rate: float, correlated: bool) -> float:
    """A trace's own failure rate, averaging `rate`."""
    if not correlated:
        return rate
    # One trace in five is in trouble and fails at four times the rate; the rest at a quarter.
    return min(1.0, rate * 4) if rng.random() < 0.2 else rate * 0.25


def generate(traffic: Traffic) -> tuple[list[int], list[TraceRecord]]:
    """Trace start times in order, and their records (IDs ordered as the times are)."""
    rng = random.Random(traffic.seed)
    times: list[int] = []
    for day in range(traffic.days):
        for _ in range(rng.randint(5, 20)):
            times.append(day * DAY + rng.randrange(DAY))
    times.sort()
    found: list[TraceRecord] = []
    for n, at in enumerate(times):
        shifted = traffic.shifted_rate is not None and at >= traffic.shift_day * DAY
        base = traffic.shifted_rate if shifted and traffic.shifted_rate else traffic.rate
        rate = _trace_rate(rng, base, traffic.correlated)
        calls = rng.randint(1, 8)
        failures = sum(rng.random() < rate for _ in range(calls))
        found.append(TraceRecord(f"{n:032x}", failures, calls))
    return times, found


def monitor(window_days: int, threshold: float = 0.05) -> monitors.Monitor:
    return monitors.Monitor(
        id="simulated",
        name="simulated",
        metric=drift.MODEL_ERROR_RATE,
        score_name=None,
        service=None,
        window_minutes=window_days * 24 * 60,
        comparison=monitors.ABOVE,
        threshold=threshold,
        min_samples=3,
        cooldown_minutes=60,
        enabled=True,
        created_ns=0,
        mode=monitors.DRIFT,
        baseline_minutes=window_days * 24 * 60,
    )


def firings(traffic: Traffic, watched: monitors.Monitor, *, first_day: int = 0) -> list[int]:
    """The times (ns) the monitor started firing, evaluating every hour from `first_day`."""
    times, found = generate(traffic)
    window = watched.window_minutes * monitors.MINUTE_NS
    state, breaches = monitors.OK, 0
    good_since: int | None = None
    last: int | None = None
    fired: list[int] = []
    for now in range(first_day * DAY, traffic.days * DAY, monitors.DRIFT_INTERVAL_NS):
        # The monitor's windows: start included, end excluded.
        start = bisect.bisect_left(times, now - 2 * window)
        split = bisect.bisect_left(times, now - window)
        end = bisect.bisect_left(times, now)
        result = drift.compare_records(
            watched.metric,
            Records(tuple(found[start:split]), proportion=True),
            Records(tuple(found[split:end]), proportion=True),
            confidence=monitors.DRIFT_CONFIDENCE,
            resamples=monitors.DRIFT_RESAMPLES,
        )
        following, breaches, good_since = monitors.next_drift_state(
            watched, result, state, breaches, good_since, last, now
        )
        if following == monitors.FIRING and state == monitors.OK:
            fired.append(now)
        state, last = following, now
    return fired


def false_alarms(seed: int, window_days: int, correlated: bool) -> int:
    """Firings in one 30-day month of hourly evaluations, with no change in the traffic."""
    days = MONTH_DAYS + 2 * window_days
    traffic = Traffic(seed=seed, days=days, correlated=correlated)
    # Started once both windows can hold traffic, so it is a full month of evaluations.
    return len(firings(traffic, monitor(window_days), first_day=2 * window_days))


def detection_delay(seed: int, window_days: int, correlated: bool) -> float | None:
    """Hours from a shift from 10% to 40% to the monitor firing, or None if it did not."""
    shift_day = 2 * window_days + 1
    traffic = Traffic(
        seed=seed,
        days=shift_day + 2 * window_days,
        shifted_rate=0.4,
        shift_day=shift_day,
        correlated=correlated,
    )
    fired = firings(traffic, monitor(window_days), first_day=shift_day)
    return (fired[0] - shift_day * DAY) / HOUR if fired else None


def main() -> None:
    """Arguments: window in days, "correlated" or "independent", months, shifts."""
    window_days, correlated = int(sys.argv[1]), sys.argv[2] == "correlated"
    months, shifts = int(sys.argv[3]), int(sys.argv[4])
    with ProcessPoolExecutor() as pool:
        counts = list(
            pool.map(false_alarms, range(months), repeat(window_days), repeat(correlated))
        )
        delays = list(
            pool.map(
                detection_delay,
                range(1000, 1000 + shifts),
                repeat(window_days),
                repeat(correlated),
            )
        )
    seen = [delay for delay in delays if delay is not None]
    print(
        f"{window_days}-day windows, {sys.argv[2]}: "
        f"{sum(counts)} firings in {months} months with no change, "
        f"{sum(c == 0 for c in counts)} months with none; shift detected in {len(seen)} of "
        f"{shifts}, median {statistics.median(seen) if seen else 0:.0f} h, "
        f"slowest {max(seen, default=0):.0f} h"
    )


if __name__ == "__main__":
    main()
