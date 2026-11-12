"""A synthetic dataset: a fictional assistant answering questions with tools and a helper agent.

Everything here is generated. It contains no real conversations, people or accounts.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, replace
from typing import Any

from agentwatch import datasets, experiments, genai, scores
from agentwatch.spans import (
    KIND_CLIENT,
    KIND_INTERNAL,
    STATUS_ERROR,
    STATUS_OK,
    Attributes,
    Span,
    SpanEvent,
    SpanLink,
)
from agentwatch.store import Store

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
    # Memory draws from its own generator, so the rest of the data is as it was without it.
    remembered = _Memory(random.Random(seed + 1))
    spans: list[Span] = []
    window = 48 * 3600 * 1_000_000_000
    starts = sorted(end_ns - rng.randrange(window) for _ in range(traces))
    # A nightly learner, half a day before the end: later turns read what it added.
    learner_at: int | None = end_ns - 12 * 3600 * 1_000_000_000
    for start in starts:
        if learner_at is not None and start >= learner_at:
            spans.extend(remembered.learner(learner_at))
            learner_at = None
        trace_id = ids.trace()
        turn = _turn(rng, ids, trace_id, start)
        spans.extend(remembered.turn(turn))
    if learner_at is not None:
        spans.extend(remembered.learner(learner_at))
    return spans


# Synthetic things the assistant remembers: key, kind and successive values.
_MEMORIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("preferred_name", "fact", ("Call me Sam.",)),
    ("timezone", "fact", ("Works in UTC+1.", "Works in UTC+2 over the summer.")),
    ("meeting_hours", "fact", ("Prefers meetings after 10:00.",)),
    (
        "reminder_style",
        "fact",
        ("Short reminders, no emoji.", "Short reminders with the time first."),
    ),
    (
        "lesson:calendar:double-check",
        "lesson",
        ("Read the calendar again before saying a slot is free.",),
    ),
    ("lesson:travel:times", "lesson", ("Give local times when planning a trip.",)),
)
MEMORY_STORE = "demo-memory"


class _Memory:
    """Synthetic memory: each turn reads every current item into its prompt, some turns save
    one, and a nightly learner revises a lesson citing the turns it learned from."""

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng
        self._ids = _Ids(rng)
        # key -> (kind, value index); the first three facts exist from the start.
        self._current: dict[str, tuple[str, int]] = {
            key: (kind, 0) for key, kind, _ in _MEMORIES[:3]
        }
        self._sources: list[tuple[str, str]] = []

    def _record(self, key: str, *, with_size: bool = True) -> dict[str, Any]:
        kind, index = self._current[key]
        value = next(values for k, _, values in _MEMORIES if k == key)[index]
        metadata: dict[str, Any] = {"kind": kind, "revision": index + 1}
        if with_size:
            metadata["characters"] = len(value)
        return {"id": key, "content": value, "metadata": metadata}

    def turn(self, spans: list[Span]) -> list[Span]:
        root = spans[0]
        self._sources.append((root.trace_id, root.span_id))
        selected = [self._record(key) for key in sorted(self._current)]
        read = Span(
            trace_id=root.trace_id,
            span_id=self._ids.span(),
            parent_span_id=root.span_id,
            name="search_memory demo-memory",
            start_ns=root.start_ns + 1_000_000,
            end_ns=root.start_ns + 2_000_000,
            kind=KIND_INTERNAL,
            status_code=STATUS_OK,
            attributes={
                genai.OPERATION: "search_memory",
                "gen_ai.memory.store.id": MEMORY_STORE,
                "gen_ai.memory.record.count": len(selected),
                "gen_ai.memory.records": json.dumps(selected),
            },
            resource=_resource(),
            scope_name="agentwatch.demo",
        )
        added = [read]
        if self._rng.random() < 0.12:
            added.extend(self._save(root))
        return [root, *added, *spans[1:]]

    def _save(self, root: Span) -> list[Span]:
        key, kind, values = self._rng.choice(_MEMORIES[:4])
        if key in self._current:
            index = self._current[key][1]
            if index + 1 >= len(values):
                return []
            self._current[key] = (kind, index + 1)
            operation = "update_memory"
        else:
            self._current[key] = (kind, 0)
            operation = "create_memory"
        at = root.end_ns - 3_000_000
        tool = self._ids.span()
        return [
            Span(
                trace_id=root.trace_id,
                span_id=tool,
                parent_span_id=root.span_id,
                name="execute_tool remember",
                start_ns=at,
                end_ns=at + 2_000_000,
                kind=KIND_INTERNAL,
                status_code=STATUS_OK,
                attributes={genai.OPERATION: "execute_tool", genai.TOOL_NAME: "remember"},
                resource=_resource(),
                scope_name="agentwatch.demo",
            ),
            self._write(root.trace_id, tool, operation, key, at + 500_000),
        ]

    def _write(
        self,
        trace_id: str,
        parent: str,
        operation: str,
        key: str,
        at: int,
        links: tuple[SpanLink, ...] = (),
    ) -> Span:
        return Span(
            trace_id=trace_id,
            span_id=self._ids.span(),
            parent_span_id=parent,
            name=f"{operation} demo-memory",
            start_ns=at,
            end_ns=at + 1_000_000,
            kind=KIND_INTERNAL,
            status_code=STATUS_OK,
            attributes={
                genai.OPERATION: operation,
                "gen_ai.memory.store.id": MEMORY_STORE,
                "gen_ai.memory.record.id": key,
                "gen_ai.memory.record.count": 1,
                "gen_ai.memory.records": json.dumps([self._record(key, with_size=False)]),
            },
            resource=_resource(),
            scope_name="agentwatch.demo",
            links=links,
        )

    def learner(self, start: int) -> list[Span]:
        """One nightly learner run: it adds the lessons, linked to the turns it read."""
        trace_id = self._ids.trace()
        root = self._ids.span()
        sources = tuple(
            SpanLink(trace_id=trace, span_id=span) for trace, span in self._sources[-3:]
        )
        writes: list[Span] = []
        for offset, (key, kind, _) in enumerate(_MEMORIES[4:]):
            self._current[key] = (kind, 0)
            at = start + (offset + 1) * 2_000_000_000
            writes.append(self._write(trace_id, root, "create_memory", key, at, sources))
        return [
            Span(
                trace_id=trace_id,
                span_id=root,
                parent_span_id=None,
                name="invoke_agent learner",
                start_ns=start,
                end_ns=start + 10_000_000_000,
                kind=KIND_INTERNAL,
                status_code=STATUS_OK,
                attributes={genai.OPERATION: "invoke_agent", genai.AGENT_NAME: "learner"},
                resource=_resource(),
                scope_name="agentwatch.demo",
            ),
            *writes,
        ]


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
    question, answer = rng.choice(_REQUESTS)
    spans = _with_content(spans, root, question, answer)
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
                **_messages(question, answer, failed),
            },
            resource=_resource(),
            scope_name="agentwatch.demo",
            events=_evaluations(rng, end, failed),
        ),
    )
    return spans


def _evaluations(rng: random.Random, at: int, failed: bool) -> tuple[SpanEvent, ...]:
    """Synthetic evaluator results, as gen_ai.evaluation.result span events."""
    events: list[SpanEvent] = []
    if not failed and rng.random() < 0.6:
        score = round(min(1.0, max(0.0, rng.gauss(0.78, 0.15))), 2)
        events.append(
            SpanEvent(
                name=scores.EVALUATION_EVENT,
                time_ns=at,
                attributes={
                    scores.EVALUATION_NAME: "helpfulness",
                    scores.EVALUATION_VALUE: score,
                    scores.EVALUATION_EXPLANATION: "Synthetic judge score.",
                },
            )
        )
    if rng.random() < 0.25:
        events.append(
            SpanEvent(
                name=scores.EVALUATION_EVENT,
                time_ns=at + 1,
                attributes={
                    scores.EVALUATION_NAME: "user_feedback",
                    scores.EVALUATION_VALUE: (not failed) and rng.random() < 0.8,
                },
            )
        )
    if rng.random() < 0.4:
        events.append(
            SpanEvent(
                name=scores.EVALUATION_EVENT,
                time_ns=at + 2,
                attributes={
                    scores.EVALUATION_NAME: "topic",
                    scores.EVALUATION_LABEL: rng.choice(
                        ("calendar", "research", "notes", "travel")
                    ),
                },
            )
        )
    return tuple(events)


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


_REQUESTS = (
    ("What is on my calendar tomorrow?", "You have two meetings tomorrow: 10:00 and 15:30."),
    ("Find notes about the garden project.", "I found three notes; the latest is from last week."),
    ("Remind me to call the bank on Friday.", "Done: a reminder for Friday at 09:00."),
    (
        "Summarise this article about solar panels.",
        "The article compares panel types and payback periods.",
    ),
    ("Plan a two-day trip to the coast.", "Here is a two-day plan with a stop at the lighthouse."),
    ("Which tasks are overdue?", "Two tasks are overdue: the tax form and the library book."),
)


def _messages(question: str, answer: str, failed: bool) -> Attributes:
    """Synthetic captured messages, in the GenAI conventions' opt-in message format."""
    captured: Attributes = {
        "gen_ai.input.messages": json.dumps(
            [{"role": "user", "parts": [{"type": "text", "content": question}]}]
        )
    }
    if not failed:
        captured["gen_ai.output.messages"] = json.dumps(
            [{"role": "assistant", "parts": [{"type": "text", "content": answer}]}]
        )
    return captured


