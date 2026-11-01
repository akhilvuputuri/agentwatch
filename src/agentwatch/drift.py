"""Drift: did a metric move between two time windows?

Two windows hold different traffic, so nothing pairs up and the paired comparisons in `stats` do
not apply. Calls within one trace are not independent either: a retry loop can fail fifty times
for one reason. So a comparison resamples whole traces, independently in each window, and takes
the percentile interval of (current - baseline).

The verdict says only what the data can support:

- `underpowered`: a window has fewer than MIN_TRACES traces, so nothing is claimed;
- `changed`: the interval excludes zero;
- `no detectable difference`: it does not. `detectable`, the interval's half-width, says how small
  a change this data could have shown, so this is not read as "no change".

It never says better or worse, since only the caller knows which way is bad, and it does not say
why: a window can differ from the one before for reasons no deploy caused, such as the day of the
week. The same seed and data give the same result.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import chain
from operator import attrgetter
from typing import Any

from agentwatch.metrics import (
    MAX_CALLS_PER_TRACE,
    Window,
    call_rows,
    cost_rows,
    score_names,
    score_rows,
)
from agentwatch.stats import (
    CHANGED,
    NO_DIFFERENCE,
    UNDERPOWERED,
    newcombe,
    quantile,
    unpaired_cluster_bootstrap,
    z_for,
)
from agentwatch.store import Store

MODEL_ERROR_RATE = "model_error_rate"
TOOL_ERROR_RATE = "tool_error_rate"
MODEL_MEAN_MS = "model_mean_ms"
MODEL_P95_MS = "model_p95_ms"
COST_PER_TRACE = "cost_per_trace"
SCORE_MEAN = "score_mean"
METRICS = (
    MODEL_ERROR_RATE,
    TOOL_ERROR_RATE,
    MODEL_MEAN_MS,
    MODEL_P95_MS,
    COST_PER_TRACE,
    SCORE_MEAN,
)
# Everything a page compares when it asks about all metrics; scores are added by name.
CALL_METRICS = METRICS[:-1]
# Metrics that a model filter can narrow. A tool call names no model, nor does a score.
MODEL_METRICS = (MODEL_ERROR_RATE, MODEL_MEAN_MS, MODEL_P95_MS, COST_PER_TRACE)

HIGHER = "higher"
LOWER = "lower"
NONE = "none"

MIN_TRACES = 10
# The most recent traces kept per window, which bounds both memory and the bootstrap's work.
MAX_TRACES = 20_000
# A percentile sorts every resample, so its windows are bounded more tightly.
MAX_QUANTILE_TRACES = 5_000
MAX_QUANTILE_SAMPLES = 20_000
RESAMPLES = 1000
SEED = 7
MAX_SCORE_NAMES = 50


class DriftError(ValueError):
    """A comparison that cannot be made, with the reason."""


@dataclass(frozen=True, slots=True)
class TraceRecord:
    """One trace's part of a metric: failures / calls, total / count, or call durations."""

    trace_id: str
    numerator: float
    # The samples this trace contributes.
    denominator: int
    # Only for a percentile, which cannot be built from a sum.
    values: tuple[float, ...] = ()


@dataclass(frozen=True, slots=True)
class Records:
    """A window's traces in a stable order, so a seed always draws the same ones."""

    traces: tuple[TraceRecord, ...]
    # Older traces were left out, or a trace's oldest calls.
    truncated: bool = False
    # Whether the metric is a proportion: an error rate, or a score that is all BOOLEAN.
    proportion: bool = False


@dataclass(frozen=True, slots=True)
class Comparison:
    metric: str
    score_name: str | None
    baseline_value: float | None
    current_value: float | None
    difference: float | None
    # (low, high) of current - baseline; None when underpowered.
    interval: tuple[float, float] | None
    baseline_traces: int
    current_traces: int
    baseline_samples: int
    current_samples: int
    # Which way it moved, when it did: "none" unless the verdict is "changed".
    direction: str
    verdict: str
    # The interval's half-width: the smallest change this data could have shown.
    detectable: float | None
    truncated: bool
    # Whether the values are proportions, so a page can show them as percentages.
    proportion: bool
    confidence: float


