"""Experiment runs: a dataset run through a task, with each item's output, trace and scores.

Modelled on Langfuse dataset runs and Braintrust experiments. A run belongs to one dataset and
has a unique name within it. A run item records the task's output (or error), its timing and
the trace that holds it. When the caller did not trace the task itself, the server records a
one-span trace for the item, so every item has a trace for its scores to attach to.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

from agentwatch import scores
from agentwatch.datasets import Conflict, DatasetError, NotFound
from agentwatch.spans import KIND_INTERNAL, STATUS_ERROR, STATUS_OK, Attributes, Span
from agentwatch.store import Store

MAX_NAME = 200
MAX_TEXT = 2000
MAX_VALUE_BYTES = 512 * 1024
SERVICE = "agentwatch.experiment"
RUN_ID = "agentwatch.experiment.run_id"
RUN_NAME = "agentwatch.experiment.run_name"
DATASET_ID = "agentwatch.dataset.id"
ITEM_ID = "agentwatch.dataset_item.id"


@dataclass(frozen=True, slots=True)
class Run:
    id: str
    dataset_id: str
    name: str
    description: str | None
    metadata: dict[str, Any]
    created_ns: int


@dataclass(frozen=True, slots=True)
class RunItem:
    run_id: str
    item_id: str
    trace_id: str
    output: Any
    error: str | None
    start_ns: int
    end_ns: int
    created_ns: int


def create_run(store: Store, dataset_id: str, body: Any) -> Run:
    data = _object(body, "a run")
    name = _text(data.get("name"), MAX_NAME, "name", required=True)
    assert name is not None
    run = Run(
        id=uuid.uuid4().hex,
        dataset_id=dataset_id,
        name=name,
        description=_text(data.get("description"), MAX_TEXT, "description"),
        metadata=_metadata(data.get("metadata")),
        created_ns=time.time_ns(),
    )
    with store.transaction() as db:
        if not db.execute("SELECT 1 FROM datasets WHERE id = ?", [dataset_id]).fetchone():
            raise NotFound("dataset not found")
        if db.execute(
            "SELECT 1 FROM runs WHERE dataset_id = ? AND name = ?", [dataset_id, name]
        ).fetchone():
            raise Conflict(f"this dataset already has a run named {name!r}")
        db.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
            [run.id, dataset_id, name, run.description, _dumps(run.metadata), run.created_ns],
        )
    return run


def get_run(store: Store, run_id: str) -> Run:
    row = store.query_one(
        "SELECT id, dataset_id, name, description, metadata, created_ns FROM runs WHERE id = ?",
        [run_id],
    )
    if row is None:
        raise NotFound("run not found")
    return _run(row)


def delete_run(store: Store, run_id: str) -> None:
    """Remove a run and its run items. Their traces and scores stay, as trace data."""
    with store.transaction() as db:
        if not db.execute("DELETE FROM runs WHERE id = ? RETURNING id", [run_id]).fetchone():
            raise NotFound("run not found")
        db.execute("DELETE FROM run_items WHERE run_id = ?", [run_id])


# Per run: items, errors, mean latency, cost and tokens over the items' traces, and the mean
# and count of each score name on those traces.
_SUMMARY = """
    WITH r AS (SELECT * FROM runs WHERE dataset_id = ?),
    i AS (SELECT ri.* FROM run_items ri JOIN r ON r.id = ri.run_id),
    costs AS (
        SELECT i.run_id,
               sum(s.cost_usd) FILTER (WHERE s.counts_toward_totals) AS cost_usd,
               coalesce(sum(s.input_tokens) FILTER (WHERE s.is_model_call), 0) AS input_tokens,
               coalesce(sum(s.output_tokens) FILTER (WHERE s.is_model_call), 0) AS output_tokens
        FROM i JOIN spans s ON s.trace_id = i.trace_id
        GROUP BY i.run_id
    ),
    score_names AS (
        SELECT i.run_id, sc.name, count(*) AS n, avg(sc.value) AS mean
        FROM i JOIN scores sc ON sc.trace_id = i.trace_id
        GROUP BY i.run_id, sc.name
    ),
    run_scores AS (
        SELECT run_id, list(struct_pack(name := name, count := n, mean := mean) ORDER BY name) AS scores
        FROM score_names GROUP BY run_id
    ),
    counts AS (
        SELECT run_id, count(*) AS items, count(*) FILTER (WHERE error IS NOT NULL) AS errors,
               avg(greatest(end_ns - start_ns, 0)) / 1e6 AS mean_latency_ms
        FROM i GROUP BY run_id
    )
    SELECT r.id, r.dataset_id, r.name, r.description, r.metadata, r.created_ns,
           coalesce(c.items, 0), coalesce(c.errors, 0), c.mean_latency_ms,
           k.cost_usd, coalesce(k.input_tokens, 0), coalesce(k.output_tokens, 0),
           coalesce(rs.scores, [])
    FROM r
    LEFT JOIN counts c ON c.run_id = r.id
    LEFT JOIN costs k ON k.run_id = r.id
    LEFT JOIN run_scores rs ON rs.run_id = r.id
    ORDER BY r.created_ns DESC, r.id
