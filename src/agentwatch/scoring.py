"""Per-call scoring helpers for decision-style evals: one model call per case, repeated runs.

Ported from Chief's evals/decisions/scoring.py. A record is one call:
`{"id": case, "run": n, "error": None | str, "latency_ms": float, "cost": float | None, ...}`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agentwatch.stats import mean, percentile

Record = dict[str, Any]


def call_stats(records: list[Record], held_ids: set[str], paid: bool) -> dict[str, Any]:
    """Calls, errors, latency (mean, p50, p95) and cost per decision over the held-out cases.

    Every call takes wall time, failures and timeouts included. The cost per decision is the
    mean reported cost; for a paid backend it is unknown (None) when any call reported none.
    """
    calls = [r for r in records if r["id"] in held_ids]
    ok = [r for r in calls if r["error"] is None]
    latencies = [
        r["latency_ms"]
        for r in calls
        if isinstance(r.get("latency_ms"), int | float)
        and not isinstance(r.get("latency_ms"), bool)
    ]
    costs = [
        r["cost"]
        for r in calls
        if isinstance(r.get("cost"), int | float) and not isinstance(r.get("cost"), bool)
    ]
    unpriced = len(calls) - len(costs)
    if costs and not (paid and unpriced):
        cost: float | None = sum(costs) / len(costs)
    else:
        cost = None if paid else 0.0
    return {
        "calls": len(calls),
        "errors": len(calls) - len(ok),
        "calls_without_cost": unpriced if paid else 0,
        "latency_mean_ms": round(mean(latencies)) if latencies else 0,
        "latency_p50_ms": percentile(latencies, 0.5) or 0,
        "latency_p95_ms": percentile(latencies, 0.95) or 0,
        "cost_per_decision_usd": cost,
    }


def majority(per_case: dict[str, list[bool]]) -> dict[str, bool]:
    """A case counts as right when most of its calls are right (ties count as wrong)."""
    return {k: sum(v) * 2 > len(v) for k, v in per_case.items()}


def by_run(
    calls: list[Record], metric: Callable[[list[Record]], float | None]
) -> list[float | None]:
    """The metric computed separately for each run, in run order."""
    runs = sorted({c["run"] for c in calls})
    return [metric([c for c in calls if c["run"] == r]) for r in runs]
