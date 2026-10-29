from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from starlette.testclient import TestClient

from agentwatch import genai, metrics, server
from agentwatch.spans import STATUS_ERROR, Attributes, Span
from agentwatch.store import Store

HOUR = 3600 * 10**9
T0 = 1_761_004_800 * 10**9  # a whole UTC hour
T1 = "a" * 32


def make(
    span_id: str,
    parent: str | None,
    start: int,
    duration: int,
    attributes: Attributes,
    *,
    trace: str = T1,
    service: str = "svc",
    error: bool = False,
) -> Span:
    return Span(
        trace_id=trace,
        span_id=span_id.rjust(16, "0"),
        parent_span_id=None if parent is None else parent.rjust(16, "0"),
        name=str(attributes.get(genai.OPERATION, "span")),
        start_ns=start,
        end_ns=start + duration,
        status_code=STATUS_ERROR if error else 0,
        attributes=attributes,
        resource={"service.name": service},
    )


def agent(name: str) -> Attributes:
    return {genai.OPERATION: "invoke_agent", genai.AGENT_NAME: name}


def chat(model: str, tokens: int, cost: float | None, finish: list[str]) -> Attributes:
    attributes: Attributes = {
        genai.OPERATION: "chat",
        genai.REQUEST_MODEL: model,
        genai.INPUT_TOKENS: tokens,
        genai.OUTPUT_TOKENS: tokens // 10,
        genai.CACHE_READ_TOKENS: tokens // 2,
        genai.FINISH_REASONS: list(finish),
    }
    if cost is not None:
        attributes[genai.COST_USD] = cost
    return attributes


def tool(name: str) -> Attributes:
    return {genai.OPERATION: "execute_tool", genai.TOOL_NAME: name}


def sample_store() -> Store:
    store = Store()
    ms = 10**6
    store.insert(
        [
            make("1", None, T0, 5000 * ms, agent("main")),
            make("2", "1", T0 + 1 * ms, 1000 * ms, chat("m1", 1000, 0.10, ["tool_calls"])),
            make("3", "1", T0 + 1200 * ms, 200 * ms, tool("search")),
            make("4", "1", T0 + 1500 * ms, 2000 * ms, agent("helper")),
            make("5", "4", T0 + 1600 * ms, 500 * ms, chat("m2", 400, 0.01, ["stop", "length"])),
            make("6", "4", T0 + 2200 * ms, 300 * ms, tool("search"), error=True),
            make(
                "7",
                "1",
                T0 + 4000 * ms,
                100 * ms,
                {**chat("m1", 0, None, []), genai.ERROR_TYPE: "timeout"},
                error=True,
            ),
            # Outside the window and another service.
            make("8", None, T0 + 5 * HOUR, 10 * ms, chat("m1", 99, 9.0, []), trace="b" * 32),
            make(
                "9",
                None,
                T0 + 10 * ms,
                10 * ms,
                chat("m3", 5, 1.0, []),
                trace="c" * 32,
                service="other",
            ),
        ],
        received_ns=1,
    )
    return store


def test_overview_and_breakdowns() -> None:
    result = metrics.metrics(sample_store(), metrics.Window(T0, T0 + HOUR, "svc"))
    overview = result["overview"]
    assert overview["traces"] == 1
    assert (overview["model_calls"], overview["model_errors"]) == (3, 1)
    assert (overview["tool_calls"], overview["tool_errors"]) == (2, 1)
    assert overview["input_tokens"] == 1400
    assert overview["cache_read_tokens"] == 700
    assert overview["cost_usd"] == pytest.approx(0.11)
    assert overview["calls_without_cost"] == 1
    assert overview["trace_p50_ms"] == pytest.approx(5000)
    assert overview["model_p50_ms"] == pytest.approx(500)

    models = {row["model"]: row for row in result["by_model"]}
    assert models["m1"]["calls"] == 2
    assert models["m1"]["errors"] == 1
    assert models["m1"]["error_types"] == {"timeout": 1}
    assert models["m1"]["finish_reasons"] == {"tool_calls": 1}
    # Two finish reasons on one call count once each, without counting the call twice.
    assert models["m2"]["calls"] == 1
    assert models["m2"]["finish_reasons"] == {"stop": 1, "length": 1}
    assert [row["model"] for row in result["by_model"]] == ["m1", "m2"]

    [search] = result["by_tool"]
    assert (search["tool"], search["calls"], search["errors"]) == ("search", 2, 1)
    assert search["p50_ms"] == pytest.approx(250)

    agents = {row["agent"]: row for row in result["by_agent"]}
    # Each agent's figures are its own calls, not its child agent's.
    assert agents["main"]["model_calls"] == 2
    assert agents["main"]["tool_calls"] == 1
    assert agents["main"]["cost_usd"] == pytest.approx(0.10)
    assert agents["helper"]["model_calls"] == 1
    assert agents["helper"]["tool_calls"] == 1
    assert agents["helper"]["cost_usd"] == pytest.approx(0.01)
    assert agents["helper"]["p50_ms"] == pytest.approx(2000)


