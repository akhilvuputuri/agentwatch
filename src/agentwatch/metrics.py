"""Cost, token and latency figures over a time window, for the dashboard.

These are descriptive: counts, sums and percentiles of what was received. Comparisons
with intervals and verdicts belong to the statistics engine, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agentwatch.spans import STATUS_ERROR
from agentwatch.store import Store

# Bucket widths the time series may use, smallest first; the first giving at most
# MAX_BUCKETS buckets over the window is chosen.
BUCKETS_NS = tuple(
    minutes * 60 * 1_000_000_000
    for minutes in (1, 5, 15, 60, 3 * 60, 6 * 60, 12 * 60, 24 * 60, 7 * 24 * 60)
)
MAX_BUCKETS = 72
# Ancestor hops searched for a span's agent; deeper trees are left unattributed.
MAX_DEPTH = 64


@dataclass(frozen=True, slots=True)
class Window:
    start_ns: int
    end_ns: int
    service: str | None = None

    def bucket_ns(self) -> int:
        span = max(1, self.end_ns - self.start_ns)
        for width in BUCKETS_NS:
            if span / width <= MAX_BUCKETS:
                return width
        return BUCKETS_NS[-1]


def data_range(store: Store, service: str | None = None) -> tuple[int, int] | None:
    """Earliest start and latest end of stored spans, or None when there are none."""
    where, params = _service_filter(service)
    row = store.query_one(f"SELECT min(start_ns), max(end_ns) FROM spans {where}", params)
    if row is None or row[0] is None:
        return None
    return int(row[0]), int(row[1])


def metrics(store: Store, window: Window) -> dict[str, Any]:
    bucket = window.bucket_ns()
    params = _window_params(window)
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
        "by_agent": _rows(store, _BY_AGENT.format(depth=MAX_DEPTH), params),
        "series": _series(store, window, bucket),
    }


def _service_filter(service: str | None) -> tuple[str, list[Any]]:
    if service is None:
        return "", []
    return "WHERE service_name = ?", [service]


def _window_params(window: Window) -> list[Any]:
    return [window.start_ns, window.end_ns, window.service, window.service]


# Spans that started inside the window, optionally for one service.
_IN_WINDOW = """
    windowed AS (
        SELECT * FROM spans
        WHERE start_ns >= ? AND start_ns < ? AND (? IS NULL OR service_name = ?)
    )
"""

_OVERVIEW = f"""
    WITH {_IN_WINDOW},
    per_trace AS (
        SELECT trace_id, max(end_ns) - min(start_ns) AS duration_ns,
               sum(cost_usd) FILTER (WHERE counts_toward_totals) AS cost_usd
        FROM windowed GROUP BY trace_id
    )
    SELECT
        (SELECT count(*) FROM per_trace) AS traces,
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
        quantile_cont((end_ns - start_ns) / 1e6, 0.5) FILTER (WHERE is_model_call) AS model_p50_ms,
        quantile_cont((end_ns - start_ns) / 1e6, 0.95) FILTER (WHERE is_model_call) AS model_p95_ms,
        (SELECT quantile_cont(duration_ns / 1e6, 0.5) FROM per_trace) AS trace_p50_ms,
        (SELECT quantile_cont(duration_ns / 1e6, 0.95) FROM per_trace) AS trace_p95_ms,
        (SELECT quantile_cont(cost_usd, 0.5) FROM per_trace WHERE cost_usd IS NOT NULL) AS trace_cost_p50,
        (SELECT quantile_cont(cost_usd, 0.95) FROM per_trace WHERE cost_usd IS NOT NULL) AS trace_cost_p95,
        (SELECT avg(cost_usd) FROM per_trace WHERE cost_usd IS NOT NULL) AS trace_cost_mean
    FROM windowed
"""

_BY_MODEL = f"""
    WITH {_IN_WINDOW}
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
        quantile_cont((end_ns - start_ns) / 1e6, 0.5) AS p50_ms,
        quantile_cont((end_ns - start_ns) / 1e6, 0.95) AS p95_ms,
        max(end_ns - start_ns) / 1e6 AS max_ms,
        histogram(error_type) FILTER (WHERE error_type IS NOT NULL) AS error_types
    FROM windowed WHERE is_model_call
    GROUP BY 1
    ORDER BY cost_usd DESC NULLS LAST, calls DESC, model
"""

# A call can report several finish reasons, so they are counted apart from the calls.
_FINISHES = f"""
    WITH {_IN_WINDOW}
    SELECT model, histogram(finish) AS finish_reasons
    FROM (
        SELECT coalesce(response_model, request_model, '(unknown)') AS model,
               unnest(finish_reasons) AS finish
        FROM windowed WHERE is_model_call
    )
    GROUP BY model
"""

_BY_TOOL = f"""
    WITH {_IN_WINDOW}
    SELECT
        coalesce(tool_name, name) AS tool,
        count(*) AS calls,
        count(*) FILTER (WHERE status_code = {STATUS_ERROR}) AS errors,
        quantile_cont((end_ns - start_ns) / 1e6, 0.5) AS p50_ms,
        quantile_cont((end_ns - start_ns) / 1e6, 0.95) AS p95_ms,
        max(end_ns - start_ns) / 1e6 AS max_ms,
        histogram(error_type) FILTER (WHERE error_type IS NOT NULL) AS error_types
    FROM windowed WHERE operation = 'execute_tool'
    GROUP BY 1
    ORDER BY calls DESC, tool
