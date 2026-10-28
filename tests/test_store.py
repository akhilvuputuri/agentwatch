from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agentwatch import genai
from agentwatch.spans import STATUS_ERROR, Attributes, Span, Value
from agentwatch.store import Cursor, Store

T1 = "a" * 32
T2 = "b" * 32


def span(
    trace_id: str,
    span_id: str,
    parent: str | None,
    start: int,
    end: int,
    *,
    service: str = "svc",
    status: int = 0,
    **attributes: Value,
) -> Span:
    return Span(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent,
        name=f"span {span_id[-2:]}",
        start_ns=start,
        end_ns=end,
        status_code=status,
        attributes={key.replace("__", "."): value for key, value in attributes.items()},
        resource={"service.name": service},
    )


def chat(
    trace_id: str,
    span_id: str,
    parent: str,
    start: int,
    *,
    tokens: int,
    cost: float,
    model: str = "m1",
) -> Span:
    return Span(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent,
        name="chat",
        start_ns=start,
        end_ns=start + 10,
        resource={"service.name": "svc"},
        attributes={
            genai.OPERATION: "chat",
            genai.REQUEST_MODEL: model,
            genai.INPUT_TOKENS: tokens,
            genai.OUTPUT_TOKENS: tokens // 10,
            genai.CACHE_READ_TOKENS: tokens // 2,
            genai.COST_USD: cost,
        },
    )


def agent(
    trace_id: str, span_id: str, parent: str | None, start: int, end: int, **extra: int | float
) -> Span:
    attributes: Attributes = {
        genai.OPERATION: "invoke_agent",
        genai.AGENT_NAME: "a",
    }
    attributes.update({key.replace("__", "."): value for key, value in extra.items()})
    return Span(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent,
        name="invoke_agent a",
        start_ns=start,
        end_ns=end,
        resource={"service.name": "svc"},
        attributes=attributes,
    )


def test_summary_totals_skip_aggregate_spans() -> None:
    store = Store()
    root = "1" * 16
    store.insert(
        [
            # The agent span repeats its children's usage; it must not double the totals.
            agent(
                T1,
                root,
                None,
                100,
                400,
                gen_ai__usage__input_tokens=3000,
                agentwatch__usage__cost_usd=0.3,
            ),
            chat(T1, "2" * 16, root, 110, tokens=1000, cost=0.1),
            chat(T1, "3" * 16, root, 200, tokens=2000, cost=0.2, model="m2"),
            span(
                T1,
                "4" * 16,
                root,
                300,
                350,
                gen_ai__operation__name="execute_tool",
                status=STATUS_ERROR,
            ),
        ],
        received_ns=1,
    )
    [summary] = store.traces()
    assert summary.root_name == "invoke_agent a"
    assert summary.start_ns == 100
    assert summary.duration_ns == 300
    assert (summary.span_count, summary.model_calls, summary.tool_calls, summary.error_count) == (
        4,
        2,
        1,
        1,
    )
    assert (summary.input_tokens, summary.output_tokens, summary.cache_read_tokens) == (
        3000,
        300,
        1500,
    )
    assert summary.cost_usd == pytest.approx(0.3)
    assert summary.calls_without_cost == 0
    assert summary.models == ("m1", "m2")
    assert summary.complete


def test_cost_is_none_when_nothing_reports_cost() -> None:
    store = Store()
    store.insert([span(T1, "1" * 16, None, 0, 1)], received_ns=1)
    assert store.traces()[0].cost_usd is None


def test_trace_with_missing_root_is_incomplete() -> None:
    store = Store()
    store.insert([span(T1, "2" * 16, "9" * 16, 5, 9)], received_ns=1)
    [summary] = store.traces()
    assert not summary.complete
    assert summary.root_name == "span 22"


def test_resending_a_span_replaces_it() -> None:
    store = Store()
    first = span(T1, "1" * 16, None, 0, 10)
    store.insert([first], received_ns=1)
    store.insert([span(T1, "1" * 16, None, 0, 20)], received_ns=2)
    assert store.span_count() == 1
    assert store.trace(T1)[0].span.end_ns == 20


def test_paging_and_filters() -> None:
    store = Store()
    store.insert(
        [
            span(T1, "1" * 16, None, 100, 110, service="one"),
            span(T2, "1" * 16, None, 200, 210, service="two", status=STATUS_ERROR),
        ],
        received_ns=1,
    )
    assert [t.trace_id for t in store.traces()] == [T2, T1]
    assert [t.trace_id for t in store.traces(limit=1)] == [T2]
    assert [t.trace_id for t in store.traces(before=Cursor(200, T2))] == [T1]
    assert [t.trace_id for t in store.traces(service="one")] == [T1]
    assert [t.trace_id for t in store.traces(errors_only=True)] == [T2]
    assert store.services() == ["one", "two"]


def test_trace_detail_round_trips_attributes() -> None:
    store = Store()
    original = chat(T1, "2" * 16, "1" * 16, 5, tokens=10, cost=0.5)
    store.insert([original], received_ns=1)
    [stored] = store.trace(T1.upper())
    assert stored.span == original
    assert stored.fields.input_tokens == 10
    assert store.trace(T2) == []


