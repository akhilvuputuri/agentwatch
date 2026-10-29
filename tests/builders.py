"""Encode spans back into OTLP requests, for tests."""

from __future__ import annotations

import base64
from collections import defaultdict
from typing import Any

from google.protobuf import json_format
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, Status
from opentelemetry.proto.trace.v1.trace_pb2 import Span as ProtoSpan

from agentwatch.spans import Attributes, Span, Value


def encode(spans: list[Span]) -> ExportTraceServiceRequest:
    groups: dict[tuple[str, str, str], list[Span]] = defaultdict(list)
    resources: dict[str, Attributes] = {}
    for span in spans:
        key = repr(sorted(span.resource.items()))
        resources[key] = span.resource
        groups[(key, span.scope_name, span.scope_version)].append(span)
    request = ExportTraceServiceRequest()
    by_resource: dict[str, ResourceSpans] = {}
    for (key, scope_name, scope_version), members in groups.items():
        if key not in by_resource:
            resource_spans = request.resource_spans.add()
            resource_spans.resource.attributes.extend(key_values(resources[key]))
            by_resource[key] = resource_spans
        scope_spans = by_resource[key].scope_spans.add()
        scope_spans.scope.name = scope_name
        scope_spans.scope.version = scope_version
        for span in members:
            proto = ProtoSpan(
                trace_id=bytes.fromhex(span.trace_id),
                span_id=bytes.fromhex(span.span_id),
                parent_span_id=bytes.fromhex(span.parent_span_id or ""),
                name=span.name,
                kind=span.kind,  # pyright: ignore[reportArgumentType]
                start_time_unix_nano=span.start_ns,
                end_time_unix_nano=span.end_ns,
                status=Status(code=span.status_code, message=span.status_message),  # pyright: ignore[reportArgumentType]
            )
            proto.attributes.extend(key_values(span.attributes))
            for event in span.events:
                added = proto.events.add(name=event.name, time_unix_nano=event.time_ns)
                added.attributes.extend(key_values(event.attributes))
            for link in span.links:
                added_link = proto.links.add(
                    trace_id=bytes.fromhex(link.trace_id), span_id=bytes.fromhex(link.span_id)
                )
                added_link.attributes.extend(key_values(link.attributes))
            scope_spans.spans.append(proto)
    return request


def key_values(attributes: Attributes) -> list[KeyValue]:
    return [KeyValue(key=key, value=any_value(value)) for key, value in attributes.items()]


def any_value(value: Value) -> AnyValue:
    result = AnyValue()
    if isinstance(value, bool):
        result.bool_value = value
    elif isinstance(value, int):
        result.int_value = value
    elif isinstance(value, float):
        result.double_value = value
    elif isinstance(value, str):
        result.string_value = value
    elif isinstance(value, bytes):
        result.bytes_value = value
    elif isinstance(value, list):
        result.array_value.values.extend(any_value(item) for item in value)
    elif isinstance(value, dict):
        result.kvlist_value.values.extend(key_values(value))
    return result


def to_otlp_json(request: ExportTraceServiceRequest) -> dict[str, Any]:
    """OTLP/JSON: protobuf JSON mapping, but with hex trace and span IDs."""
    document = json_format.MessageToDict(request)

    def hexify(holder: dict[str, Any], key: str) -> None:
        if key in holder:
            holder[key] = base64.b64decode(holder[key]).hex()

    for resource_spans in document.get("resourceSpans", []):
        for scope_spans in resource_spans.get("scopeSpans", []):
            for span in scope_spans.get("spans", []):
                for key in ("traceId", "spanId", "parentSpanId"):
                    hexify(span, key)
                for link in span.get("links", []):
                    hexify(link, "traceId")
                    hexify(link, "spanId")
    return document
