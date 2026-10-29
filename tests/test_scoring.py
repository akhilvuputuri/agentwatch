from __future__ import annotations

from agentwatch import scoring


def test_call_stats() -> None:
    records = [
        {"id": "a", "run": 0, "error": None, "latency_ms": 100, "cost": 0.001},
        {"id": "b", "run": 0, "error": "timeout", "latency_ms": 6000, "cost": None},
        {"id": "c", "run": 0, "error": None, "latency_ms": 200, "cost": 0.003},
        {"id": "x", "run": 0, "error": None, "latency_ms": 1, "cost": 9.0},  # not held out
    ]
    paid = scoring.call_stats(records, {"a", "b", "c"}, paid=True)
    assert (paid["calls"], paid["errors"], paid["calls_without_cost"]) == (3, 1, 1)
    assert paid["latency_p50_ms"] == 200
    assert paid["latency_p95_ms"] == 6000
    assert paid["cost_per_decision_usd"] is None  # a paid call reported no cost
    free = scoring.call_stats(records, {"a", "c"}, paid=False)
    assert free["cost_per_decision_usd"] == 0.002
    assert scoring.call_stats([], set(), paid=False)["cost_per_decision_usd"] == 0.0


def test_majority_and_by_run() -> None:
    assert scoring.majority({"a": [True, True, False], "b": [True, False]}) == {
        "a": True,
        "b": False,
    }
    calls = [{"run": 1, "v": 2.0}, {"run": 0, "v": 1.0}, {"run": 1, "v": 4.0}]
    assert scoring.by_run(calls, lambda cs: sum(c["v"] for c in cs) / len(cs)) == [1.0, 3.0]
