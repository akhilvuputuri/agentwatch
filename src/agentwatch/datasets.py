"""Datasets: collections of items (an input, an expected output, metadata) to run experiments on.

Modelled on Langfuse datasets. An item can be written by hand, sent through the API, or made
from a trace, in which case it keeps a link to the trace and span it came from. Items are
archived rather than deleted, so experiments that used them stay readable.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any

import duckdb

from agentwatch.spans import Attributes
from agentwatch.store import Store

ACTIVE = "ACTIVE"
ARCHIVED = "ARCHIVED"
MAX_NAME = 200
MAX_DESCRIPTION = 2000
MAX_ITEM_BYTES = 512 * 1024
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}")

# Span attributes that hold a call's input and output, in priority order: the GenAI
# conventions' opt-in message capture, then OpenInference.
INPUT_ATTRIBUTES = ("gen_ai.input.messages", "input.value")
OUTPUT_ATTRIBUTES = ("gen_ai.output.messages", "output.value")


class DatasetError(ValueError):
    """A request that cannot be applied, with the reason."""


class NotFound(DatasetError):
    """The dataset, item or trace does not exist."""


class Conflict(DatasetError):
    """A name or ID already taken."""


@dataclass(frozen=True, slots=True)
class Dataset:
    id: str
    name: str
    description: str | None
    metadata: dict[str, Any]
    created_ns: int
    items: int = 0
    archived_items: int = 0


@dataclass(frozen=True, slots=True)
class Item:
    id: str
    dataset_id: str
    input: Any
    expected_output: Any
    metadata: dict[str, Any]
    source_trace_id: str | None
    source_span_id: str | None
    status: str
    created_ns: int


def create_dataset(store: Store, body: Any) -> Dataset:
    data = _object(body, "a dataset")
    name = _text(data.get("name"), MAX_NAME, "name", required=True)
    assert name is not None
    dataset = Dataset(
        id=uuid.uuid4().hex,
        name=name,
        description=_text(data.get("description"), MAX_DESCRIPTION, "description"),
        metadata=_metadata(data.get("metadata")),
        created_ns=time.time_ns(),
    )
    with store.transaction() as db:
        if db.execute("SELECT 1 FROM datasets WHERE name = ?", [name]).fetchone():
            raise Conflict(f"a dataset named {name!r} already exists")
        db.execute(
            "INSERT INTO datasets VALUES (?, ?, ?, ?, ?)",
            [
                dataset.id,
                dataset.name,
                dataset.description,
                json.dumps(dataset.metadata),
                dataset.created_ns,
            ],
        )
    return dataset


_DATASET_QUERY = """
    SELECT d.id, d.name, d.description, d.metadata, d.created_ns,
           count(i.id) FILTER (WHERE i.status = 'ACTIVE'),
           count(i.id) FILTER (WHERE i.status = 'ARCHIVED')
    FROM datasets d LEFT JOIN dataset_items i ON i.dataset_id = d.id
