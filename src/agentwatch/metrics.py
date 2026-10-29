"""Cost, token and latency figures over a time window, for the dashboard.

These are descriptive: counts, sums and percentiles of what was received. Comparisons
with intervals and verdicts belong to the statistics engine, not here.

Two populations, both limited to traces whose root service matches the filter:

- call figures (model and tool calls, tokens, cost, latency) cover spans that *started*
  in the window;
- trace figures (count, duration, cost per trace) cover traces whose *root* started in
  the window, with all of their spans, including any that ran past its end.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agentwatch.otlp import MAX_TIME_NS
from agentwatch.spans import STATUS_ERROR
from agentwatch.store import ROOTS_SQL, Store

WEEK_NS = 7 * 24 * 3600 * 1_000_000_000
# Bucket widths the time series may use, smallest first.
BUCKETS_NS = tuple(
    minutes * 60 * 1_000_000_000
    for minutes in (1, 5, 15, 60, 3 * 60, 6 * 60, 12 * 60, 24 * 60, 7 * 24 * 60)
)
MAX_BUCKETS = 72
# Ancestor hops searched for a span's agent; deeper trees are left unattributed.
MAX_DEPTH = 64
# Duration of a span, never negative (an exporter can send an end before the start).
_DURATION_MS = "greatest(end_ns - start_ns, 0) / 1e6"
# Calls with no invoke_agent ancestor. Real agent names never carry brackets in practice;
# a name that does is still kept apart, because owned calls are grouped by a flag.
NO_AGENT = "(no agent)"


@dataclass(frozen=True, slots=True)
class Window:
    start_ns: int
    end_ns: int
    service: str | None = None

    def bucket_ns(self) -> int:
        """The smallest width giving at most MAX_BUCKETS buckets once the first is aligned."""
        for width in BUCKETS_NS:
            if self._count(width) <= MAX_BUCKETS:
                return width
        # Longer than about 72 weeks: whole multiples of a week.
        weeks = -(-(self.end_ns - self.start_ns) // (WEEK_NS * (MAX_BUCKETS - 1)))
        return WEEK_NS * max(1, weeks)

    def first_bucket_ns(self, width: int) -> int:
        """Buckets start on whole multiples of their width since the Unix epoch (UTC)."""
        return self.start_ns - self.start_ns % width

    def _count(self, width: int) -> int:
        return max(1, -(-(self.end_ns - self.first_bucket_ns(width)) // width))


# Traces whose root service matches the filter (every trace without one).
# Parameters 1 and 2: the service, twice.
_TRACES = """
    traces AS (
        SELECT trace_id, root_start_ns FROM roots WHERE (?1 IS NULL OR service_name = ?2)
    )
"""


def data_range(store: Store, service: str | None = None) -> tuple[int, int] | None:
    """Earliest start and latest start or end of stored spans, or None when there are none."""
    row = store.query_one(
        f"WITH {ROOTS_SQL}, {_TRACES} "
        "SELECT min(start_ns), max(greatest(start_ns, end_ns)) FROM spans "
        "WHERE trace_id IN (SELECT trace_id FROM traces)",
        [service, service],
    )
    if row is None or row[0] is None:
        return None
    return int(row[0]), int(row[1])


def window_for_all(store: Store, service: str | None, now_ns: int) -> Window:
    """A window covering every stored span, or the last day when there are none."""
    bounds = data_range(store, service)
    if bounds is None:
        return Window(max(0, now_ns - 24 * 3600 * 10**9), now_ns, service)
    start, end = bounds
    return Window(start, min(end + 1, MAX_TIME_NS), service)


def metrics(store: Store, window: Window) -> dict[str, Any]:
    bucket = window.bucket_ns()
    params = _params(window)
    return {
        "window": {
            "start_ns": str(window.start_ns),
            "end_ns": str(window.end_ns),
            "bucket_ns": bucket,
            "service": window.service,
        },
        "overview": _overview(store, params),
        "by_model": _models(store, params),
        "by_tool": _rows(store, _BY_TOOL, params),
        "by_agent": _rows(store, _BY_AGENT.replace("{depth}", str(MAX_DEPTH)), params),
        "series": _series(store, window, bucket),
    }


def _params(window: Window) -> list[Any]:
    return [window.service, window.service, window.start_ns, window.end_ns]


# Parameters: service, service, window start, window end.
_SCOPE = f"""
    {ROOTS_SQL}, {_TRACES},
    windowed AS (
        SELECT * FROM spans
        WHERE start_ns >= ?3 AND start_ns < ?4 AND trace_id IN (SELECT trace_id FROM traces)
    ),
    started AS (
        SELECT trace_id, root_start_ns FROM traces
        WHERE root_start_ns >= ?3 AND root_start_ns < ?4
    )
