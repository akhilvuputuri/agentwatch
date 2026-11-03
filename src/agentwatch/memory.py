"""Memory: what an agent remembers, how each item changed, and what each model call had in context.

Read from the GenAI memory spans (semantic-conventions-genai, development status): a span whose
`gen_ai.operation.name` is `create_memory`, `update_memory`, `upsert_memory` or `delete_memory`
changes the records it names, and one whose operation is `search_memory` read them. Records come
from `gen_ai.memory.records` (a list, or a JSON string of one, as the conventions allow on spans),
else from `gen_ai.memory.record.id`. `gen_ai.memory.store.id` names the store.

Each record becomes one row, kept when its trace is pruned: an item's history outlasts the
traces it links to, though a read's copy of the content goes with its trace. A span that failed
records nothing, since its change or read may not have happened. A `delete_memory` without a
record ID (the whole store, in the conventions) and a write record without an ID are not
recorded, since no item can be named.

A model call has in context the records of the `search_memory` spans in the same agent that
ended before it started: the spans whose nearest `invoke_agent` ancestor is the call's own (or,
with none, that have none themselves). So a nested agent sees only its own reads, and a read
belongs inside the agent whose calls it feeds. An item read twice is listed once, as its latest
read. This is what was in the prompt, as the instrumentation reports it; whether the model used
an item is not known, so it is never called "used". A read alone, with no call after it, is
counted as a read, not as being in context.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

from agentwatch import genai
from agentwatch.spans import STATUS_ERROR, Span, Value, jsonable

if TYPE_CHECKING:
    from agentwatch.store import Store

STORE_ID = "gen_ai.memory.store.id"
RECORD_ID = "gen_ai.memory.record.id"
RECORDS = "gen_ai.memory.records"
QUERY_TEXT = "gen_ai.memory.query.text"

SEARCH = "search_memory"
DELETE = "delete_memory"
WRITES = frozenset({"create_memory", "update_memory", "upsert_memory", DELETE})
OPERATIONS = WRITES | {SEARCH}

# Bump when the rules above change; stored spans are then read again. They also depend on
# genai.DERIVATION_VERSION, which decides a span's operation.
DERIVATION_VERSION = 1


def derivation() -> str:
    """The version of every rule that decides the stored rows."""
    return f"{DERIVATION_VERSION}.{genai.DERIVATION_VERSION}"


MAX_RECORDS = 500
MAX_ID = 500
MAX_CONTENT_BYTES = 64 * 1024
MAX_METADATA_BYTES = 8 * 1024
MAX_PAGE = 500
MAX_READS = 50


@dataclass(frozen=True, slots=True)
class Row:
    """One record of one memory span."""

    trace_id: str
    span_id: str
    position: int
    operation: str
    store_id: str
    record_id: str | None
    # JSON text, or None when the span carried none (content capture off, or a bare ID).
    content: str | None
    metadata: str | None
    score: float | None
    service_name: str
    time_ns: int
    received_ns: int


COLUMNS = (
    "trace_id, span_id, position, operation, store_id, record_id, content, metadata, score, "
    "service_name, time_ns, received_ns"
)


def from_span(span: Span, received_ns: int) -> list[Row]:
    """The memory rows a span carries; none unless it is a successful memory operation."""
    operation = genai.read(span).operation
    if operation not in OPERATIONS or span.status_code == STATUS_ERROR:
        return []
    attributes = span.attributes
    store = attributes.get(STORE_ID)
    store_id = store[:MAX_ID] if isinstance(store, str) else ""
    records = _records(attributes.get(RECORDS))
    if records is None:
        single = _id(attributes.get(RECORD_ID))
        records = [] if single is None else [{"id": single}]
    rows: list[Row] = []
    for position, record in enumerate(records[:MAX_RECORDS]):
        record_id = _id(record.get("id"))
        if record_id is None and operation != SEARCH:
            continue
        rows.append(
            Row(
                trace_id=span.trace_id,
                span_id=span.span_id,
                position=position,
                operation=operation,
                store_id=store_id,
                record_id=record_id,
                # A null content is the conventions' way to send a record without its text.
                content=None
                if record.get("content") is None
                else _capped(record["content"], MAX_CONTENT_BYTES),
                metadata=_metadata(record.get("metadata")),
                score=_score(record.get("score")),
                service_name=span.service_name,
                time_ns=span.end_ns,
                received_ns=received_ns,
            )
        )
    return rows


def _records(value: Value) -> list[dict[str, Any]] | None:
    """The records list, from structured form or a JSON string; None when absent or unreadable."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return None
    if not isinstance(value, list):
        return None
    return [item for item in value if isinstance(item, dict)]  # pyright: ignore[reportUnknownVariableType]


