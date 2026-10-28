"""Decode OTLP/HTTP trace exports (protobuf or JSON) into spans."""

from __future__ import annotations

import base64
import gzip
import json
import zlib
from dataclasses import dataclass
from typing import Any, cast

from google.protobuf import json_format
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

from agentwatch.spans import Attributes, Span, SpanEvent, SpanLink, Value

PROTOBUF = "application/x-protobuf"
JSON = "application/json"

# Largest decompressed body accepted, to bound memory on a malformed or hostile export.
MAX_BODY_BYTES = 32 * 1024 * 1024


class OtlpError(ValueError):
    """The request body is not a valid OTLP trace export."""


class UnsupportedMediaType(OtlpError):
    """The content type is neither OTLP protobuf nor OTLP JSON."""


@dataclass(frozen=True, slots=True)
class Decoded:
    """Valid spans, plus how many were rejected and why (OTLP partial success)."""

    spans: list[Span]
    rejected: int = 0
    error: str = ""


# OTLP timestamps are unsigned 64-bit; storage is signed 64-bit (good until year 2262).
MAX_TIME_NS = 2**63 - 1


def decompress(body: bytes, encoding: str | None) -> bytes:
    encoding = (encoding or "identity").strip().lower()
    if encoding in ("", "identity"):
        data = body
    elif encoding == "gzip":
        try:
            data = _gunzip(body)
        except (OSError, zlib.error, EOFError, gzip.BadGzipFile) as error:
            raise OtlpError(f"invalid gzip body: {error}") from error
    elif encoding == "deflate":
        try:
            data = _bounded(zlib.decompressobj(), body)
        except zlib.error as error:
            raise OtlpError(f"invalid deflate body: {error}") from error
    else:
        raise OtlpError(f"unsupported content encoding: {encoding}")
    if len(data) > MAX_BODY_BYTES:
        raise OtlpError("body too large")
    return data


def _gunzip(body: bytes) -> bytes:
    """Decompress every gzip member; a stream may concatenate several."""
    parts: list[bytes] = []
    size = 0
    rest = body
    while True:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        part: bytes = decoder.decompress(rest, MAX_BODY_BYTES + 1 - size)
        size += len(part)
        if size > MAX_BODY_BYTES or decoder.unconsumed_tail:
            raise OtlpError("body too large")
        if not decoder.eof:
            raise OtlpError("compressed body is truncated")
        parts.append(part)
        rest = decoder.unused_data
        if not rest:
            return b"".join(parts)


def _bounded(decoder: Any, body: bytes) -> bytes:
    data: bytes = decoder.decompress(body, MAX_BODY_BYTES + 1)
    if len(data) > MAX_BODY_BYTES or decoder.unconsumed_tail:
        raise OtlpError("body too large")
    if not decoder.eof:
        raise OtlpError("compressed body is truncated")
    return data


def parse_request(body: bytes, content_type: str) -> ExportTraceServiceRequest:
    media = content_type.split(";", 1)[0].strip().lower()
    request = ExportTraceServiceRequest()
    if media == PROTOBUF:
        try:
            request.ParseFromString(body)
        except DecodeError as error:
            raise OtlpError(f"invalid protobuf body: {error}") from error
        return request
    if media == JSON:
        try:
            document = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OtlpError(f"invalid JSON body: {error}") from error
        if not isinstance(document, dict):
            raise OtlpError("JSON body must be an object")
        try:
            json_format.ParseDict(
                _hex_ids_to_base64(cast(dict[str, Any], document)),
                request,
                ignore_unknown_fields=True,
            )
        except json_format.ParseError as error:
            raise OtlpError(f"invalid OTLP JSON: {error}") from error
        return request
    raise UnsupportedMediaType(f"unsupported content type: {content_type or '(none)'}")


