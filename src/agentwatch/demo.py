"""A synthetic dataset: a fictional assistant answering questions with tools and a helper agent.

Everything here is generated. It contains no real conversations, people or accounts.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from agentwatch import genai
from agentwatch.spans import (
    KIND_CLIENT,
    KIND_INTERNAL,
    STATUS_ERROR,
    STATUS_OK,
    Attributes,
    Span,
    SpanEvent,
)

SERVICE = "demo-assistant"


@dataclass(frozen=True, slots=True)
class _Model:
    name: str
    provider: str
    input_per_million: float
    cached_per_million: float
    output_per_million: float


_MODELS = (
    _Model("example/large-reasoner", "example", 5.0, 0.5, 25.0),
    _Model("example/fast-chat", "example", 0.3, 0.03, 2.5),
)
_TOOLS = (
    ("search_notes", 40, 400),
    ("read_calendar", 80, 600),
    ("web_search", 600, 4000),
    ("fetch_page", 300, 6000),
    ("create_reminder", 50, 300),
)
_ERRORS = ("rate_limited", "timeout", "provider_error")


class _Ids:
    def __init__(self, rng: random.Random) -> None:
        self._rng = rng

    def trace(self) -> str:
        return f"{self._rng.getrandbits(128):032x}"

    def span(self) -> str:
        return f"{self._rng.getrandbits(64):016x}"


def generate(*, traces: int = 120, seed: int = 7, end_ns: int) -> list[Span]:
    """Generate `traces` traces spread over the 48 hours before `end_ns`."""
    rng = random.Random(seed)
    ids = _Ids(rng)
    spans: list[Span] = []
    window = 48 * 3600 * 1_000_000_000
    starts = sorted(end_ns - rng.randrange(window) for _ in range(traces))
    for start in starts:
        trace_id = ids.trace()
        spans.extend(_turn(rng, ids, trace_id, start))
    return spans


def _turn(rng: random.Random, ids: _Ids, trace_id: str, start: int) -> list[Span]:
    root = ids.span()
    conversation = rng.randrange(1, 9)
    spans: list[Span] = []
    model = _MODELS[0] if rng.random() < 0.7 else _MODELS[1]
    clock = start + rng.randrange(2, 20) * 1_000_000
    failed = False
    steps = rng.choice((1, 2, 2, 3, 3, 4, 5))
    for step in range(steps):
        call, clock, ok = _model_call(rng, ids, trace_id, root, model, clock, step, steps)
        spans.extend(call)
        if not ok:
            failed = True
            break
        if step == steps - 1:
            break
        if rng.random() < 0.15:
            child, clock = _helper_agent(rng, ids, trace_id, root, clock)
            spans.extend(child)
        else:
            for _ in range(rng.choice((1, 1, 2))):
                tool, clock = _tool_call(rng, ids, trace_id, root, clock)
                spans.append(tool)
    end = clock + rng.randrange(1, 15) * 1_000_000
    spans.insert(
        0,
        Span(
            trace_id=trace_id,
            span_id=root,
            parent_span_id=None,
            name="invoke_agent assistant",
            start_ns=start,
            end_ns=end,
            kind=KIND_INTERNAL,
            status_code=STATUS_ERROR if failed else STATUS_OK,
            status_message="model call failed" if failed else "",
            attributes={
                genai.OPERATION: "invoke_agent",
                genai.AGENT_NAME: "assistant",
                genai.CONVERSATION_ID: f"demo-{conversation}",
                genai.USER_ID: f"user-{conversation % 3 + 1}",
            },
            resource=_resource(),
            scope_name="agentwatch.demo",
        ),
    )
    return spans


def _model_call(
    rng: random.Random,
    ids: _Ids,
    trace_id: str,
    parent: str,
    model: _Model,
    clock: int,
    step: int,
    steps: int,
) -> tuple[list[Span], int, bool]:
    spans: list[Span] = []
    attempts = 2 if rng.random() < 0.06 else 1
    for attempt in range(attempts):
        last = attempt == attempts - 1
        fails = not last or rng.random() < 0.02
        input_tokens = rng.randrange(4_000, 28_000) + step * rng.randrange(500, 3_000)
        cached = (
            int(input_tokens * rng.uniform(0.55, 0.95)) if step > 0 or rng.random() < 0.8 else 0
        )
        output = rng.randrange(40, 1_400)
        reasoning = int(output * rng.uniform(0.1, 0.6)) if model is _MODELS[0] else 0
        latency = int(rng.lognormvariate(0.6, 0.5) * 1e9 * (2.2 if model is _MODELS[0] else 0.8))
        attrs: Attributes = {
            genai.OPERATION: "chat",
            genai.PROVIDER: model.provider,
            genai.REQUEST_MODEL: model.name,
        }
        if fails:
            latency = int(latency * rng.uniform(0.05, 0.5))
            error = rng.choice(_ERRORS)
            attrs[genai.ERROR_TYPE] = error
            status, message = STATUS_ERROR, error
        else:
            fresh = input_tokens - cached
            cost = (
                fresh * model.input_per_million
                + cached * model.cached_per_million
                + output * model.output_per_million
            ) / 1e6
            attrs.update(
                {
                    genai.RESPONSE_MODEL: model.name,
                    genai.INPUT_TOKENS: input_tokens,
                    genai.OUTPUT_TOKENS: output,
                    genai.CACHE_READ_TOKENS: cached,
                    genai.CACHE_WRITE_TOKENS: rng.randrange(0, 2_000) if step == 0 else 0,
                    genai.REASONING_TOKENS: reasoning,
                    genai.COST_USD: round(cost, 6),
                    genai.FINISH_REASONS: ["stop" if step == steps - 1 else "tool_calls"],
                }
            )
            status, message = STATUS_OK, ""
        spans.append(
            Span(
                trace_id=trace_id,
                span_id=ids.span(),
                parent_span_id=parent,
                name=f"chat {model.name}",
                start_ns=clock,
                end_ns=clock + latency,
                kind=KIND_CLIENT,
                status_code=status,
                status_message=message,
                attributes=attrs,
                resource=_resource(),
                scope_name="agentwatch.demo",
                events=(
                    (
                        SpanEvent(
                            name="retry",
                            time_ns=clock + latency,
                            attributes={"attempt": attempt + 1},
                        ),
                    )
                    if fails and not last
                    else ()
                ),
            )
        )
        clock += latency + rng.randrange(1, 5) * 1_000_000
        if fails and last:
            return spans, clock, False
    return spans, clock, True


def _tool_call(
    rng: random.Random, ids: _Ids, trace_id: str, parent: str, clock: int
) -> tuple[Span, int]:
    name, low, high = rng.choice(_TOOLS)
    latency = rng.randrange(low, high) * 1_000_000
    fails = rng.random() < 0.04
    span = Span(
        trace_id=trace_id,
        span_id=ids.span(),
        parent_span_id=parent,
        name=f"execute_tool {name}",
        start_ns=clock,
        end_ns=clock + latency,
        kind=KIND_INTERNAL,
        status_code=STATUS_ERROR if fails else STATUS_OK,
        status_message="tool failed" if fails else "",
        attributes={
            genai.OPERATION: "execute_tool",
            genai.TOOL_NAME: name,
            genai.TOOL_CALL_ID: f"call_{rng.getrandbits(40):010x}",
            **({genai.ERROR_TYPE: "tool_error"} if fails else {}),
        },
        resource=_resource(),
        scope_name="agentwatch.demo",
    )
    return span, clock + latency + rng.randrange(1, 4) * 1_000_000


def _helper_agent(
    rng: random.Random, ids: _Ids, trace_id: str, parent: str, clock: int
) -> tuple[list[Span], int]:
    agent = ids.span()
    start = clock
    clock += rng.randrange(1, 5) * 1_000_000
    children: list[Span] = []
    steps = rng.choice((2, 3))
    ok = True
    for step in range(steps):
        call, clock, ok = _model_call(rng, ids, trace_id, agent, _MODELS[1], clock, step, steps)
        children.extend(call)
        if not ok or step == steps - 1:
            break
        tool, clock = _tool_call(rng, ids, trace_id, agent, clock)
        children.append(tool)
    span = Span(
        trace_id=trace_id,
        span_id=agent,
        parent_span_id=parent,
        name="invoke_agent researcher",
        start_ns=start,
        end_ns=clock,
        kind=KIND_INTERNAL,
        status_code=STATUS_OK if ok else STATUS_ERROR,
        attributes={genai.OPERATION: "invoke_agent", genai.AGENT_NAME: "researcher"},
        resource=_resource(),
        scope_name="agentwatch.demo",
    )
    return [span, *children], clock + rng.randrange(1, 5) * 1_000_000


def _resource() -> Attributes:
    return {"service.name": SERVICE, "service.version": "demo"}