def _id(value: Any) -> str | None:
    if isinstance(value, str) and value and len(value) <= MAX_ID:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _score(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


CUT = "… [cut by agentwatch]"


def _capped(value: Any, limit: int) -> str:
    """JSON text of a value, at most `limit` bytes; a longer one is kept as a cut string."""
    text = json.dumps(jsonable(value), ensure_ascii=False)
    if len(text.encode()) <= limit:
        return text
    whole = value if isinstance(value, str) else text
    # Escaping can grow the text, so shrink until the JSON of what is kept fits.
    keep = whole.encode()[:limit].decode(errors="ignore")
    while True:
        cut = json.dumps(keep + CUT, ensure_ascii=False)
        if len(cut.encode()) <= limit:
            return cut
        keep = keep[: max(0, len(keep) - max(1, (len(cut.encode()) - limit) // 2 + 1))]


def _metadata(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    text = json.dumps(jsonable(value), ensure_ascii=False)  # pyright: ignore[reportUnknownArgumentType]
    return text if len(text.encode()) <= MAX_METADATA_BYTES else None


def write(db: Any, spans: Iterable[Span], received_ns: int) -> None:
    """Replace the rows of these spans: a re-sent span replaces its earlier records.

    `db` is the store's connection inside a transaction.
    """
    spans = list(spans)
    if not spans:
        return
    keys = [(span.trace_id, span.span_id) for span in spans]
    for start in range(0, len(keys), 200):
        chunk = keys[start : start + 200]
        db.execute(
            "DELETE FROM memory_records WHERE (trace_id, span_id) IN ("
            + ", ".join("(?, ?)" for _ in chunk)
            + ")",
            [value for key in chunk for value in key],
        )
    rows = [row for span in spans for row in from_span(span, received_ns)]
    # A deleted item stays deleted: records from before its deletion are not recorded again.
    if rows:
        gone = {
            (str(store_id), str(record_id)): int(deleted)
            for store_id, record_id, deleted in db.execute(
                "SELECT store_id, record_id, deleted_ns FROM memory_deleted"
            ).fetchall()
        }
        if gone:
            rows = [
                r
                for r in rows
                if r.record_id is None or r.time_ns > gone.get((r.store_id, r.record_id), -1)
            ]
    for start in range(0, len(rows), 200):
        chunk = rows[start : start + 200]
        marks = ", ".join("(" + ", ".join("?" * 12) + ")" for _ in chunk)
        db.execute(
            f"INSERT INTO memory_records ({COLUMNS}) VALUES {marks}",
            [
                value
                for row in chunk
                for value in (
                    row.trace_id,
                    row.span_id,
                    row.position,
                    row.operation,
                    row.store_id,
                    row.record_id,
                    row.content,
                    row.metadata,
                    row.score,
                    row.service_name,
                    row.time_ns,
                    row.received_ns,
                )
            ],
        )


@dataclass(frozen=True, slots=True)
class ItemCursor:
    """A position in the item list: latest activity descending, then store and record ID."""

    last_ns: int
    store_id: str
    record_id: str

    def encode(self) -> str:
        key = json.dumps([self.store_id, self.record_id]).encode()
        return f"{self.last_ns}:{base64.urlsafe_b64encode(key).decode()}"

    @staticmethod
    def decode(value: str) -> ItemCursor:
        last, _, key = value.partition(":")
        try:
            if not last.isascii() or not last.isdigit() or len(last) > 19:
                raise ValueError
            store_id, record_id = json.loads(base64.urlsafe_b64decode(key.encode()))
            if not isinstance(store_id, str) or not isinstance(record_id, str):
                raise ValueError
        except (ValueError, TypeError, binascii.Error) as error:
            raise ValueError("cursor must be one from `next`") from error
        return ItemCursor(int(last), store_id, record_id)


_ITEMS_SQL = f"""
WITH items AS (
    SELECT
        store_id,
        record_id,
        min(time_ns) AS first_ns,
        max(time_ns) AS last_ns,
        max(time_ns) FILTER (WHERE operation <> '{SEARCH}') AS last_write_ns,
        count(*) FILTER (WHERE operation <> '{SEARCH}') AS writes,
        count(*) FILTER (WHERE operation = '{SEARCH}') AS reads,
        max(time_ns) FILTER (WHERE operation = '{SEARCH}') AS last_read_ns,
        arg_max(operation, (time_ns, received_ns, span_id, position))
            FILTER (WHERE operation <> '{SEARCH}') AS last_write,
        arg_max(content, (time_ns, received_ns, span_id, position))
            FILTER (WHERE content IS NOT NULL) AS content,
        arg_max(metadata, (time_ns, received_ns, span_id, position))
            FILTER (WHERE metadata IS NOT NULL) AS metadata,
        list(DISTINCT service_name ORDER BY service_name) AS services
    FROM memory_records
    WHERE record_id IS NOT NULL
    GROUP BY store_id, record_id
)
"""


def items(
    store: Store,
    *,
    limit: int = 50,
    before: ItemCursor | None = None,
    store_id: str | None = None,
    service: str | None = None,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """Remembered items, most recently active first. Page with the last item's `cursor`."""
    filters: list[str] = []
    params: list[Any] = []
    if store_id is not None:
        filters.append("store_id = ?")
        params.append(store_id)
    if service is not None:
        filters.append("list_contains(services, ?)")
        params.append(service)
    if query:
        filters.append(
            "(contains(lower(record_id), ?) OR contains(lower(coalesce(content, '')), ?))"
        )
        params.extend([query.lower(), query.lower()])
    if before is not None:
        filters.append(
            "(last_ns < ? OR (last_ns = ? AND (store_id > ? OR (store_id = ? AND record_id > ?))))"
        )
        params.extend(
            [before.last_ns, before.last_ns, before.store_id, before.store_id, before.record_id]
        )
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    params.append(max(1, min(limit, MAX_PAGE)))
    rows = store.query(
        f"{_ITEMS_SQL} SELECT * FROM items {where} "
        "ORDER BY last_ns DESC, store_id, record_id LIMIT ?",
        params,
        with_names=True,
    )
    return [_item(row) for row in rows]


def delete_item(store: Store, store_id: str, record_id: str, *, now_ns: int | None = None) -> bool:
    """Deletes an item's history: its records, and its records inside the stored memory spans,
    which are rewritten without them. The deletion is remembered, so re-reading or re-sending
    those spans cannot bring it back; a later change to the item is recorded as usual. Its text
    can still be in captured prompts and tool results until their traces are pruned. True
    when the item had any records."""
    now_ns = time.time_ns() if now_ns is None else now_ns
    with store.transaction() as db:
        spans = db.execute(
            "SELECT DISTINCT m.trace_id, m.span_id, s.attributes FROM memory_records m "
            "JOIN spans s USING (trace_id, span_id) WHERE m.store_id = ? AND m.record_id = ?",
            [store_id, record_id],
        ).fetchall()
        found = db.execute(
            "DELETE FROM memory_records WHERE store_id = ? AND record_id = ? RETURNING time_ns",
            [store_id, record_id],
        ).fetchall()
        if not found:
            return False
        latest = max(int(row[0]) for row in found)
        db.execute(
            "INSERT OR REPLACE INTO memory_deleted VALUES (?, ?, ?, ?)",
            [store_id, record_id, max(latest, now_ns), now_ns],
        )
        for trace_id, span_id, attributes in spans:
            db.execute(
                "UPDATE spans SET attributes = ? WHERE trace_id = ? AND span_id = ?",
                [json.dumps(_without(json.loads(attributes), record_id)), trace_id, span_id],
            )
    return True


def without_deleted(db: Any, spans: list[Span]) -> list[Span]:
    """Spans as they may be stored: memory spans lose the records of items deleted after them,
    so re-sending a span cannot bring a deleted item's text back."""
    if not any(genai.read(s).operation in OPERATIONS for s in spans):
        return spans
    gone = {
        (str(store_id), str(record_id)): int(deleted)
        for store_id, record_id, deleted in db.execute(
            "SELECT store_id, record_id, deleted_ns FROM memory_deleted"
        ).fetchall()
    }
    if not gone:
        return spans
    kept: list[Span] = []
    for span in spans:
        store = span.attributes.get(STORE_ID)
        store_id = store[:MAX_ID] if isinstance(store, str) else ""
        attributes: dict[str, Any] = dict(span.attributes)
        for (deleted_store, record_id), deleted_ns in gone.items():
            if deleted_store == store_id and span.end_ns <= deleted_ns:
                attributes = _without(attributes, record_id)
        kept.append(span if attributes == span.attributes else replace(span, attributes=attributes))
    return kept


def _without(attributes: dict[str, Any], record_id: str) -> dict[str, Any]:
    """A memory span's attributes without one item's records."""
    kept = dict(attributes)
    records = _records(kept.get(RECORDS))
    if records is not None:
        remaining = [r for r in records if _id(r.get("id")) != record_id]
        kept[RECORDS] = json.dumps(remaining) if isinstance(kept[RECORDS], str) else remaining
    if _id(kept.get(RECORD_ID)) == record_id:
        del kept[RECORD_ID]
    return kept


def stores(store: Store) -> list[str]:
    rows = store.query("SELECT DISTINCT store_id FROM memory_records ORDER BY store_id")
    return [str(row[0]) for row in rows]


def item(store: Store, store_id: str, record_id: str) -> dict[str, Any] | None:
    """One item: its summary, its changes and its most recent reads, each newest first.

    Changes and reads are fetched apart, since an agent that reads every item on every turn
    would otherwise bury the few changes under its reads.
    """
    found = store.query(
        f"{_ITEMS_SQL} SELECT * FROM items WHERE store_id = ? AND record_id = ?",
        [store_id, record_id],
        with_names=True,
    )
    if not found:
        return None
    summary = found[0]
    changes = _events(store, store_id, record_id, reads=False, limit=MAX_PAGE)
    reads = _events(store, store_id, record_id, reads=True, limit=MAX_READS)
    return {
        "item": _item(summary),
        "changes": changes,
        "reads": reads,
        "changes_truncated": int(summary["writes"]) > len(changes),
        "reads_truncated": int(summary["reads"]) > len(reads),
    }


def _events(
    store: Store, store_id: str, record_id: str, *, reads: bool, limit: int
) -> list[dict[str, Any]]:
    rows = store.query(
        f"""SELECT m.trace_id, m.span_id, m.operation, m.content, m.metadata, m.score,
                   m.service_name, m.time_ns, s.span_id IS NOT NULL AS stored, s.links, s.name
            FROM memory_records m
            LEFT JOIN spans s ON s.trace_id = m.trace_id AND s.span_id = m.span_id
            WHERE m.store_id = ? AND m.record_id = ?
              AND m.operation {"=" if reads else "<>"} '{SEARCH}'
            ORDER BY m.time_ns DESC, m.received_ns DESC, m.span_id, m.position
            LIMIT ?""",
        [store_id, record_id, limit],
        with_names=True,
    )
    return [
        {
            "trace_id": row["trace_id"],
            "span_id": row["span_id"],
            "operation": row["operation"],
            "content": _loads(row["content"]),
            "metadata": _loads(row["metadata"]),
            "score": row["score"],
            "service_name": row["service_name"],
            "time_ns": str(row["time_ns"]),
            # False once the trace was pruned: the record stays, its trace link does not.
            "trace_stored": bool(row["stored"]),
            "span_name": row["name"],
            "links": _links(row["links"]),
        }
        for row in rows
    ]


def in_context(store: Store, spans: Sequence[Span]) -> dict[str, list[dict[str, Any]]]:
    """For each model call in one trace, the memory records it had in context."""
    if not spans:
        return {}
    trace_id = spans[0].trace_id
    rows = store.query(
        f"""SELECT span_id, position, store_id, record_id, content, metadata, score
            FROM memory_records WHERE trace_id = ? AND operation = '{SEARCH}'
            ORDER BY span_id, position""",
        [trace_id],
        with_names=True,
    )
    if not rows:
        return {}
    by_id = {span.span_id: span for span in spans}
    operations = {span.span_id: genai.read(span).operation for span in spans}
    records: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        records.setdefault(row["span_id"], []).append(row)
    agents = {span_id: _agent(span_id, by_id, operations) for span_id in by_id}
    searches = [
        by_id[span_id]
        for span_id in records
        if span_id in by_id and by_id[span_id].status_code != STATUS_ERROR
    ]
    searches.sort(key=lambda s: (s.end_ns, s.span_id))
    found: dict[str, list[dict[str, Any]]] = {}
    for span in spans:
        fields = genai.read(span)
        # An embeddings call has no prompt to hold memory.
        if not fields.is_model_call or fields.operation == "embeddings":
            continue
        # An item read more than once is in the prompt once: its latest read stands.
        latest: dict[tuple[str, str] | int, dict[str, Any]] = {}
        unnamed = 0
        for search in searches:
            if search.end_ns > span.start_ns or agents[search.span_id] != agents[span.span_id]:
                continue
            for row in records[search.span_id]:
                key: tuple[str, str] | int
                if row["record_id"] is None:
                    unnamed += 1
                    key = unnamed
                else:
                    key = (row["store_id"], row["record_id"])
                latest.pop(key, None)
                latest[key] = {
                    "search_span_id": search.span_id,
                    "search_span_name": search.name,
                    "store_id": row["store_id"],
                    "record_id": row["record_id"],
                    "content": _loads(row["content"]),
                    "metadata": _loads(row["metadata"]),
                    "score": row["score"],
                    "characters": _characters(row["content"], row["metadata"]),
                }
        if latest:
            found[span.span_id] = list(latest.values())
    return found


def _agent(span_id: str, by_id: dict[str, Span], operations: dict[str, str | None]) -> str | None:
    """The nearest invoke_agent ancestor of a span (not the span itself), or None."""
    seen: set[str] = set()
    parent = by_id[span_id].parent_span_id
    while parent is not None and parent in by_id and parent not in seen:
        if operations[parent] == "invoke_agent":
            return parent
        seen.add(parent)
        parent = by_id[parent].parent_span_id
    return None


def _characters(content: str | None, metadata: str | None) -> int | None:
    """An item's size in characters: from its metadata when the instrumentation gave one,
    else from its content when captured."""
    meta = _loads(metadata)
    if isinstance(meta, dict):
        given = meta.get("characters")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if isinstance(given, int) and not isinstance(given, bool) and given >= 0:
            return given
    value = _loads(content)
    if value is None:
        return None
    return len(value) if isinstance(value, str) else len(json.dumps(value, ensure_ascii=False))


def _item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "store_id": row["store_id"],
        "record_id": row["record_id"],
        # The last change was a delete; a read after it does not undo that.
        "deleted": row["last_write"] == DELETE,
        "content": _loads(row["content"]),
        "metadata": _loads(row["metadata"]),
        "writes": int(row["writes"]),
        "reads": int(row["reads"]),
        "first_ns": str(row["first_ns"]),
        "last_ns": str(row["last_ns"]),
        "last_write_ns": None if row["last_write_ns"] is None else str(row["last_write_ns"]),
        "last_read_ns": None if row["last_read_ns"] is None else str(row["last_read_ns"]),
        "services": list(row["services"]),
        "cursor": ItemCursor(int(row["last_ns"]), row["store_id"], row["record_id"]).encode(),
    }


def _links(text: Any) -> list[dict[str, str]]:
    """A span's links (to the turns a learner read, say) as trace and span IDs."""
    stored: Any = _loads(text)
    if not isinstance(stored, list):
        return []
    found: list[dict[str, str]] = []
    for link in cast("list[Any]", stored):
        if isinstance(link, dict):
            entry = cast("dict[str, Any]", link)
            found.append(
                {"trace_id": str(entry.get("trace_id")), "span_id": str(entry.get("span_id"))}
            )
    return found


def _loads(text: Any) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text