"""


def list_datasets(store: Store) -> list[Dataset]:
    rows = store.query(f"{_DATASET_QUERY} GROUP BY ALL ORDER BY d.created_ns DESC, d.id")
    return [_dataset(row) for row in rows]


def get_dataset(store: Store, dataset_id: str) -> Dataset:
    row = store.query_one(f"{_DATASET_QUERY} WHERE d.id = ? GROUP BY ALL", [dataset_id])
    if row is None:
        raise NotFound("dataset not found")
    return _dataset(row)


def delete_dataset(store: Store, dataset_id: str) -> None:
    with store.transaction() as db:
        if not db.execute(
            "DELETE FROM datasets WHERE id = ? RETURNING id", [dataset_id]
        ).fetchone():
            raise NotFound("dataset not found")
        db.execute("DELETE FROM dataset_items WHERE dataset_id = ?", [dataset_id])


def add_items(store: Store, dataset_id: str, bodies: list[Any]) -> list[Item]:
    """Add items, or replace those with a given ID in the same dataset; all or none."""
    now = time.time_ns()
    items = [_item(dataset_id, body, now + index) for index, body in enumerate(bodies)]
    ids = [item.id for item in items]
    if len(set(ids)) != len(ids):
        raise Conflict("an item ID is given twice")
    with store.transaction() as db:
        _require_dataset(db, dataset_id)
        if ids:
            marks = ", ".join("?" for _ in ids)
            taken = db.execute(
                f"SELECT id FROM dataset_items WHERE id IN ({marks}) AND dataset_id <> ?",
                [*ids, dataset_id],
            ).fetchone()
            if taken:
                raise Conflict(f"item ID {taken[0]!r} belongs to another dataset")
            # A replaced item keeps its place (creation time) and archived state.
            existing = dict(
                db.execute(
                    f"SELECT id, (created_ns, status) FROM dataset_items WHERE id IN ({marks})", ids
                ).fetchall()
            )
            items = [
                replace(item, created_ns=existing[item.id][0], status=existing[item.id][1])
                if item.id in existing
                else item
                for item in items
            ]
        for item in items:
            _write_item(db, item)
    return items


def item_from_trace(store: Store, dataset_id: str, body: Any) -> Item:
    """An item made from a span: its recorded input and output, linked back to the span."""
    data = _object(body, "the request")
    trace_id = _hex(data.get("trace_id"), 32, "trace_id")
    span_raw = data.get("span_id")
    span_id = None if span_raw in (None, "") else _hex(span_raw, 16, "span_id")
    spans = store.trace(trace_id)
    if not spans:
        raise NotFound("trace not found")
    if span_id is None:
        # The root: the span with no parent, else the earliest.
        chosen = min(spans, key=lambda s: (s.span.parent_span_id is not None, s.span.start_ns))
    else:
        matches = [s for s in spans if s.span.span_id == span_id]
        if not matches:
            raise NotFound("span not found in the trace")
        chosen = matches[0]
    attributes = chosen.span.attributes
    item = Item(
        id=uuid.uuid4().hex,
        dataset_id=dataset_id,
        input=_captured(attributes, INPUT_ATTRIBUTES),
        expected_output=_captured(attributes, OUTPUT_ATTRIBUTES),
        metadata=_metadata(data.get("metadata")),
        source_trace_id=trace_id,
        source_span_id=chosen.span.span_id,
        status=ACTIVE,
        created_ns=time.time_ns(),
    )
    _check_size(item)
    with store.transaction() as db:
        _require_dataset(db, dataset_id)
        _write_item(db, item)
    return item


def list_items(store: Store, dataset_id: str, *, archived: bool = False) -> list[Item]:
    get_dataset(store, dataset_id)
    rows = store.query(
        "SELECT id, dataset_id, input, expected_output, metadata, source_trace_id, source_span_id, "
        "status, created_ns FROM dataset_items WHERE dataset_id = ? "
        + ("" if archived else "AND status = 'ACTIVE' ")
        + "ORDER BY created_ns, id",
        [dataset_id],
    )
    return [_stored_item(row) for row in rows]


def set_item_status(store: Store, item_id: str, body: Any) -> None:
    status = _object(body, "the request").get("status")
    if status not in (ACTIVE, ARCHIVED):
        raise DatasetError(f"status must be {ACTIVE} or {ARCHIVED}")
    with store.transaction() as db:
        if not db.execute(
            "UPDATE dataset_items SET status = ? WHERE id = ? RETURNING id", [status, item_id]
        ).fetchone():
            raise NotFound("item not found")


def _captured(attributes: Attributes, names: tuple[str, ...]) -> Any:
    """The first captured value, parsed when it holds JSON text (as both conventions use)."""
    for name in names:
        value = attributes.get(name)
        if value is None or value == "":
            continue
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value
    return None


def _item(dataset_id: str, body: Any, now: int) -> Item:
    data = _object(body, "an item")
    if "input" not in data:
        raise DatasetError("an item needs an input")
    given = data.get("id")
    if given is None:
        item_id = uuid.uuid4().hex
    elif isinstance(given, str) and _ID.fullmatch(given) and given not in (".", ".."):
        item_id = given
    else:
        raise DatasetError("id must be 1 to 200 letters, digits or . _ : - (not . or ..)")
    trace = data.get("source_trace_id")
    span = data.get("source_span_id")
    item = Item(
        id=item_id,
        dataset_id=dataset_id,
        input=data["input"],
        expected_output=data.get("expected_output"),
        metadata=_metadata(data.get("metadata")),
        source_trace_id=None if trace is None else _hex(trace, 32, "source_trace_id"),
        source_span_id=None if span is None else _hex(span, 16, "source_span_id"),
        status=ACTIVE,
        created_ns=now,
    )
    _check_size(item)
    return item


def _check_size(item: Item) -> None:
    size = len(json.dumps([item.input, item.expected_output, item.metadata]))
    if size > MAX_ITEM_BYTES:
        raise DatasetError(f"an item is limited to {MAX_ITEM_BYTES // 1024} KiB of JSON")


def _write_item(db: duckdb.DuckDBPyConnection, item: Item) -> None:
    db.execute(
        "INSERT OR REPLACE INTO dataset_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            item.id,
            item.dataset_id,
            json.dumps(item.input),
            json.dumps(item.expected_output),
            json.dumps(item.metadata),
            item.source_trace_id,
            item.source_span_id,
            item.status,
            item.created_ns,
        ],
    )


def _require_dataset(db: duckdb.DuckDBPyConnection, dataset_id: str) -> None:
    if not db.execute("SELECT 1 FROM datasets WHERE id = ?", [dataset_id]).fetchone():
        raise NotFound("dataset not found")


def _dataset(row: Any) -> Dataset:
    return Dataset(
        id=row[0],
        name=row[1],
        description=row[2],
        metadata=json.loads(row[3]),
        created_ns=int(row[4]),
        items=int(row[5]),
        archived_items=int(row[6]),
    )


def _stored_item(row: Any) -> Item:
    return Item(
        id=row[0],
        dataset_id=row[1],
        input=json.loads(row[2]),
        expected_output=json.loads(row[3]),
        metadata=json.loads(row[4]),
        source_trace_id=row[5],
        source_span_id=row[6],
        status=row[7],
        created_ns=int(row[8]),
    )


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
