"""Read OpenTelemetry GenAI semantic-convention attributes off a span.

Names follow open-telemetry/semantic-conventions-genai (development status). Older names that
instrumentations still emit are accepted as fallbacks. Cost has no standard
attribute, so `agentwatch.usage.cost_usd` is this project's extension.
"""

from __future__ import annotations

import math
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

LEGACY_ALIASES: dict[str, tuple[str, ...]] = {
    INPUT_TOKENS: ("gen_ai.usage.prompt_tokens",),
    OUTPUT_TOKENS: ("gen_ai.usage.completion_tokens",),
    # Renamed in semantic-conventions-genai (changelog 440).
    CACHE_WRITE_TOKENS: ("gen_ai.usage.cache_creation.input_tokens",),
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

    @property
    def model(self) -> str | None:
        return self.response_model or self.request_model

    @property
    def is_model_call(self) -> bool:
        return self.operation in MODEL_OPERATIONS

    @property
    def counts_toward_totals(self) -> bool:
        """Spans whose cost adds up to a trace total (tokens count model calls only)."""
        return self.operation not in AGGREGATE_OPERATIONS


def read(span: Span) -> GenAI:
    attrs = span.attributes
    return GenAI(
        operation=_text(attrs, OPERATION),
        provider=_text(attrs, PROVIDER) or _text(attrs, PROVIDER_LEGACY),
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
    )


def _lookup(attrs: Attributes, key: str) -> Value:
    if key in attrs:
        return attrs[key]
    for alias in LEGACY_ALIASES.get(key, ()):
        if alias in attrs:
            return attrs[alias]
    return None


def _text(attrs: Attributes, key: str) -> str | None:
    value = _lookup(attrs, key)
    if isinstance(value, str) and value != "":
        return value
    return None


def _texts(attrs: Attributes, key: str) -> tuple[str, ...]:
    value = _lookup(attrs, key)
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str) and item)
    return ()


# Counts are stored as signed 64-bit integers; anything larger is not a real count.
_MAX_COUNT = 2**63 - 1


def _count(attrs: Attributes, key: str) -> int | None:
    value = _lookup(attrs, key)
    if isinstance(value, bool):
        return None
    number: int | None = None
    if isinstance(value, int):
        number = value
    elif (isinstance(value, float) and math.isfinite(value) and value.is_integer()) or (
        isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 19
    ):
        number = int(value)
    return number if number is not None and 0 <= number <= _MAX_COUNT else None


def _money(attrs: Attributes, key: str) -> float | None:
    value = _lookup(attrs, key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float) and math.isfinite(value) and 0 <= value < 1e15:
        return float(value)
    return None
