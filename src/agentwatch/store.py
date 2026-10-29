"""DuckDB storage for spans, with trace summaries computed at query time."""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from agentwatch import genai
from agentwatch.spans import STATUS_ERROR, Attributes, Span, SpanEvent, SpanLink, jsonable

SCHEMA_VERSION = 2

# The original (version 1) schema. Newer versions are reached through MIGRATIONS, so a new
# database and an upgraded one end up identical.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL);
CREATE TABLE IF NOT EXISTS spans (
    trace_id VARCHAR NOT NULL,
    span_id VARCHAR NOT NULL,
    parent_span_id VARCHAR,
    name VARCHAR NOT NULL,
    kind TINYINT NOT NULL,
    start_ns BIGINT NOT NULL,
    end_ns BIGINT NOT NULL,
    status_code TINYINT NOT NULL,
    status_message VARCHAR NOT NULL,
    service_name VARCHAR NOT NULL,
    scope_name VARCHAR NOT NULL,
    scope_version VARCHAR NOT NULL,
    attributes JSON NOT NULL,
    resource JSON NOT NULL,
    events JSON NOT NULL,
    links JSON NOT NULL,
    operation VARCHAR,
    provider VARCHAR,
    request_model VARCHAR,
    response_model VARCHAR,
    input_tokens BIGINT,
    output_tokens BIGINT,
    cache_read_tokens BIGINT,
    cache_write_tokens BIGINT,
    reasoning_tokens BIGINT,
    cost_usd DOUBLE,
    finish_reasons VARCHAR[] NOT NULL,
    tool_name VARCHAR,
    agent_name VARCHAR,
    conversation_id VARCHAR,
    error_type VARCHAR,
    is_model_call BOOLEAN NOT NULL,
    counts_toward_totals BOOLEAN NOT NULL,
    received_ns BIGINT NOT NULL,
    PRIMARY KEY (trace_id, span_id)
);
CREATE INDEX IF NOT EXISTS spans_start ON spans (start_ns);
"""

# Statements that take the schema from version N-1 to N.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: ("ALTER TABLE spans ADD COLUMN user_id VARCHAR",),
}

# Columns computed from a span's attributes by genai.read. They are re-read for every stored
# span when genai.DERIVATION_VERSION changes, so rule changes apply to old data too.
_DERIVED = (
    "operation",
    "provider",
    "request_model",
    "response_model",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "cost_usd",
    "finish_reasons",
    "tool_name",
    "agent_name",
    "conversation_id",
    "error_type",
    "is_model_call",
    "counts_toward_totals",
    "user_id",
)

_COLUMNS = (
    "trace_id, span_id, parent_span_id, name, kind, start_ns, end_ns, status_code, "
    "status_message, service_name, scope_name, scope_version, attributes, resource, events, "
    "links, operation, provider, request_model, response_model, input_tokens, output_tokens, "
    "cache_read_tokens, cache_write_tokens, reasoning_tokens, cost_usd, finish_reasons, "
    "tool_name, agent_name, conversation_id, error_type, is_model_call, counts_toward_totals, "
    "received_ns, user_id"
)
_PLACEHOLDERS = ", ".join("?" for _ in _COLUMNS.split(","))
_INSERT_CHUNK = 200


@dataclass(frozen=True, slots=True)
class TraceSummary:
    trace_id: str
    root_name: str
    service_name: str
    start_ns: int
    duration_ns: int
    span_count: int
    error_count: int
    model_calls: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cost_usd: float | None
    # Model calls that reported no cost; when above zero, cost_usd is a lower bound.
    calls_without_cost: int
    models: tuple[str, ...]
    complete: bool

    @property
    def cursor(self) -> Cursor:
        return Cursor(self.start_ns, self.trace_id)


@dataclass(frozen=True, slots=True)
class Cursor:
    """A position in the trace list: traces sort by start time descending, then by ID."""

    start_ns: int
    trace_id: str

    def encode(self) -> str:
        return f"{self.start_ns}:{self.trace_id}"

    @staticmethod
    def decode(value: str) -> Cursor:
        start, _, trace_id = value.partition(":")
        trace_id = trace_id.lower()
        if (
            not start.isascii()
            or not start.lstrip("-").isdigit()
            or len(trace_id) != 32
            or any(c not in "0123456789abcdef" for c in trace_id)
        ):
            raise ValueError("cursor must be <start_ns>:<trace_id>")
        return Cursor(int(start), trace_id)


@dataclass(frozen=True, slots=True)
class StoredSpan:
    span: Span
    fields: genai.GenAI


# A trace's root: among spans whose parent is absent from the trace, a span with no parent
# at all wins over an orphan, then the earliest. `complete` is whether that root has no parent.
ROOTS_SQL = """
roots AS (
    SELECT
        s.trace_id,
        arg_min(s.name, (s.parent_span_id IS NOT NULL, s.start_ns, s.span_id)) AS root_name,
        arg_min(s.service_name, (s.parent_span_id IS NOT NULL, s.start_ns, s.span_id)) AS service_name,
        arg_min(s.start_ns, (s.parent_span_id IS NOT NULL, s.start_ns, s.span_id)) AS root_start_ns,
        bool_or(s.parent_span_id IS NULL) AS complete
    FROM spans s
    LEFT JOIN spans p ON p.trace_id = s.trace_id AND p.span_id = s.parent_span_id
    WHERE p.span_id IS NULL
    GROUP BY s.trace_id
)
"""


class Store:
    """A single-process store. All access is serialized through one lock."""

    def __init__(self, path: str | Path | None = None) -> None:
        target = ":memory:" if path is None else str(path)
        if path is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = duckdb.connect(target)
        with self._lock:
            self._migrate()
            self._rederive()

    def _meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", [key]).fetchone()
        return None if row is None else str(row[0])

    def _set_meta(self, key: str, value: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", [key, value])

    def _migrate(self) -> None:
        self._db.execute(_SCHEMA)
        stored = self._meta("schema")
        version = 1 if stored is None else int(stored)
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema {version} is newer than this agentwatch supports "
                f"({SCHEMA_VERSION}); upgrade agentwatch"
            )
        if stored is None:
            self._set_meta("schema", "1")
        for target in range(version + 1, SCHEMA_VERSION + 1):
            self._db.execute("BEGIN")
            try:
                for statement in MIGRATIONS[target]:
                    self._db.execute(statement)
                self._set_meta("schema", str(target))
                self._db.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(duckdb.Error):
                    self._db.execute("ROLLBACK")
                raise

    def _rederive(self) -> None:
        """Re-read every stored span's attributes when the extraction rules have changed."""
        current = str(genai.DERIVATION_VERSION)
        stored = self._meta("derivation")
        if stored == current:
            return
        rows = self._db.execute("SELECT trace_id, span_id, attributes FROM spans").fetchall()
        assignments = ", ".join(f"{column} = d.{column}" for column in _DERIVED)
        self._db.execute("BEGIN")
        try:
            self._db.execute(
                "CREATE TEMP TABLE derived AS SELECT trace_id, span_id, "
                + ", ".join(_DERIVED)
                + " FROM spans LIMIT 0"
            )
            for start in range(0, len(rows), _INSERT_CHUNK):
                chunk = rows[start : start + _INSERT_CHUNK]
                values: list[Any] = []
                for trace_id, span_id, attributes in chunk:
                    span = Span(
                        trace_id=trace_id,
                        span_id=span_id,
                        parent_span_id=None,
                        name="",
                        start_ns=0,
                        end_ns=0,
                        attributes=json.loads(attributes),
                    )
                    values.extend((trace_id, span_id, *_derived(genai.read(span))))
                marks = "(" + ", ".join("?" for _ in range(len(_DERIVED) + 2)) + ")"
                self._db.execute(
                    f"INSERT INTO derived VALUES {', '.join(marks for _ in chunk)}", values
                )
            if rows:
                self._db.execute(
                    f"UPDATE spans SET {assignments} FROM derived d "
                    "WHERE spans.trace_id = d.trace_id AND spans.span_id = d.span_id"
                )
            self._db.execute("DROP TABLE derived")
            self._set_meta("derivation", current)
            self._db.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(duckdb.Error):
                self._db.execute("ROLLBACK")
            raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def insert(self, spans: Iterable[Span], received_ns: int) -> int:
        """Insert spans; a re-sent span (same trace and span ID) replaces the earlier copy."""
        latest: dict[tuple[str, str], Span] = {}
        for span in spans:
            latest[(span.trace_id, span.span_id)] = span
        rows = [_row(span, received_ns) for span in latest.values()]
        if not rows:
            return 0
        with self._lock:
            self._db.execute("BEGIN")
            try:
                # One multi-row statement per chunk is several times faster than executemany.
                for start in range(0, len(rows), _INSERT_CHUNK):
                    chunk = rows[start : start + _INSERT_CHUNK]
                    values = ", ".join(f"({_PLACEHOLDERS})" for _ in chunk)
                    self._db.execute(
                        f"INSERT OR REPLACE INTO spans ({_COLUMNS}) VALUES {values}",
                        [value for row in chunk for value in row],
                    )
                self._db.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(duckdb.Error):
                    self._db.execute("ROLLBACK")
                raise
        return len(rows)

    def query(self, sql: str, params: Sequence[Any] = (), *, with_names: bool = False) -> list[Any]:
        """Run a read query. With `with_names`, rows are dicts keyed by column name."""
        with self._lock:
            cursor = self._db.execute(sql, list(params))
            rows = cursor.fetchall()
            names = [column[0] for column in cursor.description or []]
        if with_names:
            return [dict(zip(names, row, strict=True)) for row in rows]
        return rows

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> Any:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def span_count(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT count(*) FROM spans").fetchone()
        return 0 if row is None else int(row[0])

    def services(self) -> list[str]:
        """Services of trace roots, the values the trace list filters on."""
        with self._lock:
            rows = self._db.execute(
                f"WITH {ROOTS_SQL} SELECT DISTINCT service_name FROM roots ORDER BY service_name"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def traces(
        self,
        *,
        limit: int = 50,
        before: Cursor | None = None,
        service: str | None = None,
        errors_only: bool = False,
    ) -> list[TraceSummary]:
        """Most recent traces first. Page with the last trace's `cursor` as `before`."""
        filters: list[str] = []
        params: list[Any] = []
        if service is not None:
            filters.append("service_name = ?")
            params.append(service)
        if before is not None:
            # Traces sharing a start time are ordered by ID, so the cursor needs both.
            filters.append("(start_ns < ? OR (start_ns = ? AND trace_id > ?))")
            params.extend([before.start_ns, before.start_ns, before.trace_id])
        if errors_only:
            filters.append("error_count > 0")
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        params.append(max(1, min(limit, 500)))
        query = f"""
            WITH {ROOTS_SQL},
            totals AS (
                SELECT
                    trace_id,
                    min(start_ns) AS start_ns,
                    max(end_ns) AS end_ns,
                    count(*) AS span_count,
                    count(*) FILTER (WHERE status_code = {STATUS_ERROR}) AS error_count,
                    count(*) FILTER (WHERE is_model_call) AS model_calls,
                    count(*) FILTER (WHERE operation = 'execute_tool') AS tool_calls,
                    coalesce(sum(input_tokens) FILTER (WHERE is_model_call), 0) AS input_tokens,
                    coalesce(sum(output_tokens) FILTER (WHERE is_model_call), 0) AS output_tokens,
                    coalesce(sum(cache_read_tokens) FILTER (WHERE is_model_call), 0) AS cache_read,
                    sum(cost_usd) FILTER (WHERE counts_toward_totals) AS cost_usd,
                    count(*) FILTER (WHERE is_model_call AND cost_usd IS NULL) AS calls_without_cost,
                    list(DISTINCT coalesce(response_model, request_model) ORDER BY coalesce(response_model, request_model))
                        FILTER (WHERE is_model_call AND coalesce(response_model, request_model) IS NOT NULL) AS models
                FROM spans GROUP BY trace_id
            ),
            joined AS (
                SELECT t.*, r.root_name, r.service_name, r.complete
                FROM totals t JOIN roots r USING (trace_id)
            )
            SELECT trace_id, root_name, service_name, start_ns, end_ns - start_ns, span_count,
                   error_count, model_calls, tool_calls, input_tokens, output_tokens, cache_read,
                   cost_usd, calls_without_cost, coalesce(models, []), complete
            FROM joined {where}
            ORDER BY start_ns DESC, trace_id
            LIMIT ?
        """
        with self._lock:
            rows = self._db.execute(query, params).fetchall()
        return [
            TraceSummary(
                trace_id=row[0],
                root_name=row[1],
                service_name=row[2],
                start_ns=int(row[3]),
                duration_ns=max(0, int(row[4])),
                span_count=int(row[5]),
                error_count=int(row[6]),
                model_calls=int(row[7]),
                tool_calls=int(row[8]),
                input_tokens=int(row[9]),
                output_tokens=int(row[10]),
                cache_read_tokens=int(row[11]),
                cost_usd=None if row[12] is None else float(row[12]),
                calls_without_cost=int(row[13]),
                models=tuple(row[14]),
                complete=bool(row[15]),
            )
            for row in rows
        ]

    def trace(self, trace_id: str) -> list[StoredSpan]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_COLUMNS} FROM spans WHERE trace_id = ? ORDER BY start_ns, span_id",
                [trace_id.lower()],
            ).fetchall()
        return [_stored(row) for row in rows]