"""

_OVERVIEW = f"""
    WITH {_SCOPE},
    per_trace AS (
        SELECT s.trace_id,
               greatest(max(s.end_ns) - min(s.start_ns), 0) AS duration_ns,
               sum(s.cost_usd) FILTER (WHERE s.counts_toward_totals) AS cost_usd
        FROM spans s JOIN started USING (trace_id)
        GROUP BY s.trace_id
    )
    SELECT
        (SELECT count(*) FROM started) AS traces,
        count(*) FILTER (WHERE is_model_call) AS model_calls,
        count(*) FILTER (WHERE is_model_call AND status_code = {STATUS_ERROR}) AS model_errors,
        count(*) FILTER (WHERE operation = 'execute_tool') AS tool_calls,
        count(*) FILTER (WHERE operation = 'execute_tool' AND status_code = {STATUS_ERROR}) AS tool_errors,
        coalesce(sum(input_tokens) FILTER (WHERE is_model_call), 0) AS input_tokens,
        coalesce(sum(output_tokens) FILTER (WHERE is_model_call), 0) AS output_tokens,
        coalesce(sum(cache_read_tokens) FILTER (WHERE is_model_call), 0) AS cache_read_tokens,
        coalesce(sum(cache_write_tokens) FILTER (WHERE is_model_call), 0) AS cache_write_tokens,
        coalesce(sum(reasoning_tokens) FILTER (WHERE is_model_call), 0) AS reasoning_tokens,
        sum(cost_usd) FILTER (WHERE counts_toward_totals) AS cost_usd,
        count(*) FILTER (WHERE is_model_call AND cost_usd IS NULL) AS calls_without_cost,
        quantile_cont({_DURATION_MS}, 0.5) FILTER (WHERE is_model_call) AS model_p50_ms,
        quantile_cont({_DURATION_MS}, 0.95) FILTER (WHERE is_model_call) AS model_p95_ms,
        (SELECT quantile_cont(duration_ns / 1e6, 0.5) FROM per_trace) AS trace_p50_ms,
        (SELECT quantile_cont(duration_ns / 1e6, 0.95) FROM per_trace) AS trace_p95_ms,
        (SELECT quantile_cont(cost_usd, 0.5) FROM per_trace WHERE cost_usd IS NOT NULL) AS trace_cost_p50,
        (SELECT quantile_cont(cost_usd, 0.95) FROM per_trace WHERE cost_usd IS NOT NULL) AS trace_cost_p95,
        (SELECT avg(cost_usd) FROM per_trace WHERE cost_usd IS NOT NULL) AS trace_cost_mean
    FROM windowed
"""

_BY_MODEL = f"""
    WITH {_SCOPE}
    SELECT
        coalesce(response_model, request_model, '(unknown)') AS model,
        any_value(provider) AS provider,
        count(*) AS calls,
        count(*) FILTER (WHERE status_code = {STATUS_ERROR}) AS errors,
        coalesce(sum(input_tokens), 0) AS input_tokens,
        coalesce(sum(output_tokens), 0) AS output_tokens,
        coalesce(sum(cache_read_tokens), 0) AS cache_read_tokens,
        coalesce(sum(reasoning_tokens), 0) AS reasoning_tokens,
        sum(cost_usd) AS cost_usd,
        count(*) FILTER (WHERE cost_usd IS NULL) AS calls_without_cost,
        quantile_cont({_DURATION_MS}, 0.5) AS p50_ms,
        quantile_cont({_DURATION_MS}, 0.95) AS p95_ms,
        max({_DURATION_MS}) AS max_ms,
        histogram(error_type) FILTER (WHERE error_type IS NOT NULL) AS error_types
    FROM windowed WHERE is_model_call
    GROUP BY 1
    ORDER BY cost_usd DESC NULLS LAST, calls DESC, model