# Synthetic arguments and results for each demo tool.
_TOOL_CONTENT: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "search_notes": (
        {"query": "garden project"},
        {
            "notes": [
                {"title": "Garden plan", "updated": "last week"},
                {"title": "Seed order", "updated": "last month"},
                {"title": "Watering rota", "updated": "in spring"},
            ]
        },
    ),
    "read_calendar": (
        {"day": "tomorrow"},
        {
            "events": [
                {"at": "10:00", "title": "Design review"},
                {"at": "15:30", "title": "Dentist"},
            ]
        },
    ),
    "web_search": (
        {"query": "solar panel payback period"},
        {"results": [{"title": "Panel types compared", "url": "https://example.com/panels"}]},
    ),
    "fetch_page": (
        {"url": "https://example.com/panels"},
        {
            "status": 200,
            "text": "Monocrystalline panels cost more and pay back sooner.\n\nSee the table.",
        },
    ),
    "create_reminder": (
        {"text": "Call the bank", "at": "Friday 09:00"},
        {"created": True, "id": "reminder-demo"},
    ),
}
_SYSTEM = "You are a demo assistant. Use the tools to answer, and say so when one fails."


def _with_content(spans: list[Span], root: str, question: str, answer: str) -> list[Span]:
    """The turn's spans with synthetic captured content: what each model call was sent and
    said (text, or the tool calls that follow it), and each tool's arguments and result.

    Draws no random numbers, so the rest of the demo data is as it was without content.
    """

    def text(role: str, content: str) -> dict[str, Any]:
        return {"role": role, "parts": [{"type": "text", "content": content}]}

    found: list[Span] = []
    for index, span in enumerate(spans):
        operation = span.attributes.get(genai.OPERATION)
        extra: Attributes = {}
        if operation == "execute_tool":
            arguments, result = _TOOL_CONTENT[str(span.attributes[genai.TOOL_NAME])]
            if span.status_code == STATUS_ERROR:
                result = {"error": "The demo service did not answer in time."}
            extra = {
                "gen_ai.tool.call.arguments": json.dumps(arguments),
                "gen_ai.tool.call.result": json.dumps(result),
            }
        elif operation == "chat":
            extra["gen_ai.input.messages"] = json.dumps(
                [text("system", _SYSTEM), text("user", question)]
            )
            if span.status_code != STATUS_ERROR:
                calls: list[dict[str, Any]] = []
                for later in spans[index + 1 :]:
                    if later.parent_span_id != span.parent_span_id:
                        continue
                    following = later.attributes.get(genai.OPERATION)
                    if following == "chat":
                        break
                    if following == "execute_tool":
                        name = str(later.attributes[genai.TOOL_NAME])
                        calls.append(
                            {
                                "type": "tool_call",
                                "id": later.attributes[genai.TOOL_CALL_ID],
                                "name": name,
                                "arguments": _TOOL_CONTENT[name][0],
                            }
                        )
                    elif following == "invoke_agent":
                        calls.append(
                            {
                                "type": "tool_call",
                                "id": f"call_{later.span_id}",
                                "name": "ask_researcher",
                                "arguments": {"question": question},
                            }
                        )
                said = answer if span.parent_span_id == root else "Here is what I found."
                parts = calls or [{"type": "text", "content": said}]
                extra["gen_ai.output.messages"] = json.dumps(
                    [{"role": "assistant", "parts": parts}]
                )
        found.append(replace(span, attributes={**span.attributes, **extra}) if extra else span)
    return found


