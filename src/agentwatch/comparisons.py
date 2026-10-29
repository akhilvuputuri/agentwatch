"""Compare two experiment runs on the same dataset, item by item, with paired statistics.

Items are paired by dataset item: only items both runs recorded are compared. For each score
name both runs have, the comparison is binary (boolean scores, as pass/fail) or numeric; an
item whose task failed counts as failing every binary score the other run's item has, so a
crash is never hidden by a missing score. Task success itself and latency are compared too.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from agentwatch import experiments, stats
from agentwatch.datasets import DatasetError
from agentwatch.store import Store

LATENCY = "latency"
SUCCESS = "task succeeded"


def compare_runs(
    store: Store, baseline_id: str, candidate_id: str, *, margin: float = 0.05, seed: int = 7
) -> dict[str, Any]:
    baseline = experiments.get_run(store, baseline_id)
    candidate = experiments.get_run(store, candidate_id)
    if baseline.dataset_id != candidate.dataset_id:
        raise DatasetError("both runs must be on the same dataset")
    if baseline.id == candidate.id:
        raise DatasetError("choose two different runs")
    a_items = {item["item_id"]: item for item in experiments.run_items(store, baseline.id)}
    b_items = {item["item_id"]: item for item in experiments.run_items(store, candidate.id)}
    paired = sorted(set(a_items) & set(b_items))

    def values(item: dict[str, Any]) -> dict[str, tuple[str, float]]:
        """Mean value per score name on an item (several scores of one name are averaged)."""
        found: dict[str, list[float]] = {}
        types: dict[str, str] = {}
        for score in item["scores"]:
            if score["value"] is None:
                continue  # labels only: not compared
            found.setdefault(score["name"], []).append(float(score["value"]))
            types[score["name"]] = score["data_type"]
        return {name: (types[name], sum(v) / len(v)) for name, v in found.items()}

    a_values = {item_id: values(a_items[item_id]) for item_id in paired}
    b_values = {item_id: values(b_items[item_id]) for item_id in paired}
    names = sorted({n for v in (*a_values.values(), *b_values.values()) for n in v})
    comparisons: list[dict[str, Any]] = []
    flips: dict[str, dict[str, str]] = {item_id: {} for item_id in paired}

    success_pairs = [(a_items[i]["error"] is None, b_items[i]["error"] is None) for i in paired]
    comparisons.append(
        _binary(SUCCESS, stats.compare_binary(success_pairs, margin=margin, seed=seed))
    )
    for item_id, (a_ok, b_ok) in zip(paired, success_pairs, strict=True):
        if a_ok != b_ok:
            flips[item_id][SUCCESS] = "improved" if b_ok else "regressed"

    for name in names:
        kinds = {v[name][0] for v in (*a_values.values(), *b_values.values()) if name in v}
        binary = kinds == {"BOOLEAN"}
        pairs: list[tuple[str, float, float]] = []
        for item_id in paired:
            a = a_values[item_id].get(name)
            b = b_values[item_id].get(name)
            if a is None and b is None:
                continue
            if a is None or b is None:
                # Only a failed task's missing score is filled in, as a failure; any other
                # missing score leaves the pair out.
                if not binary:
                    continue
                if a is None and a_items[item_id]["error"] is not None:
                    a = ("BOOLEAN", 0.0)
                elif b is None and b_items[item_id]["error"] is not None:
                    b = ("BOOLEAN", 0.0)
                else:
                    continue
            assert a is not None
            assert b is not None
            pairs.append((item_id, a[1], b[1]))
        if binary:
            result = stats.compare_binary(
                [(a >= 0.5, b >= 0.5) for _, a, b in pairs], margin=margin, seed=seed
            )
            comparisons.append(_binary(name, result))
            for item_id, a, b in pairs:
                if (a >= 0.5) != (b >= 0.5):
                    flips[item_id][name] = "improved" if b >= 0.5 else "regressed"
        else:
            result = stats.compare_numeric([(a, b) for _, a, b in pairs], seed=seed)
            comparisons.append(_numeric(name, result))
            # A numeric change counts as a flip only beyond the margin that matters.
            for item_id, a, b in pairs:
                if abs(b - a) > result.margin:
                    flips[item_id][name] = "improved" if b > a else "regressed"

    latency = stats.compare_numeric(
        [(a_items[i]["latency_ms"], b_items[i]["latency_ms"]) for i in paired],
        higher_is_better=False,
        seed=seed,
    )
    return {
        "baseline": asdict(baseline) | {"created_ns": str(baseline.created_ns)},
        "candidate": asdict(candidate) | {"created_ns": str(candidate.created_ns)},
        "pairs": len(paired),
        "only_baseline": len(set(a_items) - set(b_items)),
        "only_candidate": len(set(b_items) - set(a_items)),
        "margin": margin,
        "comparisons": comparisons,
        "latency": _numeric(LATENCY, latency) | {"unit": "ms", "lower_is_better": True},
        "items": [
            {
                "item_id": item_id,
                "input": a_items[item_id]["input"],
                "expected_output": a_items[item_id]["expected_output"],
                "baseline": _side(a_items[item_id]),
                "candidate": _side(b_items[item_id]),
                "flips": flips[item_id],
            }
            for item_id in paired
        ],
    }


def _side(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item[key] for key in ("output", "error", "trace_id", "latency_ms", "scores")}


def _binary(name: str, result: stats.BinaryComparison) -> dict[str, Any]:
    return {"name": name, "kind": "binary"} | _clean(asdict(result))


def _numeric(name: str, result: stats.NumericComparison) -> dict[str, Any]:
    return {"name": name, "kind": "numeric"} | _clean(asdict(result))


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe: NaN (no pairs) becomes null, intervals become two-item lists."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, float) and value != value:
            out[key] = None
        elif isinstance(value, tuple):
            out[key] = list(value)  # pyright: ignore[reportUnknownArgumentType]
        else:
            out[key] = value
    return out
