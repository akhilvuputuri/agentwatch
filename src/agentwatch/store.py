"""DuckDB storage for spans, with trace summaries computed at query time."""

from __future__ import annotations

import contextlib
import json
import logging
import re
import tempfile
import threading
from collections.abc import Generator, Iterable, Sequence
from dataclasses import astuple, dataclass, replace
from pathlib import Path
from typing import Any

import duckdb

from agentwatch import genai
from agentwatch.scores import Score, ScoreConflict, from_span_events
from agentwatch.spans import STATUS_ERROR, Attributes, Span, SpanEvent, SpanLink, jsonable

SCHEMA_VERSION = 9

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
    3: (
        """CREATE TABLE scores (
            id VARCHAR PRIMARY KEY,
            trace_id VARCHAR NOT NULL,
            span_id VARCHAR,
            name VARCHAR NOT NULL,
            data_type VARCHAR NOT NULL,
            value DOUBLE,
            label VARCHAR,
            source VARCHAR NOT NULL,
            comment VARCHAR,
            created_ns BIGINT NOT NULL
        )""",
        "CREATE INDEX scores_trace ON scores (trace_id)",
    ),
    4: (
        "ALTER TABLE spans ADD COLUMN cost_source VARCHAR",
        "UPDATE spans SET cost_source = 'reported' WHERE cost_usd IS NOT NULL",
        """CREATE TABLE prices (
            id VARCHAR PRIMARY KEY,
            match_pattern VARCHAR NOT NULL,
            input_per_million DOUBLE NOT NULL,
            output_per_million DOUBLE NOT NULL,
            cache_read_per_million DOUBLE,
            cache_write_per_million DOUBLE,
            start_ns BIGINT,
            created_ns BIGINT NOT NULL
        )""",
    ),
    5: (
        """CREATE TABLE datasets (
            id VARCHAR PRIMARY KEY,
            name VARCHAR NOT NULL UNIQUE,
            description VARCHAR,
            metadata JSON NOT NULL,
            created_ns BIGINT NOT NULL
        )""",
        """CREATE TABLE dataset_items (
            id VARCHAR PRIMARY KEY,
            dataset_id VARCHAR NOT NULL,
            input JSON,
            expected_output JSON,
            metadata JSON NOT NULL,
            source_trace_id VARCHAR,
            source_span_id VARCHAR,
            status VARCHAR NOT NULL,
            created_ns BIGINT NOT NULL
        )""",
        "CREATE INDEX dataset_items_dataset ON dataset_items (dataset_id)",
    ),
    6: (
        """CREATE TABLE runs (
            id VARCHAR PRIMARY KEY,
            dataset_id VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            description VARCHAR,
            metadata JSON NOT NULL,
            created_ns BIGINT NOT NULL,
            UNIQUE (dataset_id, name)
        )""",
        """CREATE TABLE run_items (
            run_id VARCHAR NOT NULL,
            item_id VARCHAR NOT NULL,
            trace_id VARCHAR NOT NULL,
            output JSON,
            error VARCHAR,
            start_ns BIGINT NOT NULL,
            end_ns BIGINT NOT NULL,
            created_ns BIGINT NOT NULL,
            PRIMARY KEY (run_id, item_id)
        )""",
    ),
    7: (
        """CREATE TABLE judges (
            id VARCHAR PRIMARY KEY,
            name VARCHAR NOT NULL UNIQUE,
            model VARCHAR NOT NULL,
            base_url VARCHAR NOT NULL,
            api_key_env VARCHAR,
            prompt VARCHAR NOT NULL,
            score_type VARCHAR NOT NULL,
            sample_rate DOUBLE NOT NULL,
            daily_limit INTEGER NOT NULL,
            service VARCHAR,
            enabled BOOLEAN NOT NULL,
            created_ns BIGINT NOT NULL,
            enabled_ns BIGINT
        )""",
        """CREATE TABLE judgements (
            judge_id VARCHAR NOT NULL,
            trace_id VARCHAR NOT NULL,
            outcome VARCHAR NOT NULL,
            value DOUBLE,
            detail VARCHAR,
            input_tokens BIGINT,
            output_tokens BIGINT,
            cost_usd DOUBLE,
            created_ns BIGINT NOT NULL,
            PRIMARY KEY (judge_id, trace_id)
        )""",
    ),
    8: ("ALTER TABLE judges ADD COLUMN sessions_only BOOLEAN NOT NULL DEFAULT false",),
    9: (
        """CREATE TABLE monitors (
            id VARCHAR PRIMARY KEY,
            name VARCHAR NOT NULL UNIQUE,
            metric VARCHAR NOT NULL,
            score_name VARCHAR,
            service VARCHAR,
            window_minutes INTEGER NOT NULL,
            comparison VARCHAR NOT NULL,
            threshold DOUBLE NOT NULL,
            min_samples INTEGER NOT NULL,
            enabled BOOLEAN NOT NULL,
            created_ns BIGINT NOT NULL
        )""",
        """CREATE TABLE monitor_states (
            monitor_id VARCHAR PRIMARY KEY,
            state VARCHAR NOT NULL,
            value DOUBLE,
            bound DOUBLE,
            samples BIGINT NOT NULL,
            evaluated_ns BIGINT NOT NULL,
            since_ns BIGINT NOT NULL,
            insufficient BOOLEAN NOT NULL
        )""",
        """CREATE TABLE alerts (
            id VARCHAR PRIMARY KEY,
            monitor_id VARCHAR NOT NULL,
            monitor_name VARCHAR NOT NULL,
            kind VARCHAR NOT NULL,
            metric VARCHAR NOT NULL,
            comparison VARCHAR NOT NULL,
            threshold DOUBLE NOT NULL,
            value DOUBLE NOT NULL,
            bound DOUBLE,
            samples BIGINT NOT NULL,
            window_minutes INTEGER NOT NULL,
            created_ns BIGINT NOT NULL,
            delivered_ns BIGINT
        )""",
    ),
}
REPORTED = "reported"
CALCULATED = "calculated"
_PRICE_COLUMNS = (
    "id, match_pattern, input_per_million, output_per_million, cache_read_per_million, "
    "cache_write_per_million, start_ns, created_ns"
)