def records(
    store: Store,
    metric: str,
    window: Window,
    *,
    model: str | None = None,
    score_name: str | None = None,
) -> Records:
    """Per-trace records by the dashboard's populations: call metrics cover calls that started in
    the window, cost covers traces whose root started in it (those with a known cost), and scores
    those recorded in it, on traces in scope."""
    if metric in (MODEL_ERROR_RATE, TOOL_ERROR_RATE):
        rows, truncated = call_rows(
            store, window, tools=metric == TOOL_ERROR_RATE, model=model, limit=MAX_TRACES
        )
        found = [TraceRecord(r["trace_id"], r["failures"], r["calls"]) for r in rows]
        return Records(_ordered(found), truncated, proportion=True)
    if metric == MODEL_MEAN_MS:
        rows, truncated = call_rows(store, window, tools=False, model=model, limit=MAX_TRACES)
        found = [TraceRecord(r["trace_id"], r["total_ms"], r["calls"]) for r in rows]
        return Records(_ordered(found), truncated)
    if metric == MODEL_P95_MS:
        rows, truncated = call_rows(
            store, window, tools=False, model=model, limit=MAX_QUANTILE_TRACES, durations=True
        )
        return _durations(rows, truncated)
    if metric == COST_PER_TRACE:
        rows, truncated = cost_rows(store, window, model=model, limit=MAX_TRACES)
        found = [TraceRecord(r["trace_id"], r["cost_usd"], 1) for r in rows]
        return Records(_ordered(found), truncated)
    if metric == SCORE_MEAN:
        rows, truncated = score_rows(store, window, score_name or "", limit=MAX_TRACES)
        found = [TraceRecord(r["trace_id"], r["total"], r["samples"]) for r in rows]
        return Records(_ordered(found), truncated, proportion=all(r["booleans"] for r in rows))
    raise DriftError(f"metric must be one of {', '.join(METRICS)}")


def _durations(rows: list[dict[str, Any]], truncated: bool) -> Records:
    """Newest traces first, up to MAX_QUANTILE_SAMPLES calls."""
    found: list[TraceRecord] = []
    samples = 0
    for row in rows:
        values = tuple(float(value) for value in row["durations"])
        truncated = truncated or row["calls"] > MAX_CALLS_PER_TRACE
        if samples + len(values) > MAX_QUANTILE_SAMPLES:
            truncated = True
            break
        samples += len(values)
        found.append(TraceRecord(row["trace_id"], 0.0, len(values), values))
    return Records(_ordered(found), truncated)


def _ordered(found: Sequence[TraceRecord]) -> tuple[TraceRecord, ...]:
    usable = [r for r in found if math.isfinite(r.numerator) and r.denominator > 0]
    return tuple(sorted(usable, key=lambda r: r.trace_id))


_numerator = attrgetter("numerator")
_denominator = attrgetter("denominator")
_values = attrgetter("values")


def _ratio(sample: Sequence[TraceRecord]) -> float | None:
    """The pooled value: total failures / total calls, not an average of each trace's rate."""
    samples: int = sum(map(_denominator, sample))
    return sum(map(_numerator, sample)) / samples if samples else None


def _p95(sample: Sequence[TraceRecord]) -> float | None:
    return quantile(sorted(chain.from_iterable(map(_values, sample))), 0.95)


