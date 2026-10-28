from __future__ import annotations

from collections import defaultdict

from agentwatch import demo, genai
from agentwatch.spans import Span
from agentwatch.store import Store

END = 1_761_000_000_000_000_000


def test_demo_is_deterministic() -> None:
    assert demo.generate(traces=20, seed=3, end_ns=END) == demo.generate(
        traces=20, seed=3, end_ns=END
    )
    assert demo.generate(traces=20, seed=3, end_ns=END) != demo.generate(
        traces=20, seed=4, end_ns=END
    )


def test_demo_traces_are_well_formed() -> None:
    spans = demo.generate(traces=60, end_ns=END)
    by_trace: dict[str, list[Span]] = defaultdict(list)
    for span in spans:
        by_trace[span.trace_id].append(span)
    assert len(by_trace) == 60
    for members in by_trace.values():
        ids = {span.span_id for span in members}
        assert len(ids) == len(members)
        roots = [span for span in members if span.parent_span_id is None]
        assert len(roots) == 1
        root = roots[0]
        for span in members:
            assert span.parent_span_id is None or span.parent_span_id in ids
            assert root.start_ns <= span.start_ns <= span.end_ns <= root.end_ns
            assert span.end_ns <= END + 10**12


def test_demo_loads_into_the_store() -> None:
    store = Store()
    spans = demo.generate(traces=30, end_ns=END)
    store.insert(spans, received_ns=END)
    summaries = store.traces(limit=100)
    assert len(summaries) == 30
    assert all(summary.complete for summary in summaries)
    assert sum(s.model_calls for s in summaries) == sum(
        1 for s in spans if genai.read(s).is_model_call
    )
    assert any(s.error_count for s in summaries)
    assert all(s.cost_usd is None or s.cost_usd > 0 for s in summaries)