def seed_datasets(store: Store) -> None:
    """A demo dataset: the synthetic requests with their expected answers."""
    dataset = datasets.create_dataset(
        store,
        {"name": "Demo requests", "description": "Synthetic requests and expected answers."},
    )
    items = datasets.add_items(
        store,
        dataset.id,
        [
            {
                "input": [{"role": "user", "parts": [{"type": "text", "content": question}]}],
                "expected_output": answer,
            }
            for question, answer in _REQUESTS
        ],
    )
    # Two synthetic runs to compare: the second answers more items correctly.
    rng = random.Random(11)
    start = time.time_ns() - 3600 * 10**9
    for run_name, accuracy in (("baseline", 0.5), ("prompt v2", 0.83)):
        run = experiments.create_run(store, dataset.id, {"name": run_name})
        results: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            correct = rng.random() < accuracy
            latency = int(rng.uniform(0.8, 3.0) * 1e9)
            item_start = start + index * 5 * 10**9
            results.append(
                {
                    "item_id": item.id,
                    "output": item.expected_output if correct else "I could not find that.",
                    "start_ns": item_start,
                    "end_ns": item_start + latency,
                    "scores": [
                        {"name": "exact", "value": correct},
                        {
                            "name": "helpfulness",
                            "value": round(
                                rng.uniform(0.7, 1.0) if correct else rng.uniform(0.1, 0.5), 2
                            ),
                        },
                    ],
                }
            )
        experiments.add_run_items(store, run.id, results)


def _resource() -> Attributes:
    return {"service.name": SERVICE, "service.version": "demo"}
