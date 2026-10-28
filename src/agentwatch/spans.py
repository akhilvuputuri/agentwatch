"""The span record that every ingest path produces and the store keeps."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# OTLP span kinds and status codes, as numbered in the OTLP protobuf.
KIND_UNSPECIFIED = 0
KIND_INTERNAL = 1
KIND_SERVER = 2
KIND_CLIENT = 3
KIND_PRODUCER = 4
KIND_CONSUMER = 5

STATUS_UNSET = 0
STATUS_OK = 1
STATUS_ERROR = 2

type Value = str | bool | int | float | bytes | list[Value] | dict[str, Value] | None
type Attributes = dict[str, Value]


@dataclass(frozen=True, slots=True)
class SpanEvent:
    name: str
    time_ns: int
    attributes: Attributes = field(default_factory=lambda: {})


@dataclass(frozen=True, slots=True)
class SpanLink:
    trace_id: str
    span_id: str
    attributes: Attributes = field(default_factory=lambda: {})


@dataclass(frozen=True, slots=True)
class Span:
    """One finished span. IDs are lowercase hex; times are Unix nanoseconds."""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    start_ns: int
    end_ns: int
    kind: int = KIND_INTERNAL
    status_code: int = STATUS_UNSET
    status_message: str = ""
    attributes: Attributes = field(default_factory=lambda: {})
    resource: Attributes = field(default_factory=lambda: {})
    scope_name: str = ""
    scope_version: str = ""
    events: tuple[SpanEvent, ...] = ()
    links: tuple[SpanLink, ...] = ()

    @property
    def duration_ns(self) -> int:
        return max(0, self.end_ns - self.start_ns)

    @property
    def service_name(self) -> str:
        value = self.resource.get("service.name")
        return value if isinstance(value, str) else ""


def jsonable(value: Value) -> Any:
    """Convert a span value to strict JSON. Bytes become hex; NaN and infinities become strings."""
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    return value