def _hex_ids_to_base64(document: dict[str, Any]) -> dict[str, Any]:
    """OTLP/JSON writes trace and span IDs as hex; protobuf's JSON mapping expects base64."""

    def convert(holder: dict[str, Any], key: str) -> None:
        value = holder.get(key)
        if not isinstance(value, str) or value == "":
            return
        try:
            raw = bytes.fromhex(value)
        except ValueError as error:
            raise OtlpError(f"{key} is not hex: {value[:40]!r}") from error
        holder[key] = base64.b64encode(raw).decode("ascii")

    for resource_spans in _objects(document.get("resourceSpans")):
        for scope_spans in _objects(resource_spans.get("scopeSpans")):
            for span in _objects(scope_spans.get("spans")):
                for key in ("traceId", "spanId", "parentSpanId"):
                    convert(span, key)
                for link in _objects(span.get("links")):
                    for key in ("traceId", "spanId"):
                        convert(link, key)
    return document


def _objects(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]  # pyright: ignore[reportUnknownVariableType]


def spans_from_request(request: ExportTraceServiceRequest) -> Decoded:
    """Convert every valid span. Invalid spans are counted and skipped, not fatal."""
    spans: list[Span] = []
    rejected = 0
    first_error = ""
    for resource_spans in request.resource_spans:
        resource = attributes(resource_spans.resource.attributes)
        for scope_spans in resource_spans.scope_spans:
            scope = scope_spans.scope
            for span in scope_spans.spans:
                trace_id = span.trace_id.hex()
                span_id = span.span_id.hex()
                problem = _problem(span, trace_id, span_id)
                if problem:
                    rejected += 1
                    first_error = first_error or problem
                    continue
                parent = span.parent_span_id.hex() or None
                if parent == "0" * 16:
                    parent = None
                spans.append(
                    Span(
                        trace_id=trace_id,
                        span_id=span_id,
                        parent_span_id=parent,
                        name=span.name,
                        start_ns=span.start_time_unix_nano,
                        end_ns=span.end_time_unix_nano,
                        kind=int(span.kind),
                        status_code=int(span.status.code),
                        status_message=span.status.message,
                        attributes=attributes(span.attributes),
                        resource=resource,
                        scope_name=scope.name,
                        scope_version=scope.version,
                        events=tuple(
                            SpanEvent(
                                name=event.name,
                                time_ns=event.time_unix_nano,
                                attributes=attributes(event.attributes),
                            )
                            for event in span.events
                        ),
                        links=tuple(
                            SpanLink(
                                trace_id=link.trace_id.hex(),
                                span_id=link.span_id.hex(),
                                attributes=attributes(link.attributes),
                            )
                            for link in span.links
                        ),
                    )
                )
    if rejected:
        return Decoded(spans, rejected, f"{rejected} span(s) rejected; first: {first_error}")
    return Decoded(spans)


def _problem(span: Any, trace_id: str, span_id: str) -> str:
    if len(trace_id) != 32 or len(span_id) != 16:
        return "invalid trace or span ID length"
    if trace_id == "0" * 32 or span_id == "0" * 16:
        return "all-zero trace or span ID"
    parent = span.parent_span_id.hex()
    if parent and len(parent) != 16:
        return "invalid parent span ID length"
    # Proto3 enums accept any integer; only the defined kinds and status codes are stored.
    if not 0 <= span.kind <= 5 or not 0 <= span.status.code <= 2:
        return "unknown span kind or status code"
    if span.start_time_unix_nano > MAX_TIME_NS or span.end_time_unix_nano > MAX_TIME_NS:
        return "timestamp out of range"
    if any(event.time_unix_nano > MAX_TIME_NS for event in span.events):
        return "event timestamp out of range"
    return ""


def decode(body: bytes, content_type: str, content_encoding: str | None = None) -> Decoded:
    return spans_from_request(parse_request(decompress(body, content_encoding), content_type))


def attributes(pairs: Any) -> Attributes:
    result: Attributes = {}
    for pair in pairs:
        assert isinstance(pair, KeyValue)
        result[pair.key] = any_value(pair.value)
    return result


def any_value(value: AnyValue) -> Value:
    match value.WhichOneof("value"):
        case "string_value":
            return value.string_value
        case "bool_value":
            return value.bool_value
        case "int_value":
            return value.int_value
        case "double_value":
            return value.double_value
        case "bytes_value":
            return value.bytes_value
        case "array_value":
            return [any_value(item) for item in value.array_value.values]
        case "kvlist_value":
            return attributes(value.kvlist_value.values)
        case _:
            return None