def _row(span: Span, received_ns: int) -> Sequence[Any]:
    fields = genai.read(span)
    return (
        span.trace_id,
        span.span_id,
        span.parent_span_id,
        span.name,
        span.kind,
        span.start_ns,
        span.end_ns,
        span.status_code,
        span.status_message,
        span.service_name,
        span.scope_name,
        span.scope_version,
        json.dumps(jsonable(span.attributes)),
        json.dumps(jsonable(span.resource)),
        json.dumps(
            [
                {"name": e.name, "time_ns": e.time_ns, "attributes": jsonable(e.attributes)}
                for e in span.events
            ]
        ),
        json.dumps(
            [
                {"trace_id": k.trace_id, "span_id": k.span_id, "attributes": jsonable(k.attributes)}
                for k in span.links
            ]
        ),
        *_derived(fields)[:-1],
        received_ns,
        fields.user_id,
    )


def _derived(fields: genai.GenAI) -> tuple[Any, ...]:
    """Values for the _DERIVED columns, in order."""
    return (
        fields.operation,
        fields.provider,
        fields.request_model,
        fields.response_model,
        fields.input_tokens,
        fields.output_tokens,
        fields.cache_read_tokens,
        fields.cache_write_tokens,
        fields.reasoning_tokens,
        fields.cost_usd,
        list(fields.finish_reasons),
        fields.tool_name,
        fields.agent_name,
        fields.conversation_id,
        fields.error_type,
        fields.is_model_call,
        fields.counts_toward_totals,
        fields.user_id,
    )


def _stored(row: Sequence[Any]) -> StoredSpan:
    events: list[dict[str, Any]] = json.loads(row[14])
    links: list[dict[str, Any]] = json.loads(row[15])
    attributes: Attributes = json.loads(row[12])
    span = Span(
        trace_id=row[0],
        span_id=row[1],
        parent_span_id=row[2],
        name=row[3],
        kind=int(row[4]),
        start_ns=int(row[5]),
        end_ns=int(row[6]),
        status_code=int(row[7]),
        status_message=row[8],
        scope_name=row[10],
        scope_version=row[11],
        attributes=attributes,
        resource=json.loads(row[13]),
        events=tuple(
            SpanEvent(name=e["name"], time_ns=int(e["time_ns"]), attributes=e["attributes"])
            for e in events
        ),
        links=tuple(
            SpanLink(trace_id=k["trace_id"], span_id=k["span_id"], attributes=k["attributes"])
            for k in links
        ),
    )
    return StoredSpan(span=span, fields=genai.read(span))
