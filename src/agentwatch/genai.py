"""Read LLM attributes off a span, whichever convention the instrumentation used.

Three conventions are in common use, and each field is read from the first one present:

1. OpenTelemetry GenAI (open-telemetry/semantic-conventions-genai, development status),
   including names it has since renamed or deprecated;
2. OpenInference (Arize, used by Phoenix and many instrumentations);
3. OpenLLMetry (Traceloop), for the names it kept outside the GenAI conventions.

Cost has no GenAI attribute, so `agentwatch.usage.cost_usd` is this project's extension,
with OpenInference's `llm.cost.total` as a fallback.

DERIVATION_VERSION changes whenever these rules change, so stored spans are re-read.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from agentwatch.spans import Attributes, Span, Value

OPERATION = "gen_ai.operation.name"
PROVIDER = "gen_ai.provider.name"
PROVIDER_LEGACY = "gen_ai.system"
REQUEST_MODEL = "gen_ai.request.model"
RESPONSE_MODEL = "gen_ai.response.model"
FINISH_REASONS = "gen_ai.response.finish_reasons"
INPUT_TOKENS = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
CACHE_READ_TOKENS = "gen_ai.usage.cache_read.input_tokens"
CACHE_WRITE_TOKENS = "gen_ai.usage.cache_write.input_tokens"
REASONING_TOKENS = "gen_ai.usage.reasoning.output_tokens"
TOOL_NAME = "gen_ai.tool.name"
TOOL_CALL_ID = "gen_ai.tool.call.id"
AGENT_NAME = "gen_ai.agent.name"
AGENT_ID = "gen_ai.agent.id"
CONVERSATION_ID = "gen_ai.conversation.id"
ERROR_TYPE = "error.type"
COST_USD = "agentwatch.usage.cost_usd"
USER_ID = "user.id"

# Bump when the rules below change; the store then re-reads every stored span.
DERIVATION_VERSION = 2

# Other names for each field, in priority order after the GenAI name.
ALIASES: dict[str, tuple[str, ...]] = {
    PROVIDER: (PROVIDER_LEGACY, "llm.provider", "llm.system"),
    REQUEST_MODEL: ("llm.request.model_name", "llm.model_name"),
    RESPONSE_MODEL: ("llm.response.model_name",),
    INPUT_TOKENS: (
        "gen_ai.usage.prompt_tokens",
        "llm.token_count.prompt",
        "llm.usage.prompt_tokens",
    ),
    OUTPUT_TOKENS: (
        "gen_ai.usage.completion_tokens",
        "llm.token_count.completion",
        "llm.usage.completion_tokens",
    ),
    CACHE_READ_TOKENS: ("llm.token_count.prompt_details.cache_read",),
    # Renamed in semantic-conventions-genai (changelog 440).
    CACHE_WRITE_TOKENS: (
        "gen_ai.usage.cache_creation.input_tokens",
        "llm.token_count.prompt_details.cache_write",
    ),
    REASONING_TOKENS: ("llm.token_count.completion_details.reasoning",),
    COST_USD: ("llm.cost.total",),
    FINISH_REASONS: ("llm.finish_reason",),
    TOOL_NAME: ("tool.name",),
    AGENT_NAME: ("agent.name",),
    CONVERSATION_ID: ("session.id",),
}

# OpenInference span kinds as GenAI operation names. Kinds with no GenAI equivalent keep
# their own name, lowercased, as the conventions allow for custom operations.
OPENINFERENCE_KINDS: dict[str, str] = {
    "LLM": "chat",
    "EMBEDDING": "embeddings",
    "TOOL": "execute_tool",
    "AGENT": "invoke_agent",
    "CHAIN": "invoke_workflow",
    "RETRIEVER": "retrieval",
}
# OpenLLMetry's span kinds, and its request types for model calls.
TRACELOOP_KINDS: dict[str, str] = {
    "workflow": "invoke_workflow",
    "agent": "invoke_agent",
    "tool": "execute_tool",
}
TRACELOOP_REQUEST_TYPES: dict[str, str] = {
    "chat": "chat",
    "completion": "text_completion",
    "embedding": "embeddings",
}

# Operations that are one call to a model; tokens and cost are summed over these.
MODEL_OPERATIONS = frozenset(
    {"chat", "text_completion", "generate_content", "embeddings", "completion"}
)
# Operations that wrap other spans; their usage would double-count children.
AGGREGATE_OPERATIONS = frozenset({"invoke_agent", "create_agent", "invoke_workflow"})


@dataclass(frozen=True, slots=True)
class GenAI:
    operation: str | None
    provider: str | None
    request_model: str | None
    response_model: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    reasoning_tokens: int | None
    cost_usd: float | None
    finish_reasons: tuple[str, ...]
    tool_name: str | None
    agent_name: str | None
    conversation_id: str | None
    error_type: str | None
    user_id: str | None = None

    @property
    def model(self) -> str | None:
        return self.response_model or self.request_model

    @property
    def is_model_call(self) -> bool:
        if self.operation is not None:
            return self.operation in MODEL_OPERATIONS
        # Older instrumentations send a model and usage without an operation name.
        return self.model is not None and (
            self.input_tokens is not None or self.output_tokens is not None
        )

    @property
    def counts_toward_totals(self) -> bool:
        """Spans whose cost adds up to a trace total (tokens count model calls only)."""
        return self.operation not in AGGREGATE_OPERATIONS


def read(span: Span) -> GenAI:
    attrs = span.attributes
    return GenAI(
        operation=_operation(attrs),
        provider=_text(attrs, PROVIDER),
        request_model=_text(attrs, REQUEST_MODEL),
        response_model=_text(attrs, RESPONSE_MODEL),
        input_tokens=_count(attrs, INPUT_TOKENS),
        output_tokens=_count(attrs, OUTPUT_TOKENS),
        cache_read_tokens=_count(attrs, CACHE_READ_TOKENS),
        cache_write_tokens=_count(attrs, CACHE_WRITE_TOKENS),
        reasoning_tokens=_count(attrs, REASONING_TOKENS),
        cost_usd=_money(attrs, COST_USD),
        finish_reasons=_texts(attrs, FINISH_REASONS),
        tool_name=_text(attrs, TOOL_NAME),
        agent_name=_text(attrs, AGENT_NAME),
        conversation_id=_text(attrs, CONVERSATION_ID),
        error_type=_text(attrs, ERROR_TYPE),
        user_id=_text(attrs, USER_ID),
    )


def _operation(attrs: Attributes) -> str | None:
    operation = _text(attrs, OPERATION)
    if operation is not None:
        return operation
    kind = attrs.get("openinference.span.kind")
    if isinstance(kind, str) and kind:
        return OPENINFERENCE_KINDS.get(kind.upper(), kind.lower())
    request_type = attrs.get("llm.request.type")
    if isinstance(request_type, str) and request_type in TRACELOOP_REQUEST_TYPES:
        return TRACELOOP_REQUEST_TYPES[request_type]
    traceloop_kind = attrs.get("traceloop.span.kind")
    if isinstance(traceloop_kind, str):
        return TRACELOOP_KINDS.get(traceloop_kind)
    return None


def _values(attrs: Attributes, key: str) -> list[Value]:
    """The value under each name for this field that is present, in priority order."""
    return [attrs[name] for name in (key, *ALIASES.get(key, ())) if name in attrs]


def _first[T](attrs: Attributes, key: str, parse: Callable[[Value], T | None]) -> T | None:
    for value in _values(attrs, key):
        parsed = parse(value)
        if parsed is not None:
            return parsed
    return None


def _text(attrs: Attributes, key: str) -> str | None:
    return _first(attrs, key, _as_text)


def _texts(attrs: Attributes, key: str) -> tuple[str, ...]:
    return _first(attrs, key, _as_texts) or ()


def _count(attrs: Attributes, key: str) -> int | None:
    return _first(attrs, key, _as_count)


def _money(attrs: Attributes, key: str) -> float | None:
    return _first(attrs, key, _as_money)


def _as_text(value: Value) -> str | None:
    return value if isinstance(value, str) and value != "" else None


def _as_texts(value: Value) -> tuple[str, ...] | None:
    if isinstance(value, str):
        return (value,) if value else None
    if isinstance(value, list):
        texts = tuple(item for item in value if isinstance(item, str) and item)
        return texts or None
    return None


# Counts are stored as signed 64-bit integers; anything larger is not a real count.
_MAX_COUNT = 2**63 - 1


def _as_count(value: Value) -> int | None:
    if isinstance(value, bool):
        return None
    number: int | None = None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():  # noqa: SIM114
        number = int(value)
    elif isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 19:
        number = int(value)
    return number if number is not None and 0 <= number <= _MAX_COUNT else None


def _as_money(value: Value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float) and math.isfinite(value) and 0 <= value < 1e15:
        return float(value)
    return None
