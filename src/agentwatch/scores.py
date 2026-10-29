"""Scores: named judgements attached to a trace or a span.

A score is numeric (any finite number), boolean (stored as 1 or 0) or categorical (a label),
and records where it came from: the REST API, a person annotating in the UI, or an evaluator.
Evaluators that follow the GenAI conventions report results as `gen_ai.evaluation.result`
events, either as OTLP log records or as span events; both become scores here.
"""

from __future__ import annotations

import hashlib
import math
import time
import uuid
from dataclasses import dataclass
from typing import Any

from agentwatch.spans import Attributes, Span

NUMERIC = "NUMERIC"
BOOLEAN = "BOOLEAN"
CATEGORICAL = "CATEGORICAL"
DATA_TYPES = (NUMERIC, BOOLEAN, CATEGORICAL)

API = "API"
ANNOTATION = "ANNOTATION"
EVAL = "EVAL"
SOURCES = (API, ANNOTATION, EVAL)

EVALUATION_EVENT = "gen_ai.evaluation.result"
EVALUATION_NAME = "gen_ai.evaluation.name"
EVALUATION_VALUE = "gen_ai.evaluation.score.value"
EVALUATION_LABEL = "gen_ai.evaluation.score.label"
EVALUATION_EXPLANATION = "gen_ai.evaluation.explanation"

MAX_NAME = 200
MAX_TEXT = 2000
MAX_ID = 200


class ScoreError(ValueError):
    """A score that cannot be stored, with the reason."""


@dataclass(frozen=True, slots=True)
class Score:
    id: str
    trace_id: str
    span_id: str | None
    name: str
    data_type: str
    value: float | None
    label: str | None
    source: str
    comment: str | None
    created_ns: int


def from_api(body: Any, *, source: str = API, now_ns: int | None = None) -> Score:
    """Validate a score posted to the REST API (or the UI, as an annotation)."""
    if not isinstance(body, dict):
        raise ScoreError("a score must be a JSON object")
    data: dict[str, Any] = body  # pyright: ignore[reportUnknownVariableType]
    trace_id = _hex(data.get("trace_id"), 32, "trace_id")
    span_raw = data.get("span_id")
    span_id = None if span_raw in (None, "") else _hex(span_raw, 16, "span_id")
    name = _text(data.get("name"), MAX_NAME, "name", required=True)
    assert name is not None
    raw_value = data.get("value")
    label = _text(data.get("label"), MAX_TEXT, "label")
    comment = _text(data.get("comment"), MAX_TEXT, "comment")
    declared = data.get("data_type")
    if declared is not None and declared not in DATA_TYPES:
        raise ScoreError(f"data_type must be one of {', '.join(DATA_TYPES)}")
    if isinstance(raw_value, bool):
        data_type, value = BOOLEAN, 1.0 if raw_value else 0.0
    elif isinstance(raw_value, int | float):
        value = float(raw_value)
        if not math.isfinite(value):
            raise ScoreError("value must be a finite number")
        data_type = NUMERIC
    elif raw_value is None:
        if label is None:
            raise ScoreError("a score needs a value or a label")
        data_type, value = CATEGORICAL, None
    else:
        raise ScoreError("value must be a number or a boolean")
    if declared is not None and declared != data_type:
        if declared == BOOLEAN and data_type == NUMERIC and value in (0.0, 1.0):
            data_type = BOOLEAN
        else:
            raise ScoreError(f"data_type {declared} does not match the value given")
    given_id = data.get("id")
    score_id = (
        _text(given_id, MAX_ID, "id", required=True) if given_id is not None else uuid.uuid4().hex
    )
    assert score_id is not None
    return Score(
        id=score_id,
        trace_id=trace_id,
        span_id=span_id,
        name=name,
        data_type=data_type,
        value=value,
        label=label,
        source=source,
        comment=comment,
        created_ns=time.time_ns() if now_ns is None else now_ns,
    )


def from_evaluation(
    trace_id: str, span_id: str | None, time_ns: int, attributes: Attributes
) -> Score | None:
    """A score from a gen_ai.evaluation.result event, or None when it has no name or result."""
    name = attributes.get(EVALUATION_NAME)
    if not isinstance(name, str) or not name or len(name) > MAX_NAME:
        return None
    raw_value = attributes.get(EVALUATION_VALUE)
    raw_label = attributes.get(EVALUATION_LABEL)
    value: float | None = None
    if isinstance(raw_value, bool):
        value = 1.0 if raw_value else 0.0
    elif isinstance(raw_value, int | float) and math.isfinite(raw_value):
        value = float(raw_value)
    label = raw_label if isinstance(raw_label, str) and raw_label else None
    if value is None and label is None:
        return None
    explanation = attributes.get(EVALUATION_EXPLANATION)
    comment = explanation[:MAX_TEXT] if isinstance(explanation, str) and explanation else None
    data_type = (
        BOOLEAN if isinstance(raw_value, bool) else NUMERIC if value is not None else CATEGORICAL
    )
    # The same event sent twice keeps one score.
    key = "|".join([trace_id, span_id or "", name, str(time_ns), repr(value), label or ""])
    return Score(
        id=hashlib.sha256(key.encode()).hexdigest()[:32],
        trace_id=trace_id,
        span_id=span_id,
        name=name,
        data_type=data_type,
        value=value,
        label=label[:MAX_TEXT] if label else None,
        source=EVAL,
        comment=comment,
        created_ns=time_ns,
    )


def from_span_events(span: Span) -> list[Score]:
    """Scores carried as gen_ai.evaluation.result events on a span."""
    found: list[Score] = []
    for event in span.events:
        if event.name == EVALUATION_EVENT:
            score = from_evaluation(span.trace_id, span.span_id, event.time_ns, event.attributes)
            if score is not None:
                found.append(score)
    return found


def _hex(value: Any, length: int, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != length
        or any(c not in "0123456789abcdef" for c in value.lower())
        or value.strip("0") == ""
    ):
        raise ScoreError(f"{field} must be {length} hex characters, not all zero")
    return value.lower()


def _text(value: Any, limit: int, field: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ScoreError(f"{field} is required")
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise ScoreError(f"{field} must be a non-empty string")
    if len(value) > limit:
        raise ScoreError(f"{field} is longer than {limit} characters")
    return value or None