"""

# A call can report several finish reasons, so they are counted apart from the calls.
_FINISHES = f"""
    WITH {_SCOPE}
    SELECT model, histogram(finish) AS finish_reasons
    FROM (
        SELECT coalesce(response_model, request_model, '(unknown)') AS model,
               unnest(finish_reasons) AS finish
        FROM windowed WHERE is_model_call
    )
    GROUP BY model
"""

_BY_TOOL = f"""
    WITH {_SCOPE}
    SELECT
        coalesce(tool_name, name) AS tool,
        count(*) AS calls,
        count(*) FILTER (WHERE status_code = {STATUS_ERROR}) AS errors,
        quantile_cont({_DURATION_MS}, 0.5) AS p50_ms,
        quantile_cont({_DURATION_MS}, 0.95) AS p95_ms,
        max({_DURATION_MS}) AS max_ms,
        histogram(error_type) FILTER (WHERE error_type IS NOT NULL) AS error_types
    FROM windowed WHERE operation = 'execute_tool'
    GROUP BY 1
    ORDER BY calls DESC, tool
"""

# Model calls, tool calls and other spans that carry cost belong to their nearest
# invoke_agent ancestor, so an agent's figures are its own, not its child agents'.
_BY_AGENT = (
    "WITH RECURSIVE "
    + _SCOPE
    + f""",
    leaves AS (
        SELECT trace_id, span_id, parent_span_id FROM windowed
        WHERE is_model_call OR operation = 'execute_tool'
           OR (counts_toward_totals AND cost_usd IS NOT NULL)
    ),
    walk(trace_id, span_id, ancestor, depth, agent) AS (
        SELECT trace_id, span_id, parent_span_id, 0, NULL::VARCHAR FROM leaves
        UNION ALL
        SELECT w.trace_id, w.span_id, p.parent_span_id, w.depth + 1,
               CASE WHEN p.operation = 'invoke_agent' THEN coalesce(p.agent_name, p.name) END
        FROM walk w JOIN spans p ON p.trace_id = w.trace_id AND p.span_id = w.ancestor
        WHERE w.agent IS NULL AND w.depth < {{depth}}
    ),
    owner AS (
        SELECT trace_id, span_id, any_value(agent) AS agent
        FROM walk WHERE agent IS NOT NULL GROUP BY trace_id, span_id
    ),
    own AS (
        SELECT o.agent IS NULL AS unowned,
               coalesce(o.agent, '{NO_AGENT}') AS agent,
               count(*) FILTER (WHERE s.is_model_call) AS model_calls,
               count(*) FILTER (WHERE s.operation = 'execute_tool') AS tool_calls,
               coalesce(sum(s.input_tokens) FILTER (WHERE s.is_model_call), 0) AS input_tokens,
               coalesce(sum(s.output_tokens) FILTER (WHERE s.is_model_call), 0) AS output_tokens,
               sum(s.cost_usd) FILTER (WHERE s.counts_toward_totals) AS cost_usd
        FROM windowed s
        JOIN leaves l ON l.trace_id = s.trace_id AND l.span_id = s.span_id
        LEFT JOIN owner o ON o.trace_id = s.trace_id AND o.span_id = s.span_id
        GROUP BY 1, 2
    ),
    runs AS (
        SELECT coalesce(agent_name, name) AS agent,
               count(*) AS runs,
               count(*) FILTER (WHERE status_code = {STATUS_ERROR}) AS errors,
               quantile_cont({_DURATION_MS}, 0.5) AS p50_ms,
               quantile_cont({_DURATION_MS}, 0.95) AS p95_ms
        FROM windowed WHERE operation = 'invoke_agent'
        GROUP BY 1
    )
    SELECT coalesce(r.agent, o.agent) AS agent,
           coalesce(o.unowned, false) AS unowned,
           coalesce(r.runs, 0) AS runs, coalesce(r.errors, 0) AS errors,
           r.p50_ms, r.p95_ms,
           coalesce(o.model_calls, 0) AS model_calls, coalesce(o.tool_calls, 0) AS tool_calls,
           coalesce(o.input_tokens, 0) AS input_tokens, coalesce(o.output_tokens, 0) AS output_tokens,
           o.cost_usd
    FROM runs r FULL OUTER JOIN (SELECT * FROM own WHERE NOT unowned) o ON o.agent = r.agent
    UNION ALL
    SELECT agent, true, 0, 0, NULL, NULL, model_calls, tool_calls, input_tokens, output_tokens,
           cost_usd
    FROM own WHERE unowned
    ORDER BY cost_usd DESC NULLS LAST, runs DESC, agent