"""


def list_runs(store: Store, dataset_id: str) -> list[dict[str, Any]]:
    if store.query_one("SELECT 1 FROM datasets WHERE id = ?", [dataset_id]) is None:
        raise NotFound("dataset not found")
    return [
        {
            "run": _run(row[:6]),
            "items": int(row[6]),
            "errors": int(row[7]),
            "mean_latency_ms": None if row[8] is None else float(row[8]),
            "cost_usd": None if row[9] is None else float(row[9]),
            "input_tokens": int(row[10]),
            "output_tokens": int(row[11]),
            "scores": [
                {"name": s["name"], "count": int(s["count"]), "mean": s["mean"]} for s in row[12]
            ],
        }
        for row in store.query(_SUMMARY, [dataset_id])
    ]


def run_items(store: Store, run_id: str) -> list[dict[str, Any]]:
    """The run's items with their dataset input and expected output, and their scores."""
    get_run(store, run_id)
    rows = store.query(
        """
        SELECT ri.item_id, ri.trace_id, ri.output, ri.error, ri.start_ns, ri.end_ns,
               di.input, di.expected_output,
               coalesce(list(struct_pack(id := sc.id, name := sc.name, data_type := sc.data_type,
                                         value := sc.value, label := sc.label, comment := sc.comment)
                             ORDER BY sc.name, sc.created_ns) FILTER (WHERE sc.id IS NOT NULL), [])
        FROM run_items ri
        LEFT JOIN dataset_items di ON di.id = ri.item_id
        LEFT JOIN scores sc ON sc.trace_id = ri.trace_id
        WHERE ri.run_id = ?
        GROUP BY ALL
        ORDER BY ri.start_ns, ri.item_id
        """,
        [run_id],
    )
    return [
        {
            "item_id": row[0],
            "trace_id": row[1],
            "output": json.loads(row[2]),
            "error": row[3],
            "latency_ms": max(0, int(row[5]) - int(row[4])) / 1e6,
            "input": None if row[6] is None else json.loads(row[6]),
            "expected_output": None if row[7] is None else json.loads(row[7]),
            "scores": [dict(score) for score in row[8]],
        }
        for row in rows
    ]


def add_run_items(store: Store, run_id: str, bodies: list[Any]) -> list[RunItem]:
    """Record run items (an item run again replaces its earlier result); all or none."""
    run = get_run(store, run_id)
    now = time.time_ns()
    parsed = [_run_item(run, body, now) for body in bodies]
    ids = [item.item_id for item, _, _ in parsed]
    if len(set(ids)) != len(ids):
        raise Conflict("an item is given twice")
    marks = ", ".join("?" for _ in ids)
    known = {
        row[0]: row[1]
        for row in store.query(
            f"SELECT id, input FROM dataset_items WHERE dataset_id = ? AND id IN ({marks})",
            [run.dataset_id, *ids],
        )
    }
    missing = [item_id for item_id in ids if item_id not in known]
    if missing:
        raise NotFound(f"item {missing[0]!r} is not in this run's dataset")
    found_scores: list[scores.Score] = []
    traces: list[Span] = []
    for item, item_scores, traced in parsed:
        found_scores.extend(item_scores)
        if not traced:
            traces.append(_item_trace(run, item, json.loads(known[item.item_id])))
    # Scores first, so a score ID conflict fails before anything else is recorded; then the
    # synthetic traces, so run items point at stored spans.
    try:
        store.upsert_scores(found_scores)
    except scores.ScoreConflict as error:
        raise Conflict(str(error)) from error
    if traces:
        store.insert(traces, now)
    with store.transaction() as db:
        for item, _, _ in parsed:
            db.execute(
                "INSERT OR REPLACE INTO run_items VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    item.run_id,
                    item.item_id,
                    item.trace_id,
                    _dumps(item.output),
                    item.error,
                    item.start_ns,
                    item.end_ns,
                    item.created_ns,
                ],
            )
    return [item for item, _, _ in parsed]


