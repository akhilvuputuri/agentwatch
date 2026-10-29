from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from agentwatch import genai, server
from agentwatch.spans import Attributes, Span
from agentwatch.store import SessionCursor, Store


def trace(
    n: int, start: int, *, session: str | None, user: str | None = None, cost: float = 0.1
) -> list[Span]:
    trace_id = f"{n:032x}"
    root: Attributes = {genai.OPERATION: "invoke_agent", genai.AGENT_NAME: "a"}
    if session is not None:
        root[genai.CONVERSATION_ID] = session
    if user is not None:
        root[genai.USER_ID] = user
    return [
        Span(
            trace_id=trace_id,
            span_id="1" * 16,
            parent_span_id=None,
            name="agent",
            start_ns=start,
            end_ns=start + 100,
            attributes=root,
            resource={"service.name": "svc"},
        ),
        Span(
            trace_id=trace_id,
            span_id="2" * 16,
            parent_span_id="1" * 16,
            name="chat",
            start_ns=start + 1,
            end_ns=start + 50,
            attributes={
                genai.OPERATION: "chat",
                genai.REQUEST_MODEL: "m",
                genai.INPUT_TOKENS: 10,
                genai.COST_USD: cost,
            },
            resource={"service.name": "svc"},
        ),
    ]


def store() -> Store:
    db = Store()
    db.insert(
        [
            *trace(1, 100, session="s1", user="u1"),
            *trace(2, 200, session="s1", user="u1"),
            *trace(3, 300, session="s2", user="u2", cost=0.5),
            *trace(4, 400, session=None),
        ],
        received_ns=1,
    )
    return db


def test_sessions_group_traces() -> None:
    sessions = {s.session_id: s for s in store().sessions()}
    assert set(sessions) == {"s1", "s2"}
    s1 = sessions["s1"]
    assert (s1.traces, s1.model_calls, s1.input_tokens) == (2, 2, 20)
    assert s1.cost_usd == pytest.approx(0.2)
    assert (s1.first_start_ns, s1.last_start_ns, s1.last_end_ns) == (100, 200, 300)
    assert s1.users == ("u1",)
    # Most recent activity first.
    assert [s.session_id for s in store().sessions()] == ["s2", "s1"]


def test_session_from_a_child_span_when_the_root_has_none() -> None:
    db = Store()
    spans = trace(9, 10, session=None)
    child = spans[1]
    db.insert([spans[0]], received_ns=1)
    db.insert(
        [
            Span(
                trace_id=child.trace_id,
                span_id=child.span_id,
                parent_span_id=child.parent_span_id,
                name=child.name,
                start_ns=child.start_ns,
                end_ns=child.end_ns,
                attributes={**child.attributes, "session.id": "from-child"},
            )
        ],
        received_ns=1,
    )
    [summary] = db.traces()
    assert summary.session_id == "from-child"


def test_trace_filters_and_paging() -> None:
    db = store()
    assert [t.trace_id[-1] for t in db.traces(session="s1")] == ["2", "1"]
    assert [t.trace_id[-1] for t in db.traces(user="u2")] == ["3"]
    page = db.sessions(limit=1)
    assert [s.session_id for s in page] == ["s2"]
    rest = db.sessions(limit=1, before=page[-1].cursor)
    assert [s.session_id for s in rest] == ["s1"]
    assert db.sessions(user="u2")[0].session_id == "s2"


def test_session_cursor_round_trip() -> None:
    cursor = SessionCursor(5, "chat:with:colons")
    assert SessionCursor.decode(cursor.encode()) == cursor
    for bad in ("x", "5", "x:1", "-1:a"):
        with pytest.raises(ValueError, match="cursor"):
            SessionCursor.decode(bad)


def test_session_api() -> None:
    http = TestClient(server.create_app(store(), allowed_hosts=("testserver",)))
    listing = http.get("/api/sessions").json()
    assert [s["session_id"] for s in listing["sessions"]] == ["s2", "s1"]
    assert listing["sessions"][0]["first_start_ns"] == "300"
    detail = http.get("/api/sessions/s1").json()
    assert detail["session"]["traces"] == 2
    # Oldest first within a session.
    assert [t["trace_id"][-1] for t in detail["traces"]] == ["1", "2"]
    assert detail["truncated"] is False
    assert detail["traces"][0]["user_id"] == "u1"
    assert http.get("/api/sessions/nope").status_code == 404
    assert (
        http.get("/api/traces", params={"session": "s2"}).json()["traces"][0]["session_id"] == "s2"
    )
    assert http.get("/api/sessions", params={"before": "x"}).status_code == 400


def test_session_ids_with_slashes() -> None:
    db = Store()
    db.insert(trace(1, 1, session="team/chat 1"), received_ns=1)
    http = TestClient(server.create_app(db, allowed_hosts=("testserver",)))
    assert http.get("/api/sessions/team/chat%201").json()["session"]["session_id"] == "team/chat 1"
