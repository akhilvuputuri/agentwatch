"""Simulated traffic for the drift monitor's rule, with no store: the rule and the comparison are
the ones the monitor runs, and only the traces are made up.

`uv run python -m tests.drift_simulation 7 sessions 60 20` gives one row of the table in
docs/design.md, and `uv run python -m tests.drift_simulation calibration 2000` its calibration.
"""

from __future__ import annotations

import bisect
import math
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

# Failures independent within and between traces.
INDEPENDENT = "independent"
# One trace in five is in trouble and fails at four times the rate; the rest at a quarter.
CORRELATED = "correlated"
# The same, shared by a whole session of 1 to 7 neighbouring traces.
SESSIONS = "sessions"
# Each day's rate varies, lognormally with sigma 0.5 around the same mean.
OVERDISPERSED = "overdispersed"
# Weekdays at 0.8 times the rate and weekends at 1.5 times.
WEEKLY = "weekly"
# About one six-hour incident a fortnight, failing 60% of calls, then back to normal.
INCIDENTS = "incidents"
MODELS = (INDEPENDENT, CORRELATED, SESSIONS, OVERDISPERSED, WEEKLY, INCIDENTS)

_SIGMA = 0.5
_INCIDENT_PER_DAY = 1 / 15
_INCIDENT_NS = 6 * HOUR
_INCIDENT_RATE = 0.6


@dataclass(frozen=True, slots=True)
class Traffic:
    """5 to 20 traces a day, each of 1 to 8 calls, at a base error rate of `rate`."""

    seed: int
    days: int
    rate: float = 0.1
    shifted_rate: float | None = None
    shift_day: int = 0
    model: str = INDEPENDENT


def _trouble(rng: random.Random) -> float:
    return 4.0 if rng.random() < 0.2 else 0.25