# Cost of a model call from a price row: fresh input, cache reads and cache writes (each at
# its own price, or the input price when none is set) and output. Input tokens include the
# cached ones, as the GenAI conventions and OpenInference both define them; when the cache
# counts exceed input, the instrumentation reported fresh input only (as some Anthropic
# instrumentations do), so input is then taken as fresh. Reasoning tokens are part of output.
_COST = """(
    CASE
        WHEN coalesce(s.cache_read_tokens, 0) + coalesce(s.cache_write_tokens, 0)
             > coalesce(s.input_tokens, 0)
        THEN coalesce(s.input_tokens, 0)
        ELSE coalesce(s.input_tokens, 0) - coalesce(s.cache_read_tokens, 0)
             - coalesce(s.cache_write_tokens, 0)
    END * p.input_per_million
    + coalesce(s.cache_read_tokens, 0) * coalesce(p.cache_read_per_million, p.input_per_million)
    + coalesce(s.cache_write_tokens, 0) * coalesce(p.cache_write_per_million, p.input_per_million)
    + coalesce(s.output_tokens, 0) * p.output_per_million
) / 1e6"""
_SCORE_COLUMNS = "id, trace_id, span_id, name, data_type, value, label, source, comment, created_ns"

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
    # "reported" when the span carried a cost; "calculated" once a model price applied.
    "cost_source",
)
_DERIVED_TYPES = {
    "trace_id": "VARCHAR",
    "span_id": "VARCHAR",
    "operation": "VARCHAR",
    "provider": "VARCHAR",
    "request_model": "VARCHAR",
    "response_model": "VARCHAR",
    "input_tokens": "BIGINT",
    "output_tokens": "BIGINT",
    "cache_read_tokens": "BIGINT",
    "cache_write_tokens": "BIGINT",
    "reasoning_tokens": "BIGINT",
    "cost_usd": "DOUBLE",
    "finish_reasons": "VARCHAR[]",
    "tool_name": "VARCHAR",
    "agent_name": "VARCHAR",
    "conversation_id": "VARCHAR",
    "error_type": "VARCHAR",
    "is_model_call": "BOOLEAN",
    "counts_toward_totals": "BOOLEAN",
    "user_id": "VARCHAR",
    "cost_source": "VARCHAR",
}
_REDERIVE_CHUNK = 5000
log = logging.getLogger("agentwatch")