def _run_item(run: Run, body: Any, now: int) -> tuple[RunItem, list[scores.Score], bool]:
    data = _object(body, "a run item")
    item_id = data.get("item_id")
    if not isinstance(item_id, str) or not item_id or len(item_id) > MAX_NAME:
        raise DatasetError("item_id is required")
    given_trace = data.get("trace_id")
    traced = given_trace is not None
    trace_id = _hex(given_trace, 32, "trace_id") if traced else os.urandom(16).hex()
    start_ns = _time(data.get("start_ns"), now, "start_ns")
    end_ns = _time(data.get("end_ns"), start_ns, "end_ns")
    if end_ns < start_ns:
        raise DatasetError("end_ns is before start_ns")
    output = data.get("output")
    if len(_dumps(output)) > MAX_VALUE_BYTES:
        raise DatasetError(f"an output is limited to {MAX_VALUE_BYTES // 1024} KiB of JSON")
    item = RunItem(
        run_id=run.id,
        item_id=item_id,
        trace_id=trace_id,
        output=output,
        error=_text(data.get("error"), MAX_TEXT, "error"),
        start_ns=start_ns,
        end_ns=end_ns,
        created_ns=now,
    )
    raw_scores: Any = data.get("scores") or []
    if not isinstance(raw_scores, list):
        raise DatasetError("scores must be a list")
    found: list[scores.Score] = []
    for raw in raw_scores:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(raw, dict):
            raise DatasetError("each score must be a JSON object")
        try:
            found.append(
                scores.from_api({**raw, "trace_id": trace_id}, source=scores.EVAL, now_ns=end_ns)
            )
        except scores.ScoreError as error:
            raise DatasetError(f"score: {error}") from error
    return item, found, traced


def _item_trace(run: Run, item: RunItem, item_input: Any) -> Span:
    """A one-span trace standing for an item's untraced task call."""
    attributes: Attributes = {
        RUN_ID: run.id,
        RUN_NAME: run.name,
        DATASET_ID: run.dataset_id,
        ITEM_ID: item.item_id,
        "input.value": _dumps(item_input),
        "output.value": _dumps(item.output),
    }
    if item.error is not None:
        attributes["error.type"] = item.error.split(":", 1)[0][:100]
    return Span(
        trace_id=item.trace_id,
        span_id=os.urandom(8).hex(),
        parent_span_id=None,
        name=f"experiment {run.name}",
        start_ns=item.start_ns,
        end_ns=item.end_ns,
        kind=KIND_INTERNAL,
        status_code=STATUS_ERROR if item.error is not None else STATUS_OK,
        status_message=item.error or "",
        attributes=attributes,
        resource={"service.name": SERVICE},
        scope_name="agentwatch.experiments",
    )


def _run(row: Any) -> Run:
    return Run(
        id=row[0],
        dataset_id=row[1],
        name=row[2],
        description=row[3],
        metadata=json.loads(row[4]),
        created_ns=int(row[5]),
    )


def _dumps(value: Any) -> str:
    try:
        return json.dumps(value, allow_nan=False)
    except (ValueError, RecursionError) as error:
        raise DatasetError("values must be standard JSON (no NaN or Infinity)") from error


def _time(value: Any, default: int, field: str) -> int:
    if value is None:
        return default
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 19:
        value = int(value)
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**63 - 1:
        return value
    raise DatasetError(f"{field} must be Unix nanoseconds")


def _object(body: Any, what: str) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise DatasetError(f"{what} must be a JSON object")
    return body  # pyright: ignore[reportUnknownVariableType]


def _metadata(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise DatasetError("metadata must be a JSON object")
    return value  # pyright: ignore[reportUnknownVariableType]


def _text(value: Any, limit: int, field: str, *, required: bool = False) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise DatasetError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise DatasetError(f"{field} must be a string")
    value = value.strip()
    if len(value) > limit:
        raise DatasetError(f"{field} is longer than {limit} characters")
    return value


def _hex(value: Any, length: int, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != length
        or any(c not in "0123456789abcdef" for c in value.lower())
        or value.strip("0") == ""
    ):
        raise DatasetError(f"{field} must be {length} hex characters, not all zero")
    return value.lower()