def test_calls_without_an_agent_are_grouped() -> None:
    store = Store()
    store.insert([make("1", None, T0, 10, chat("m", 10, 0.5, []))], received_ns=1)
    [row] = metrics.metrics(store, metrics.Window(T0, T0 + HOUR))["by_agent"]
    assert (row["agent"], row["runs"], row["model_calls"]) == ("(no agent)", 0, 1)


def test_series_buckets_align_and_fill_gaps() -> None:
    store = sample_store()
    window = metrics.Window(T0 - HOUR // 2, T0 + 6 * HOUR)
    result = metrics.metrics(store, window)
    assert result["window"]["bucket_ns"] == 15 * 60 * 10**9  # 6.5 h / 5 min > 72 buckets
    series = result["series"]
    assert int(series[0]["start_ns"]) % result["window"]["bucket_ns"] == 0
    assert int(series[0]["start_ns"]) <= window.start_ns
    assert sum(point["model_calls"] for point in series) == result["overview"]["model_calls"]
    assert sum(point["traces"] for point in series) == 3
    assert any(point["model_calls"] == 0 and point["cost_usd"] is None for point in series)


def test_bucket_width_keeps_series_short() -> None:
    for days in (0.01, 1, 7, 30, 365):
        window = metrics.Window(0, int(days * 24 * HOUR))
        assert (
            window.end_ns / window.bucket_ns() <= metrics.MAX_BUCKETS
            or window.bucket_ns() == metrics.BUCKETS_NS[-1]
        )


def test_empty_window() -> None:
    result = metrics.metrics(Store(), metrics.Window(T0, T0 + HOUR))
    assert result["overview"]["traces"] == 0
    assert result["overview"]["cost_usd"] is None
    assert result["by_model"] == []
    assert len(result["series"]) == 60  # one-minute buckets


def test_data_range() -> None:
    assert metrics.data_range(Store()) is None
    start, end = metrics.data_range(sample_store()) or (0, 0)
    assert start == T0
    assert end == T0 + 5 * HOUR + 10 * 10**6
    assert metrics.data_range(sample_store(), "other") == (T0 + 10**7, T0 + 2 * 10**7)


@settings(max_examples=40, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=3 * HOUR),
            st.integers(min_value=0, max_value=5000),
            st.none() | st.floats(min_value=0, max_value=2, allow_nan=False),
        ),
        min_size=1,
        max_size=15,
    )
)
def test_series_add_up_to_the_overview(calls: list[tuple[int, int, float | None]]) -> None:
    store = Store()
    store.insert(
        [
            make(
                str(i + 1),
                None,
                T0 + offset,
                10,
                chat("m", tokens, cost, []),
                trace=f"{i + 1:032x}",
            )
            for i, (offset, tokens, cost) in enumerate(calls)
        ],
        received_ns=1,
    )
    result = metrics.metrics(store, metrics.Window(T0, T0 + 3 * HOUR + 1))
    series = result["series"]
    assert sum(p["input_tokens"] for p in series) == sum(t for _, t, _ in calls)
    assert sum(p["traces"] for p in series) == len(calls)
    costs = [c for _, _, c in calls if c is not None]
    total = sum(p["cost_usd"] or 0 for p in series)
    assert total == pytest.approx(sum(costs))
    assert (result["overview"]["cost_usd"] is None) == (not costs)


def test_metrics_endpoint() -> None:
    http = TestClient(server.create_app(sample_store(), allowed_hosts=("testserver",)))
    body = http.get("/api/metrics", params={"from": T0, "to": T0 + HOUR}).json()
    assert body["overview"]["model_calls"] == 4
    assert body["window"]["start_ns"] == str(T0)
    everything = http.get("/api/metrics", params={"range": "all", "service": "svc"}).json()
    assert everything["overview"]["traces"] == 2
    assert http.get("/api/metrics").status_code == 200
    assert http.get("/api/metrics", params={"from": "x"}).status_code == 400
    assert http.get("/api/metrics", params={"from": T0, "to": T0}).status_code == 400