def _scales(rng: random.Random, traffic: Traffic, times: list[int]) -> list[float]:
    """What each trace's rate is multiplied by, averaging one."""
    if traffic.model == CORRELATED:
        return [_trouble(rng) for _ in times]
    if traffic.model == SESSIONS:
        scales: list[float] = []
        while len(scales) < len(times):
            scales.extend([_trouble(rng)] * rng.randint(1, 7))
        return scales[: len(times)]
    if traffic.model == OVERDISPERSED:
        daily = [rng.lognormvariate(-(_SIGMA**2) / 2, _SIGMA) for _ in range(traffic.days)]
        return [daily[at // DAY] for at in times]
    if traffic.model == WEEKLY:
        return [1.5 if (at // DAY) % 7 >= 5 else 0.8 for at in times]
    return [1.0] * len(times)


def generate(traffic: Traffic) -> tuple[list[int], list[TraceRecord]]:
    """Trace start times in order, and their records (IDs ordered as the times are)."""
    rng = random.Random(traffic.seed)
    times: list[int] = []
    for day in range(traffic.days):
        for _ in range(rng.randint(5, 20)):
            times.append(day * DAY + rng.randrange(DAY))
    times.sort()
    incidents: list[int] = []
    if traffic.model == INCIDENTS:
        incidents = [
            day * DAY + rng.randrange(DAY)
            for day in range(traffic.days)
            if rng.random() < _INCIDENT_PER_DAY
        ]
    found: list[TraceRecord] = []
    for n, (at, scale) in enumerate(zip(times, _scales(rng, traffic, times), strict=True)):
        shifted = traffic.shifted_rate is not None and at >= traffic.shift_day * DAY
        base = traffic.shifted_rate if shifted and traffic.shifted_rate else traffic.rate
        rate = min(1.0, base * scale)
        if any(start <= at < start + _INCIDENT_NS for start in incidents):
            rate = _INCIDENT_RATE
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
        cooldown_minutes=window_days * 24 * 60,
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


def false_alarms(seed: int, window_days: int, model: str) -> int:
    """Firings in one 30-day month of hourly evaluations, with no lasting change in the traffic."""
    days = MONTH_DAYS + 2 * window_days
    traffic = Traffic(seed=seed, days=days, model=model)
    # Started once both windows can hold traffic, so it is a full month of evaluations.
    return len(firings(traffic, monitor(window_days), first_day=2 * window_days))


def detection_delay(seed: int, window_days: int, model: str) -> float | None:
    """Hours from a shift from 10% to 40% to the monitor firing, or None if it did not."""
    shift_day = 2 * window_days + 1
    traffic = Traffic(
        seed=seed,
        days=shift_day + 2 * window_days,
        shifted_rate=0.4,
        shift_day=shift_day,
        model=model,
    )
    fired = firings(traffic, monitor(window_days), first_day=shift_day)
    return (fired[0] - shift_day * DAY) / HOUR if fired else None


def _calls(rng: random.Random, n: int, kind: str) -> Records:
    """A window of `n` traces with nothing changed: a rate of 10% or lognormal durations."""
    found: list[TraceRecord] = []
    for i in range(n):
        calls = rng.randint(1, 8)
        if kind in (INDEPENDENT, CORRELATED):
            rate = 0.1 * (_trouble(rng) if kind == CORRELATED else 1.0)
            found.append(
                TraceRecord(f"{i:032x}", sum(rng.random() < rate for _ in range(calls)), calls)
            )
        else:
            values = tuple(math.exp(rng.gauss(6, 1)) for _ in range(calls))
            numerator = 0.0 if kind == drift.MODEL_P95_MS else sum(values)
            found.append(TraceRecord(f"{i:032x}", numerator, calls, values))
    return Records(tuple(found), proportion=kind in (INDEPENDENT, CORRELATED))


def false_changed(seed: int, n: int, kind: str) -> bool:
    """Whether one 95% comparison of two alike windows of `n` traces says "changed"."""
    rng = random.Random(seed)
    metric = kind if kind in (drift.MODEL_MEAN_MS, drift.MODEL_P95_MS) else drift.MODEL_ERROR_RATE
    result = drift.compare_records(metric, _calls(rng, n, kind), _calls(rng, n, kind), seed=seed)
    return result.verdict == drift.CHANGED


def _print_calibration(reps: int) -> None:
    with ProcessPoolExecutor() as pool:
        for kind in (INDEPENDENT, CORRELATED, drift.MODEL_MEAN_MS, drift.MODEL_P95_MS):
            shares: list[str] = []
            for n in (10, 20, 30, 100):
                hits = sum(pool.map(false_changed, range(reps), repeat(n), repeat(kind)))
                shares.append(f"n={n}: {hits / reps:.1%}")
            print(f"{kind}: {', '.join(shares)} false changed at a nominal 5%", flush=True)


def main() -> None:
    """Arguments: window in days, traffic model, months, shifts; or "calibration" and repeats."""
    if sys.argv[1] == "calibration":
        _print_calibration(int(sys.argv[2]))
        return
    window_days, model = int(sys.argv[1]), sys.argv[2]
    months, shifts = int(sys.argv[3]), int(sys.argv[4])
    with ProcessPoolExecutor() as pool:
        counts = list(pool.map(false_alarms, range(months), repeat(window_days), repeat(model)))
        delays = list(
            pool.map(
                detection_delay, range(1000, 1000 + shifts), repeat(window_days), repeat(model)
            )
        )
    seen = [delay for delay in delays if delay is not None]
    print(
        f"{window_days}-day windows, {model}: "
        f"{sum(counts) / months:.2f} firings a month over {months} months with no lasting change, "
        f"{sum(c == 0 for c in counts)} months with none; shift detected in {len(seen)} of "
        f"{shifts}, median {statistics.median(seen) if seen else 0:.0f} h, "
        f"slowest {max(seen, default=0):.0f} h"
    )


if __name__ == "__main__":
    main()