def test_file_database_persists(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "db.duckdb"
    store = Store(path)
    store.insert([span(T1, "1" * 16, None, 0, 1)], received_ns=1)
    store.close()
    reopened = Store(path)
    assert reopened.span_count() == 1
    reopened.close()


@settings(max_examples=60, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.sampled_from(["chat", "embeddings", "execute_tool", "invoke_agent", None]),
            st.integers(min_value=0, max_value=10_000),
            st.none() | st.floats(min_value=0, max_value=5, allow_nan=False),
        ),
        min_size=1,
        max_size=12,
    )
)
def test_totals_match_a_direct_sum(rows: list[tuple[str | None, int, float | None]]) -> None:
    store = Store()
    root = "f" * 16
    spans = [span(T1, root, None, 0, 1000)]
    for index, (operation, tokens, cost) in enumerate(rows):
        attributes: Attributes = {genai.INPUT_TOKENS: tokens}
        if operation is not None:
            attributes[genai.OPERATION] = operation
        if cost is not None:
            attributes[genai.COST_USD] = cost
        spans.append(
            Span(
                trace_id=T1,
                span_id=f"{index + 1:016x}",
                parent_span_id=root,
                name="s",
                start_ns=index,
                end_ns=index + 1,
                attributes=attributes,
            )
        )
    store.insert(spans, received_ns=1)
    [summary] = store.traces()
    # Tokens add up over model calls; cost over every span except agent wrappers.
    model_calls = [row for row in rows if row[0] in ("chat", "embeddings")]
    assert summary.input_tokens == sum(tokens for _, tokens, _ in model_calls)
    counted = [row for row in rows if row[0] != "invoke_agent"]
    costs = [cost for _, _, cost in counted if cost is not None]
    if costs:
        assert summary.cost_usd == pytest.approx(sum(costs))
    else:
        assert summary.cost_usd is None
    assert summary.model_calls == sum(1 for op, _, _ in rows if op in ("chat", "embeddings"))


def test_paging_reaches_every_trace_with_equal_start_times() -> None:
    store = Store()
    ids = [f"{n:032x}" for n in range(1, 6)]
    store.insert([span(trace, "1" * 16, None, 100, 110) for trace in ids], received_ns=1)
    seen: list[str] = []
    cursor = None
    while True:
        page = store.traces(limit=2, before=cursor)
        if not page:
            break
        seen.extend(t.trace_id for t in page)
        cursor = page[-1].cursor
    assert seen == ids


def test_true_root_wins_over_an_earlier_orphan() -> None:
    store = Store()
    store.insert(
        [span(T1, "1" * 16, "9" * 16, 1, 5), span(T1, "2" * 16, None, 2, 9)], received_ns=1
    )
    [summary] = store.traces()
    assert summary.root_name == "span 22"
    assert summary.complete


def test_cost_is_marked_partial_when_a_model_call_has_none() -> None:
    store = Store()
    root = "1" * 16
    unpriced = Span(
        trace_id=T1,
        span_id="3" * 16,
        parent_span_id=root,
        name="chat",
        start_ns=5,
        end_ns=6,
        attributes={genai.OPERATION: "chat"},
    )
    store.insert(
        [span(T1, root, None, 0, 10), chat(T1, "2" * 16, root, 1, tokens=10, cost=0.5), unpriced], 1
    )
    [summary] = store.traces()
    assert summary.cost_usd == pytest.approx(0.5)
    assert summary.calls_without_cost == 1


def test_non_finite_and_oversized_values_are_stored_safely() -> None:
    store = Store()
    weird = Span(
        trace_id=T1,
        span_id="1" * 16,
        parent_span_id=None,
        name="chat",
        start_ns=0,
        end_ns=1,
        attributes={
            genai.OPERATION: "chat",
            genai.INPUT_TOKENS: "9" * 40,
            genai.OUTPUT_TOKENS: 2**63,
            genai.COST_USD: float("nan"),
            "ratio": float("inf"),
        },
    )
    store.insert([weird], received_ns=1)
    [stored] = store.trace(T1)
    assert stored.span.attributes["ratio"] == "inf"
    assert stored.fields.input_tokens is None
    assert stored.fields.output_tokens is None
    assert stored.fields.cost_usd is None


def test_duplicate_spans_in_one_batch_keep_the_last() -> None:
    store = Store()
    store.insert([span(T1, "1" * 16, None, 0, 1), span(T1, "1" * 16, None, 0, 7)], received_ns=1)
    assert store.trace(T1)[0].span.end_ns == 7


def test_services_list_root_services() -> None:
    store = Store()
    root = "1" * 16
    store.insert(
        [
            span(T1, root, None, 0, 9, service="front"),
            span(T1, "2" * 16, root, 1, 2, service="back"),
        ],
        received_ns=1,
    )
    assert store.services() == ["front"]