def compare_records(
    metric: str,
    baseline: Records,
    current: Records,
    *,
    score_name: str | None = None,
    confidence: float = 0.95,
    resamples: int = RESAMPLES,
    seed: int = SEED,
) -> Comparison:
    """The comparison of two windows' records. Pure CPU: call it outside the store's lock."""
    statistic = _p95 if metric == MODEL_P95_MS else _ratio
    before, after = statistic(baseline.traces), statistic(current.traces)
    difference = None if before is None or after is None else after - before
    proportion = baseline.proportion and current.proportion
    interval = None
    if min(len(baseline.traces), len(current.traces)) >= MIN_TRACES:
        interval = unpaired_cluster_bootstrap(
            baseline.traces,
            current.traces,
            statistic,
            resamples=resamples,
            seed=seed,
            confidence=confidence,
        )
        if interval is not None and proportion and before is not None and after is not None:
            interval = _widened(interval, before, after, baseline, current, confidence)
    verdict, direction = UNDERPOWERED, NONE
    if interval is not None and difference is not None:
        verdict = CHANGED if interval[0] > 0 or interval[1] < 0 else NO_DIFFERENCE
        if verdict == CHANGED:
            direction = HIGHER if difference > 0 else LOWER
    return Comparison(
        metric=metric,
        score_name=score_name,
        baseline_value=before,
        current_value=after,
        difference=difference,
        interval=interval,
        baseline_traces=len(baseline.traces),
        current_traces=len(current.traces),
        baseline_samples=sum(map(_denominator, baseline.traces)),
        current_samples=sum(map(_denominator, current.traces)),
        direction=direction,
        verdict=verdict,
        detectable=None if interval is None else (interval[1] - interval[0]) / 2,
        truncated=baseline.truncated or current.truncated,
        proportion=proportion,
        confidence=confidence,
    )


def _widened(
    interval: tuple[float, float],
    before: float,
    after: float,
    baseline: Records,
    current: Records,
    confidence: float,
) -> tuple[float, float]:
    """A window at exactly 0 or 1 resamples to the same value every time, so the bootstrap sees no
    uncertainty on that side, and with both there it would claim to have ruled out any change. The
    interval then also covers a Wilson difference, counting each trace as one observation."""
    if not ({before, after} & {0.0, 1.0}):
        return interval
    n, m = len(baseline.traces), len(current.traces)
    low, high = newcombe(round(before * n), n, round(after * m), m, z_for(confidence))
    return (min(interval[0], low), max(interval[1], high))


def compare(
    store: Store,
    metric: str,
    *,
    baseline: tuple[int, int],
    current: tuple[int, int],
    service: str | None = None,
    model: str | None = None,
    score_name: str | None = None,
    confidence: float = 0.95,
    resamples: int = RESAMPLES,
    seed: int = SEED,
) -> Comparison:
    """Compares a metric over two windows, each (start, end) in nanoseconds.

    Each read takes the store's lock only for its own query; the bootstrap runs with no lock held.
    """
    if metric not in METRICS:
        raise DriftError(f"metric must be one of {', '.join(METRICS)}")
    if metric == SCORE_MEAN and not score_name:
        raise DriftError("score_name is required for score_mean")
    if metric != SCORE_MEAN:
        score_name = None
    if model and metric not in MODEL_METRICS:
        raise DriftError(f"a model filter applies only to {', '.join(MODEL_METRICS)}")
    before = records(store, metric, Window(*baseline, service), model=model, score_name=score_name)
    after = records(store, metric, Window(*current, service), model=model, score_name=score_name)
    return compare_records(
        metric,
        before,
        after,
        score_name=score_name,
        confidence=confidence,
        resamples=resamples,
        seed=seed,
    )


def summary(store: Store, *, service: str | None, days: int, now_ns: int) -> list[Comparison]:
    """Every call metric and score name: the last `days` days against the same length before."""
    length = days * 24 * 3600 * 1_000_000_000
    current = (now_ns - length, now_ns)
    baseline = (now_ns - 2 * length, now_ns - length)
    found = [
        compare(store, metric, baseline=baseline, current=current, service=service)
        for metric in CALL_METRICS
    ]
    names = score_names(store, Window(baseline[0], now_ns, service), limit=MAX_SCORE_NAMES)
    found.extend(
        compare(
            store, SCORE_MEAN, baseline=baseline, current=current, service=service, score_name=name
        )
        for name in names
    )
    return found
