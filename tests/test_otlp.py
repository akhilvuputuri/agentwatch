from __future__ import annotations

import gzip
import json
import zlib

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agentwatch import otlp
from agentwatch.spans import STATUS_ERROR, Span, SpanEvent, SpanLink
from tests.builders import encode, to_otlp_json
from tests.strategies import spans as span_strategy

TRACE = "5b8efff798038103d269b633813fc60c"
SPAN = "eee19b7ec3c1b174"
PARENT = "eee19b7ec3c1b173"


def sample() -> Span:
    return Span(
        trace_id=TRACE,
        span_id=SPAN,
        parent_span_id=PARENT,
        name="chat example/model",
        start_ns=1_700_000_000_000_000_000,
        end_ns=1_700_000_001_500_000_000,
        kind=3,
        status_code=STATUS_ERROR,
        status_message="rate limited",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.usage.input_tokens": 1200,
            "agentwatch.usage.cost_usd": 0.0042,
            "gen_ai.response.finish_reasons": ["stop"],
            "nested": {"a": [1, True, "x"]},
            "raw": b"\x00\x01",
        },
        resource={"service.name": "svc"},
        scope_name="scope",
        scope_version="1.0",
        events=(SpanEvent(name="retry", time_ns=1_700_000_000_500_000_000, attributes={"n": 1}),),
        links=(SpanLink(trace_id=TRACE, span_id=PARENT, attributes={}),),
    )


def test_protobuf_round_trip() -> None:
    body = encode([sample()]).SerializeToString()
    assert otlp.decode(body, "application/x-protobuf").spans == [sample()]


def test_json_with_hex_ids_round_trip() -> None:
    body = json.dumps(to_otlp_json(encode([sample()]))).encode()
    assert otlp.decode(body, "application/json; charset=utf-8").spans == [sample()]


def test_hand_written_otlp_json() -> None:
    body = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "svc"}}]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "manual"},
                        "spans": [
                            {
                                "traceId": TRACE.upper(),
                                "spanId": SPAN,
                                "parentSpanId": "",
                                "name": "invoke_agent helper",
                                "kind": 1,
                                "startTimeUnixNano": "1700000000000000000",
                                "endTimeUnixNano": 1700000000000001000,
                                "attributes": [
                                    {
                                        "key": "gen_ai.usage.input_tokens",
                                        "value": {"intValue": "77"},
                                    },
                                    {"key": "flag", "value": {"boolValue": True}},
                                ],
                                "status": {"code": 2, "message": "boom"},
                                "unknownField": 1,
                            }
                        ],
                    }
                ],
            }
        ]
    }
    [span] = otlp.decode(json.dumps(body).encode(), "application/json").spans
    assert span.trace_id == TRACE
    assert span.parent_span_id is None
    assert span.attributes == {"gen_ai.usage.input_tokens": 77, "flag": True}
    assert span.status_code == STATUS_ERROR
    assert span.end_ns - span.start_ns == 1000
    assert span.service_name == "svc"


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
def test_compressed_bodies(encoding: str) -> None:
    raw = encode([sample()]).SerializeToString()
    body = gzip.compress(raw) if encoding == "gzip" else zlib.compress(raw)
    assert otlp.decode(body, "application/x-protobuf", encoding).spans == [sample()]


def test_truncated_gzip_is_rejected() -> None:
    body = gzip.compress(encode([sample()]).SerializeToString())
    with pytest.raises(otlp.OtlpError, match="truncated"):
        otlp.decode(body[:-10], "application/x-protobuf", "gzip")


def test_decompression_bomb_is_rejected() -> None:
    body = gzip.compress(b"\x00" * (otlp.MAX_BODY_BYTES + 10))
    with pytest.raises(otlp.OtlpError, match="too large"):
        otlp.decode(body, "application/x-protobuf", "gzip")


@pytest.mark.parametrize(
    ("body", "content_type", "message"),
    [
        (b"\xff\xff", "application/x-protobuf", "invalid protobuf"),
        (b"[1]", "application/json", "must be an object"),
        (b"{", "application/json", "invalid JSON"),
        (
            json.dumps(
                {"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": "zz"}]}]}]}
            ).encode(),
            "application/json",
            "not hex",
        ),
    ],
)
def test_invalid_bodies(body: bytes, content_type: str, message: str) -> None:
    with pytest.raises(otlp.OtlpError, match=message):
        otlp.decode(body, content_type)


def test_invalid_spans_are_rejected_individually() -> None:
    zero = Span(
        trace_id="0" * 32, span_id=SPAN, parent_span_id=None, name="x", start_ns=0, end_ns=0
    )
    request = encode([zero, sample()])
    spans = request.resource_spans[0].scope_spans[0].spans
    short = spans.add()
    short.CopyFrom(spans[1])
    short.span_id = b"\x01"
    late = spans.add()
    late.CopyFrom(spans[1])
    late.end_time_unix_nano = 2**63 + 6
    decoded = otlp.decode(request.SerializeToString(), "application/x-protobuf")
    assert decoded.spans == [sample()]
    assert decoded.rejected == 3
    assert "all-zero" in decoded.error


def test_concatenated_gzip_members() -> None:
    first = encode([sample()]).SerializeToString()
    other = Span(
        trace_id=TRACE, span_id=PARENT, parent_span_id=None, name="root", start_ns=1, end_ns=2
    )
    # Concatenated protobuf messages merge their repeated fields.
    body = gzip.compress(first) + gzip.compress(encode([other]).SerializeToString())
    decoded = otlp.decode(body, "application/x-protobuf", "gzip")
    assert [span.name for span in decoded.spans] == [sample().name, "root"]


def test_unsupported_content_type() -> None:
    with pytest.raises(otlp.UnsupportedMediaType):
        otlp.decode(b"{}", "text/plain")


def test_unknown_encoding_is_rejected() -> None:
    with pytest.raises(otlp.OtlpError, match="unsupported content encoding"):
        otlp.decode(b"", "application/x-protobuf", "br")


@settings(max_examples=150, deadline=None)
@given(st.lists(span_strategy(), min_size=1, max_size=4))
def test_protobuf_and_json_decode_identically(spans: list[Span]) -> None:
    request = encode(spans)
    from_protobuf = otlp.decode(request.SerializeToString(), "application/x-protobuf").spans
    from_json = otlp.decode(json.dumps(to_otlp_json(request)).encode(), "application/json").spans
    assert from_protobuf == from_json
    assert sorted(from_protobuf, key=repr) == sorted(spans, key=repr)