"""

# Model calls and tool calls belong to their nearest invoke_agent ancestor, so an agent's
# figures are its own, not its child agents'.
_BY_AGENT = (
    "WITH RECURSIVE "
    + _IN_WINDOW
    + """,
    leaves AS (
        SELECT trace_id, span_id, parent_span_id FROM windowed
        WHERE is_model_call OR operation = 'execute_tool'
    ),
    walk(trace_id, span_id, ancestor, depth, agent) AS (
        SELECT trace_id, span_id, parent_span_id, 0, NULL::VARCHAR FROM leaves
        UNION ALL
        SELECT w.trace_id, w.span_id, p.parent_span_id, w.depth + 1,
               CASE WHEN p.operation = 'invoke_agent' THEN coalesce(p.agent_name, p.name) END
        FROM walk w JOIN spans p ON p.trace_id = w.trace_id AND p.span_id = w.ancestor
        WHERE w.agent IS NULL AND w.depth < {depth}
    ),
    owner AS (
        SELECT trace_id, span_id, any_value(agent) AS agent
        FROM walk WHERE agent IS NOT NULL GROUP BY trace_id, span_id
    ),
    own AS (
        SELECT coalesce(o.agent, '(no agent)') AS agent,
               count(*) FILTER (WHERE s.is_model_call) AS model_calls,
               count(*) FILTER (WHERE s.operation = 'execute_tool') AS tool_calls,
               coalesce(sum(s.input_tokens) FILTER (WHERE s.is_model_call), 0) AS input_tokens,
               coalesce(sum(s.output_tokens) FILTER (WHERE s.is_model_call), 0) AS output_tokens,
               sum(s.cost_usd) FILTER (WHERE s.is_model_call) AS cost_usd
        FROM windowed s
        JOIN leaves l ON l.trace_id = s.trace_id AND l.span_id = s.span_id
        LEFT JOIN owner o ON o.trace_id = s.trace_id AND o.span_id = s.span_id
        GROUP BY 1
    ),
    runs AS (
        SELECT coalesce(agent_name, name) AS agent,
               count(*) AS runs,
               count(*) FILTER (WHERE status_code = """
    + str(STATUS_ERROR)
    + """) AS errors,
               quantile_cont((end_ns - start_ns) / 1e6, 0.5) AS p50_ms,
               quantile_cont((end_ns - start_ns) / 1e6, 0.95) AS p95_ms
        FROM windowed WHERE operation = 'invoke_agent'
        GROUP BY 1
    )
    SELECT coalesce(r.agent, o.agent) AS agent,
           coalesce(r.runs, 0) AS runs, coalesce(r.errors, 0) AS errors,
           r.p50_ms, r.p95_ms,
           coalesce(o.model_calls, 0) AS model_calls, coalesce(o.tool_calls, 0) AS tool_calls,
           coalesce(o.input_tokens, 0) AS input_tokens, coalesce(o.output_tokens, 0) AS output_tokens,
           o.cost_usd
    FROM runs r FULL OUTER JOIN own o ON o.agent = r.agent
    ORDER BY o.cost_usd DESC NULLS LAST, runs DESC, agent
"""
)


def _series_query(bucket: int) -> str:
    return f"""
    WITH {_IN_WINDOW}
    SELECT
        (start_ns - ?) // {bucket} AS slot,
        count(*) FILTER (WHERE is_model_call) AS model_calls,
        count(*) FILTER (WHERE is_model_call AND status_code = {STATUS_ERROR}) AS model_errors,
        count(*) FILTER (WHERE operation = 'execute_tool') AS tool_calls,
        count(*) FILTER (WHERE parent_span_id IS NULL) AS traces,
        coalesce(sum(input_tokens) FILTER (WHERE is_model_call), 0) AS input_tokens,
        coalesce(sum(cache_read_tokens) FILTER (WHERE is_model_call), 0) AS cache_read_tokens,
        sum(cost_usd) FILTER (WHERE counts_toward_totals) AS cost_usd,
        quantile_cont((end_ns - start_ns) / 1e6, 0.5) FILTER (WHERE is_model_call) AS model_p50_ms,
        quantile_cont((end_ns - start_ns) / 1e6, 0.95) FILTER (WHERE is_model_call) AS model_p95_ms
    FROM windowed
    GROUP BY slot
    ORDER BY slot
    """


def _series(store: Store, window: Window, bucket: int) -> list[dict[str, Any]]:
    # Buckets start on whole multiples of their width (UTC), so hours and days line up.
    first = window.start_ns - window.start_ns % bucket
    rows = store.query(_series_query(bucket), [*_window_params(window), first], with_names=True)
    by_slot = {int(row["slot"]): row for row in rows}
    count = max(1, -(-(window.end_ns - first) // bucket))
    series: list[dict[str, Any]] = []
    for slot in range(count):
        row = by_slot.get(slot, {})
        series.append(
            {
                "start_ns": str(first + slot * bucket),
                "model_calls": int(row.get("model_calls", 0)),
                "model_errors": int(row.get("model_errors", 0)),
                "tool_calls": int(row.get("tool_calls", 0)),
                "traces": int(row.get("traces", 0)),
                "input_tokens": int(row.get("input_tokens", 0)),
                "cache_read_tokens": int(row.get("cache_read_tokens", 0)),
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