def test_traces_count_where_their_root_starts() -> None:
    store = Store()
    ms = 10**6
    store.insert(
        [
            # Root before the window, a call inside it: the call counts, the trace does not.
            make("1", None, T0 - 10 * ms, 30 * ms, agent("a"), trace="1" * 32),
            make("2", "1", T0 + 5 * ms, 1 * ms, chat("m", 10, 0.1, []), trace="1" * 32),
            # A trace whose root never arrived counts where its earliest orphan starts.
            make("3", "9", T0 + 20 * ms, 1 * ms, chat("m", 10, 0.1, []), trace="2" * 32),
            make("4", None, T0 + 30 * ms, 1 * ms, agent("a"), trace="3" * 32),
        ],
        received_ns=1,
    )
    result = metrics.metrics(store, metrics.Window(T0, T0 + HOUR))
    assert result["overview"]["traces"] == 2
    assert sum(p["traces"] for p in result["series"]) == 2
    assert result["overview"]["model_calls"] == 2


def test_service_filter_uses_the_root_service() -> None:
    store = Store()
    store.insert(
        [
            make("1", None, T0, 100, agent("a"), service="front"),
            make("2", "1", T0 + 1, 10, chat("m", 10, 0.1, []), service="back"),
        ],
        received_ns=1,
    )
    front = metrics.metrics(store, metrics.Window(T0, T0 + HOUR, "front"))["overview"]
    assert (front["traces"], front["model_calls"]) == (1, 1)
    back = metrics.metrics(store, metrics.Window(T0, T0 + HOUR, "back"))["overview"]
    assert (back["traces"], back["model_calls"]) == (0, 0)


def test_all_time_covers_spans_ending_before_they_start() -> None:
    store = Store()
    store.insert(
        [
            make("1", None, T0, 10, chat("m", 1, 0.1, []), trace="1" * 32),
            # An unended span (end 0) stored with the latest start.
            Span(
                trace_id="2" * 32,
                span_id="2" * 16,
                parent_span_id=None,
                name="chat",
                start_ns=T0 + HOUR,
                end_ns=0,
                attributes=chat("m", 1, 0.1, []),
            ),
        ],
        received_ns=1,
    )
    window = metrics.window_for_all(store, None, now_ns=T0)
    result = metrics.metrics(store, window)
    assert result["overview"]["model_calls"] == 2
    # Its negative duration is clamped, not averaged in.
    assert result["overview"]["model_p50_ms"] >= 0
    [model] = result["by_model"]
    assert model["max_ms"] == pytest.approx(10 / 1e6)


def test_all_time_on_an_empty_store_and_at_the_largest_timestamp() -> None:
    empty = metrics.window_for_all(Store(), None, now_ns=T0)
    assert empty.end_ns == T0
    store = Store()
    store.insert([make("1", None, T0, 2**63 - 1 - T0, chat("m", 1, 0.1, []))], received_ns=1)
    window = metrics.window_for_all(store, None, now_ns=T0)
    assert window.end_ns == 2**63 - 1
    assert metrics.metrics(store, window)["overview"]["model_calls"] == 1


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (T0 + HOUR // 2, T0 + HOUR // 2 + 72 * HOUR),
        (0, 2**63 - 1),
        (5, 6),
        (T0, T0 + 400 * 24 * HOUR),
    ],
)
def test_bucket_count_never_exceeds_the_cap(start: int, end: int) -> None:
    window = metrics.Window(start, end)
    width = window.bucket_ns()
    first = window.first_bucket_ns(width)
    assert -(-(end - first) // width) <= metrics.MAX_BUCKETS
    assert first <= start


def test_agent_cost_includes_priced_tool_spans() -> None:
    store = Store()
    store.insert(
        [
            make("1", None, T0, 100, agent("a")),
            make("2", "1", T0 + 1, 10, chat("m", 10, 3.0, [])),
            make("3", "1", T0 + 20, 10, {**tool("search"), genai.COST_USD: 0.5}),
            make("4", "1", T0 + 40, 10, {"custom": "x", genai.COST_USD: 0.25}),
        ],
        received_ns=1,
    )
    result = metrics.metrics(store, metrics.Window(T0, T0 + HOUR))
    assert result["overview"]["cost_usd"] == pytest.approx(3.75)
    [row] = result["by_agent"]
    assert row["cost_usd"] == pytest.approx(3.75)


def test_an_agent_named_like_the_unowned_group_stays_apart() -> None:
    store = Store()
    store.insert(
        [
            make("1", None, T0, 100, agent(metrics.NO_AGENT), trace="1" * 32),
            make("2", "1", T0 + 1, 10, chat("m", 10, 1.0, []), trace="1" * 32),
            make("3", None, T0 + 1, 10, chat("m", 10, 2.0, []), trace="2" * 32),
        ],
        received_ns=1,
    )
    rows = metrics.metrics(store, metrics.Window(T0, T0 + HOUR))["by_agent"]
    assert sorted((r["unowned"], r["cost_usd"]) for r in rows) == [(False, 1.0), (True, 2.0)]