"""
)


def _series_query(bucket: int) -> str:
    # Parameter 5 is the start of the first (aligned) bucket.
    return f"""
    WITH {_SCOPE},
    calls AS (
        SELECT
            (start_ns - ?5) // {bucket} AS slot,
            count(*) FILTER (WHERE is_model_call) AS model_calls,
            count(*) FILTER (WHERE is_model_call AND status_code = {STATUS_ERROR}) AS model_errors,
            count(*) FILTER (WHERE operation = 'execute_tool') AS tool_calls,
            coalesce(sum(input_tokens) FILTER (WHERE is_model_call), 0) AS input_tokens,
            coalesce(sum(cache_read_tokens) FILTER (WHERE is_model_call), 0) AS cache_read_tokens,
            sum(cost_usd) FILTER (WHERE counts_toward_totals) AS cost_usd,
            quantile_cont({_DURATION_MS}, 0.5) FILTER (WHERE is_model_call) AS model_p50_ms,
            quantile_cont({_DURATION_MS}, 0.95) FILTER (WHERE is_model_call) AS model_p95_ms
        FROM windowed GROUP BY slot
    ),
    trace_slots AS (
        SELECT (root_start_ns - ?5) // {bucket} AS slot, count(*) AS traces
        FROM started GROUP BY slot
    )
    SELECT coalesce(c.slot, t.slot) AS slot, c.* EXCLUDE (slot), coalesce(t.traces, 0) AS traces
    FROM calls c FULL OUTER JOIN trace_slots t ON t.slot = c.slot
    """


def _series(store: Store, window: Window, bucket: int) -> list[dict[str, Any]]:
    first = window.first_bucket_ns(bucket)
    rows = store.query(_series_query(bucket), [*_params(window), first], with_names=True)
    by_slot = {int(row["slot"]): row for row in rows}
    count = max(1, -(-(window.end_ns - first) // bucket))
    series: list[dict[str, Any]] = []
    for slot in range(count):
        row = by_slot.get(slot, {})
        series.append(
            {
                "start_ns": str(first + slot * bucket),
                "model_calls": int(row.get("model_calls") or 0),
                "model_errors": int(row.get("model_errors") or 0),
                "tool_calls": int(row.get("tool_calls") or 0),
                "traces": int(row.get("traces") or 0),
                "input_tokens": int(row.get("input_tokens") or 0),
                "cache_read_tokens": int(row.get("cache_read_tokens") or 0),
                "cost_usd": _float(row.get("cost_usd")),
                "model_p50_ms": _float(row.get("model_p50_ms")),
                "model_p95_ms": _float(row.get("model_p95_ms")),
            }
        )
    return series


def _models(store: Store, params: list[Any]) -> list[dict[str, Any]]:
    finishes = {
        row["model"]: _clean(row["finish_reasons"])
        for row in store.query(_FINISHES, params, with_names=True)
    }
    rows = _rows(store, _BY_MODEL, params)
    for row in rows:
        row["finish_reasons"] = finishes.get(row["model"], {})
        row["error_types"] = row["error_types"] or {}
    return rows


def _overview(store: Store, params: list[Any]) -> dict[str, Any]:
    [row] = store.query(_OVERVIEW, params, with_names=True)
    return {key: _clean(value) for key, value in row.items()}


def _rows(store: Store, query: str, params: list[Any]) -> list[dict[str, Any]]:
    return [
        {key: _clean(value) for key, value in row.items()}
        for row in store.query(query, params, with_names=True)
    ]


def _clean(value: Any) -> Any:
    if isinstance(value, float):
        return _float(value)
    if isinstance(value, dict):
        return {str(k): int(v) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return value


def _float(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None