# Stored as received, then derived (in _DERIVED order). _row and _stored follow this order.
_BASE_COLUMNS = (
    "trace_id",
    "span_id",
    "parent_span_id",
    "name",
    "kind",
    "start_ns",
    "end_ns",
    "status_code",
    "status_message",
    "service_name",
    "scope_name",
    "scope_version",
    "attributes",
    "resource",
    "events",
    "links",
    "received_ns",
)
_COLUMNS = ", ".join((*_BASE_COLUMNS, *_DERIVED))
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
    session_id: str | None = None
    user_id: str | None = None
    scores: tuple[ScoreAggregate, ...] = ()

    @property
    def cursor(self) -> Cursor:
        return Cursor(self.start_ns, self.trace_id)


@dataclass(frozen=True, slots=True)
class Price:
    """A model price, in US dollars per million tokens."""

    id: str
    match_pattern: str
    input_per_million: float
    output_per_million: float
    cache_read_per_million: float | None
    cache_write_per_million: float | None
    start_ns: int | None
    created_ns: int


@dataclass(frozen=True, slots=True)
class ScoreAggregate:
    """One score name on a trace: how many, their mean (numeric and boolean), commonest label."""

    name: str
    data_type: str
    count: int
    mean: float | None
    label: str | None


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    traces: int
    first_start_ns: int
    last_start_ns: int
    last_end_ns: int
    error_count: int
    model_calls: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    calls_without_cost: int
    users: tuple[str, ...]
    services: tuple[str, ...]

    @property
    def cursor(self) -> SessionCursor:
        return SessionCursor(self.last_start_ns, self.session_id)


@dataclass(frozen=True, slots=True)
class SessionCursor:
    """A position in the session list: most recent trace start descending, then session ID."""

    last_start_ns: int
    session_id: str

    def encode(self) -> str:
        return f"{self.last_start_ns}:{self.session_id}"

    @staticmethod
    def decode(value: str) -> SessionCursor:
        start, separator, session_id = value.partition(":")
        if not separator or not start.isascii() or not start.isdigit() or len(start) > 19:
            raise ValueError("cursor must be <start_ns>:<session_id>")
        return SessionCursor(int(start), session_id)


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
    # The stored cost: the reported one, or one calculated from a model price.
    cost_usd: float | None = None
    cost_source: str | None = None


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


