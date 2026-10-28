"""Hypothesis strategies for spans."""

from __future__ import annotations

from hypothesis import strategies as st

from agentwatch.spans import Attributes, Span, SpanEvent, Value

hex_trace = st.integers(min_value=1, max_value=2**128 - 1).map(lambda n: f"{n:032x}")
hex_span = st.integers(min_value=1, max_value=2**64 - 1).map(lambda n: f"{n:016x}")
keys = st.text(st.characters(codec="utf-8", exclude_categories=("Cs",)), min_size=1, max_size=12)

scalars: st.SearchStrategy[Value] = st.one_of(
    st.text(max_size=20),
    st.booleans(),
    st.integers(min_value=-(2**63), max_value=2**63 - 1),
    st.floats(allow_nan=False, allow_infinity=False),
    st.binary(max_size=8),
)
values: st.SearchStrategy[Value] = st.recursive(
    scalars,
    lambda inner: st.one_of(st.lists(inner, max_size=3), st.dictionaries(keys, inner, max_size=3)),
    max_leaves=6,
)
attribute_maps: st.SearchStrategy[Attributes] = st.dictionaries(keys, values, max_size=5)


@st.composite
def spans(draw: st.DrawFn) -> Span:
    start = draw(st.integers(min_value=0, max_value=2**62))
    return Span(
        trace_id=draw(hex_trace),
        span_id=draw(hex_span),
        parent_span_id=draw(st.none() | hex_span),
        name=draw(st.text(max_size=30)),
        start_ns=start,
        end_ns=start + draw(st.integers(min_value=0, max_value=2**40)),
        kind=draw(st.integers(min_value=0, max_value=5)),
        status_code=draw(st.integers(min_value=0, max_value=2)),
        status_message=draw(st.text(max_size=10)),
        attributes=draw(attribute_maps),
        resource=draw(attribute_maps),
        scope_name=draw(st.text(max_size=10)),
        scope_version=draw(st.text(max_size=5)),
        events=tuple(
            draw(
                st.lists(
                    st.builds(
                        SpanEvent,
                        name=st.text(max_size=10),
                        time_ns=st.integers(min_value=0, max_value=2**62),
                        attributes=attribute_maps,
                    ),
                    max_size=2,
                )
            )
        ),
    )