# One row per trace: totals, root, and the trace's session and user (from its root span when
# set there, otherwise from the earliest span that has one).
SUMMARIES_SQL = f"""
{ROOTS_SQL},
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
            FILTER (WHERE is_model_call AND coalesce(response_model, request_model) IS NOT NULL) AS models,
        arg_min(conversation_id, (parent_span_id IS NOT NULL, start_ns, span_id))
            FILTER (WHERE conversation_id IS NOT NULL) AS session_id,
        arg_min(user_id, (parent_span_id IS NOT NULL, start_ns, span_id))
            FILTER (WHERE user_id IS NOT NULL) AS user_id
    FROM spans GROUP BY trace_id
),
score_labels AS (
    SELECT trace_id, name, label, count(*) AS n
    FROM scores WHERE label IS NOT NULL GROUP BY trace_id, name, label
),
top_labels AS (
    -- The commonest label; ties go to the alphabetically first.
    SELECT trace_id, name, arg_max(label, (n, -rank)) AS label
    FROM (SELECT *, rank() OVER (PARTITION BY trace_id, name ORDER BY label) AS rank FROM score_labels)
    GROUP BY trace_id, name
),
score_names AS (
    SELECT s.trace_id, s.name, any_value(s.data_type) AS data_type, count(*) AS n,
           avg(s.value) AS mean, any_value(l.label) AS label
    FROM scores s LEFT JOIN top_labels l USING (trace_id, name)
    GROUP BY s.trace_id, s.name
),
trace_scores AS (
    SELECT trace_id,
           list(struct_pack(name := name, data_type := data_type, count := n, mean := mean, label := label)
                ORDER BY name) AS scores
    FROM score_names GROUP BY trace_id
),
summaries AS (
    SELECT t.*, r.root_name, r.service_name, r.complete, coalesce(ts.scores, []) AS scores
    FROM totals t JOIN roots r USING (trace_id) LEFT JOIN trace_scores ts USING (trace_id)
)
"""


class Store:
    """A single-process store. All access is serialized through one lock."""

    def __init__(self, path: str | Path | None = None, *, memory_limit: str | None = None) -> None:
        target = ":memory:" if path is None else str(path)
        if path is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Scratch files (re-derivation) go next to the database, not in a small /tmp.
        self._scratch = None if path is None else Path(path).parent
        self._lock = threading.Lock()
        self._db = duckdb.connect(target)
        # A library must not draw progress bars on the server's stderr.
        self._db.execute("SET enable_progress_bar = false")
        if memory_limit is not None:
            # A shared host: cap DuckDB's buffer memory (it spills to disk beyond this).
            self._db.execute(f"SET memory_limit = '{_memory(memory_limit)}'")
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
        """Re-read every stored span's attributes when the extraction rules have changed.

        One scan reads only the attribute keys genai.read uses, a chunk at a time, and the new
        values go to a newline-delimited JSON file that DuckDB loads in one UPDATE. Python's
        memory stays proportional to a chunk, and full prompts never leave DuckDB.
        """
        current = str(genai.DERIVATION_VERSION)
        if self._meta("derivation") == current:
            return
        total_row = self._db.execute("SELECT count(*) FROM spans").fetchone()
        total = 0 if total_row is None else int(total_row[0])
        if total:
            log.info("re-reading %d stored spans for extraction rules version %s", total, current)
        types = ", ".join(f"'{name}': '{kind}'" for name, kind in _DERIVED_TYPES.items())
        # Only the keys genai.read looks at, so large attributes (full prompts) stay in DuckDB.
        keys = genai.attribute_keys()
        paths = "[" + ", ".join("'$.\"" + key + "\"'" for key in keys) + "]"
        assignments = ", ".join(f"{column} = d.{column}" for column in _DERIVED)
        # Only rows whose values change are rewritten; a rule change usually touches few.
        changed = " OR ".join(f"spans.{column} IS DISTINCT FROM d.{column}" for column in _DERIVED)
        with tempfile.TemporaryDirectory(prefix="agentwatch-", dir=self._scratch) as folder:
            path = Path(folder) / "derived.ndjson"
            with path.open("w", encoding="utf-8") as out:
                # One scan, fetched a chunk at a time from DuckDB's streaming result.
                result = self._db.execute(
                    f"SELECT trace_id, span_id, json_extract(attributes, {paths}) FROM spans"
                )
                while rows := result.fetchmany(_REDERIVE_CHUNK):
                    for trace_id, span_id, extracted in rows:
                        attributes: Attributes = {
                            key: json.loads(value)
                            for key, value in zip(keys, extracted, strict=True)
                            if value is not None
                        }
                        # Only the attributes matter: genai.read looks at nothing else.
                        span = Span(trace_id, span_id, None, "", 0, 0, attributes=attributes)
                        values = dict(zip(_DERIVED, _derived(genai.read(span)), strict=True))
                        out.write(json.dumps({"trace_id": trace_id, "span_id": span_id, **values}))
                        out.write("\n")
            self._db.execute("BEGIN")
            try:
                if total:
                    self._db.execute(
                        f"UPDATE spans SET {assignments} "
                        f"FROM read_json(?, format = 'newline_delimited', columns = {{{types}}}) d "
                        "WHERE spans.trace_id = d.trace_id AND spans.span_id = d.span_id "
                        f"AND ({changed})",
                        [str(path)],
                    )
                self._apply_prices()
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
        spans = list(spans)
        if not spans:
            return 0
        with self.transaction():
            return self.write_spans(spans, received_ns)

    def write_spans(self, spans: Iterable[Span], received_ns: int) -> int:
        """insert() without its own transaction: call only inside `with store.transaction()`."""
        latest: dict[tuple[str, str], Span] = {}
        for span in spans:
            latest[(span.trace_id, span.span_id)] = span
        rows = [_row(span, received_ns) for span in latest.values()]
        if not rows:
            return 0
        event_scores = [score for span in latest.values() for score in from_span_events(span)]
        # One multi-row statement per chunk is several times faster than executemany.
        for start in range(0, len(rows), _INSERT_CHUNK):
            chunk = rows[start : start + _INSERT_CHUNK]
            values = ", ".join(f"({_PLACEHOLDERS})" for _ in chunk)
            self._db.execute(
                f"INSERT OR REPLACE INTO spans ({_COLUMNS}) VALUES {values}",
                [value for row in chunk for value in row],
            )
        self._write_scores(self._without_conflicts(event_scores))
        self._apply_prices(sorted({span.trace_id for span in latest.values()}))
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

    @contextlib.contextmanager
    def transaction(self) -> Generator[duckdb.DuckDBPyConnection]:
        """The connection, under the store's lock, in a transaction committed on success."""
        with self._lock:
            self._db.execute("BEGIN")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(duckdb.Error):
                    self._db.execute("ROLLBACK")
                raise

    def prune(self, before_ns: int) -> int:
        """Deletes traces last received before `before_ns`, with their scores and judgements,
        and every alert created before it, delivered or not.

        Age is the server's own arrival time, not the exporter's span times, so a client with
        a wrong clock (or microseconds sent as nanoseconds) cannot get a new trace deleted.
        Traces and scores of experiment runs are kept, since their runs still show them.
        Scores and judgements older than the cutoff whose trace is gone (or never arrived) go
        too. Returns the number of traces deleted.
        """
        with self.transaction() as db:
            db.execute(
                """CREATE OR REPLACE TEMP TABLE pruned AS
                SELECT trace_id FROM spans GROUP BY trace_id
                HAVING max(received_ns) < ? AND trace_id NOT IN (SELECT trace_id FROM run_items)""",
                [before_ns],
            )
            count = db.execute("SELECT count(*) FROM pruned").fetchone()
            db.execute("DELETE FROM spans WHERE trace_id IN (SELECT trace_id FROM pruned)")
            for table in ("scores", "judgements"):
                db.execute(
                    f"DELETE FROM {table} WHERE created_ns < ? "
                    "AND trace_id NOT IN (SELECT DISTINCT trace_id FROM spans) "
                    "AND trace_id NOT IN (SELECT trace_id FROM run_items)",
                    [before_ns],
                )
            db.execute("DROP TABLE pruned")
            db.execute("DELETE FROM alerts WHERE created_ns < ?", [before_ns])
        return count[0] if count else 0

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
        session: str | None = None,
        user: str | None = None,
        trace: str | None = None,
    ) -> list[TraceSummary]:
        """Most recent traces first. Page with the last trace's `cursor` as `before`."""
        filters: list[str] = []
        params: list[Any] = []
        if trace is not None:
            filters.append("trace_id = ?")
            params.append(trace)
        if service is not None:
            filters.append("service_name = ?")
            params.append(service)
        if session is not None:
            filters.append("session_id = ?")
            params.append(session)
        if user is not None:
            filters.append("user_id = ?")
            params.append(user)
        if before is not None:
            # Traces sharing a start time are ordered by ID, so the cursor needs both.
            filters.append("(start_ns < ? OR (start_ns = ? AND trace_id > ?))")
            params.extend([before.start_ns, before.start_ns, before.trace_id])
        if errors_only:
            filters.append("error_count > 0")
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        params.append(max(1, min(limit, 500)))
        query = f"""
            WITH {SUMMARIES_SQL}
            SELECT trace_id, root_name, service_name, start_ns, end_ns - start_ns, span_count,
                   error_count, model_calls, tool_calls, input_tokens, output_tokens, cache_read,
                   cost_usd, calls_without_cost, coalesce(models, []), complete, session_id, user_id,
                   scores
            FROM summaries {where}
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
                session_id=row[16],
                user_id=row[17],
                scores=tuple(
                    ScoreAggregate(
                        name=item["name"],
                        data_type=item["data_type"],
                        count=int(item["count"]),
                        mean=None if item["mean"] is None else float(item["mean"]),
                        label=item["label"],
                    )
                    for item in row[18]
                ),
            )
            for row in rows
        ]

    def sessions(
        self,
        *,
        limit: int = 50,
        before: SessionCursor | None = None,
        service: str | None = None,
        user: str | None = None,
        session: str | None = None,
    ) -> list[SessionSummary]:
        """Sessions with the most recent activity first, paged by `cursor`."""
        filters = ["session_id IS NOT NULL"]
        params: list[Any] = []
        if session is not None:
            filters.append("session_id = ?")
            params.append(session)
        if service is not None:
            filters.append("service_name = ?")
            params.append(service)
        if user is not None:
            filters.append("user_id = ?")
            params.append(user)
        having = ""
        if before is not None:
            having = "HAVING max(start_ns) < ? OR (max(start_ns) = ? AND session_id > ?)"
            params.extend([before.last_start_ns, before.last_start_ns, before.session_id])
        params.append(max(1, min(limit, 500)))
        query = f"""
            WITH {SUMMARIES_SQL}
            SELECT session_id, count(*), min(start_ns), max(start_ns), max(end_ns),
                   sum(error_count), sum(model_calls), sum(tool_calls), sum(input_tokens),
                   sum(output_tokens), sum(cost_usd), sum(calls_without_cost),
                   list(DISTINCT user_id ORDER BY user_id) FILTER (WHERE user_id IS NOT NULL),
                   list(DISTINCT service_name ORDER BY service_name)
            FROM summaries WHERE {" AND ".join(filters)}
            GROUP BY session_id {having}
            ORDER BY max(start_ns) DESC, session_id
            LIMIT ?
        """
        with self._lock:
            rows = self._db.execute(query, params).fetchall()
        return [
            SessionSummary(
                session_id=row[0],
                traces=int(row[1]),
                first_start_ns=int(row[2]),
                last_start_ns=int(row[3]),
                last_end_ns=int(row[4]),
                error_count=int(row[5]),
                model_calls=int(row[6]),
                tool_calls=int(row[7]),
                input_tokens=int(row[8]),
                output_tokens=int(row[9]),
                cost_usd=None if row[10] is None else float(row[10]),
                calls_without_cost=int(row[11]),
                users=tuple(row[12] or ()),
                services=tuple(row[13] or ()),
            )
            for row in rows
        ]

    def upsert_scores(self, found: Sequence[Score]) -> int:
        """Store scores; one with an existing ID replaces it, if it is on the same trace.

        Raises ScoreConflict, storing nothing, when an ID is already used on another trace.
        """
        if not found:
            return 0
        with self.transaction():
            return self.write_scores(found)

    def write_scores(self, found: Sequence[Score]) -> int:
        """upsert_scores() without its own transaction: call only inside `store.transaction()`."""
        self._check_conflicts(found)
        self._write_scores(found)
        return len(found)

    def _check_conflicts(self, found: Sequence[Score]) -> None:
        traces: dict[str, str] = {}
        for score in found:
            if traces.setdefault(score.id, score.trace_id) != score.trace_id:
                raise ScoreConflict(f"score id {score.id!r} is given for two traces")
        taken = self._ids_on_other_traces(traces)
        if taken:
            raise ScoreConflict(f"score id {taken[0]!r} is already used on another trace")

    def _ids_on_other_traces(self, traces: dict[str, str]) -> list[str]:
        """IDs (of id → trace) already stored on a different trace."""
        ids = list(traces)
        taken: list[str] = []
        for start in range(0, len(ids), _INSERT_CHUNK):
            chunk = ids[start : start + _INSERT_CHUNK]
            rows = self._db.execute(
                f"SELECT id, trace_id FROM scores WHERE id IN ({', '.join('?' for _ in chunk)})",
                chunk,
            ).fetchall()
            taken.extend(score_id for score_id, trace_id in rows if traces[score_id] != trace_id)
        return taken

    def _without_conflicts(self, found: Sequence[Score]) -> list[Score]:
        """Event scores minus any whose ID is already used on another trace."""
        if not found:
            return []
        taken = set(self._ids_on_other_traces({s.id: s.trace_id for s in found}))
        return [s for s in found if s.id not in taken]

    def insert_event_scores(self, found: Sequence[Score]) -> int:
        """Store evaluation-event scores, skipping any whose ID is used on another trace.

        Returns how many were skipped.
        """
        with self._lock:
            self._db.execute("BEGIN")
            try:
                kept = self._without_conflicts(found)
                self._write_scores(kept)
                self._db.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(duckdb.Error):
                    self._db.execute("ROLLBACK")
                raise
        return len(found) - len(kept)

    def _write_scores(self, found: Sequence[Score]) -> None:
        latest = {score.id: score for score in found}
        rows = [astuple(score) for score in latest.values()]
        for start in range(0, len(rows), _INSERT_CHUNK):
            chunk = rows[start : start + _INSERT_CHUNK]
            marks = ", ".join("(" + ", ".join("?" * 10) + ")" for _ in chunk)
            self._db.execute(
                f"INSERT OR REPLACE INTO scores ({_SCORE_COLUMNS}) VALUES {marks}",
                [value for row in chunk for value in row],
            )

    def prices(self) -> list[Price]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_PRICE_COLUMNS} FROM prices ORDER BY match_pattern, start_ns NULLS FIRST, id"
            ).fetchall()
        return [Price(*row) for row in rows]

    def check_pattern(self, pattern: str) -> str | None:
        """None when DuckDB accepts the regular expression, else its error message."""
        with self._lock:
            try:
                self._db.execute("SELECT regexp_full_match('', ?)", [pattern]).fetchone()
            except duckdb.Error as error:
                return str(error).splitlines()[0]
        return None

    def upsert_price(self, price: Price) -> None:
        """Add or replace a price, then recalculate every calculated cost."""
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.execute(
                    f"INSERT OR REPLACE INTO prices ({_PRICE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    astuple(price),
                )
                self._apply_prices()
                self._db.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(duckdb.Error):
                    self._db.execute("ROLLBACK")
                raise

    def delete_price(self, price_id: str) -> bool:
        with self._lock:
            self._db.execute("BEGIN")
            try:
                row = self._db.execute(
                    "DELETE FROM prices WHERE id = ? RETURNING id", [price_id]
                ).fetchone()
                self._apply_prices()
                self._db.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(duckdb.Error):
                    self._db.execute("ROLLBACK")
                raise
        return row is not None

    def _apply_prices(self, traces: Sequence[str] | None = None) -> None:
        """Set the cost of model calls that reported none from the best matching price.

        A price matches when its pattern matches the whole model name (case-insensitively) and
        it took effect by the span's start. Of the matches, the longest (most specific) pattern
        wins; among that pattern's prices, the latest to take effect, then the most recently
        added. Calls with no
        match have no cost. Reported costs are never touched. `traces` limits the work to
        those traces; None recalculates everything.
        """
        scope = ""
        params: list[Any] = []
        if traces is not None:
            if not traces:
                return
            scope = f"AND s.trace_id IN ({', '.join('?' for _ in traces)})"
            params = list(traces)
        self._db.execute(
            f"UPDATE spans s SET cost_usd = NULL, cost_source = NULL "
            f"WHERE s.cost_source = '{CALCULATED}' {scope}",
            params,
        )
        self._db.execute(
            f"""
            WITH targets AS (
                SELECT s.trace_id, s.span_id, s.start_ns, s.input_tokens, s.output_tokens,
                       s.cache_read_tokens, s.cache_write_tokens,
                       coalesce(s.response_model, s.request_model) AS model
                FROM spans s
                WHERE s.is_model_call AND s.cost_source IS NULL
                  AND (s.input_tokens IS NOT NULL OR s.output_tokens IS NOT NULL)
                  AND coalesce(s.response_model, s.request_model) IS NOT NULL {scope}
            ),
            -- Regular expressions run once per distinct model name, not once per call.
            matched AS (
                SELECT m.model, p.*
                FROM (SELECT DISTINCT model FROM targets) m
                JOIN prices p ON regexp_full_match(m.model, p.match_pattern, 'i')
            ),
            costs AS (
                SELECT s.trace_id, s.span_id,
                       arg_max({_COST}, (length(p.match_pattern), coalesce(p.start_ns, -1),
                                         p.created_ns, p.id)) AS cost
                FROM targets s JOIN matched p USING (model)
                WHERE p.start_ns IS NULL OR p.start_ns <= s.start_ns
                GROUP BY s.trace_id, s.span_id
            )
            UPDATE spans SET cost_usd = c.cost, cost_source = '{CALCULATED}'
            FROM costs c
            WHERE spans.trace_id = c.trace_id AND spans.span_id = c.span_id
            """,
            params,
        )

    def delete_score(self, score_id: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "DELETE FROM scores WHERE id = ? RETURNING id", [score_id]
            ).fetchone()
        return row is not None

    def scores(self, trace_id: str) -> list[Score]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_SCORE_COLUMNS} FROM scores WHERE trace_id = ? ORDER BY created_ns, id",
                [trace_id.lower()],
            ).fetchall()
        return [Score(*row) for row in rows]

    def trace(self, trace_id: str) -> list[StoredSpan]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_COLUMNS} FROM spans WHERE trace_id = ? ORDER BY start_ns, span_id",
                [trace_id.lower()],
            ).fetchall()
        return [_stored(row) for row in rows]


def _memory(value: str) -> str:
    """A DuckDB memory size such as 256MB or 1GiB, checked so it can be inlined in SQL."""
    match = re.fullmatch(r"(\d{1,6}(?:\.\d{1,3})?)\s*([KMGT])(i?)B", value.strip(), re.IGNORECASE)
    if not match:
        raise ValueError(f"memory limit must look like 256MB or 1GiB, not {value!r}")
    unit = (1024 if match.group(3) else 1000) ** ("KMGT".index(match.group(2).upper()) + 1)
    if float(match.group(1)) * unit < 64 * 1000**2:
        raise ValueError("memory limit must be at least 64MB")
    return value.strip()


def _row(span: Span, received_ns: int) -> Sequence[Any]:
    # Read the attributes in the form they are stored (bytes as hex, NaN as "nan"), so a
    # later re-derivation from storage gives exactly the same values.
    stored_attributes: Attributes = jsonable(span.attributes)
    fields = genai.read(replace(span, attributes=stored_attributes))
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
        received_ns,
        *_derived(fields),
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
        REPORTED if fields.cost_usd is not None else None,
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
    derived = dict(zip(_DERIVED, row[len(_BASE_COLUMNS) :], strict=True))
    return StoredSpan(
        span=span,
        fields=genai.read(span),
        cost_usd=derived["cost_usd"],
        cost_source=derived["cost_source"],
    )
