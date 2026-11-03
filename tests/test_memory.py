from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from starlette.testclient import TestClient

from agentwatch import memory, server
from agentwatch.spans import STATUS_ERROR, Span, SpanLink
from agentwatch.store import Store
from tests.builders import encode

T = "a" * 32
OTHER = "b" * 32
STORE = "prefs"
START = 1_761_000_000_000_000_000
MS = 1_000_000


def span(
    span_id: str,
    parent: str | None,
    operation: str,
    start: int,
    end: int,
    **attributes: object,
) -> Span:
    return Span(
        trace_id=T,
        span_id=span_id,
        parent_span_id=parent,
        name=operation,
        start_ns=START + start * MS,
        end_ns=START + end * MS,
        attributes={"gen_ai.operation.name": operation, **attributes},  # pyright: ignore[reportArgumentType]
        resource={"service.name": "svc"},
    )


def records(*items: dict[str, object]) -> str:
    return json.dumps(list(items))


def agent_trace() -> list[Span]:
    """An agent that reads two items, makes two model calls, and a nested agent with its own."""
    return [
        span("1" * 16, None, "invoke_agent", 0, 100),
        span(
            "2" * 16,
            "1" * 16,
            "search_memory",
            1,
            2,
            **{
                memory.STORE_ID: STORE,
                memory.RECORDS: records(
                    {"id": "tz", "content": "UTC+2", "metadata": {"kind": "fact", "characters": 5}},
                    {"id": "name", "content": "Sam", "score": 0.9},
                ),
            },
        ),
        span("3" * 16, "1" * 16, "chat", 3, 10),
        span("4" * 16, "1" * 16, "execute_tool", 11, 40),
        span("5" * 16, "4" * 16, "invoke_agent", 12, 39),
        span("6" * 16, "5" * 16, "chat", 13, 20),
        span("7" * 16, "1" * 16, "chat", 41, 50),
    ]


def test_records_from_a_search_span() -> None:
    [first, second] = memory.from_span(agent_trace()[1], received_ns=START)
    assert (first.operation, first.store_id, first.record_id) == ("search_memory", STORE, "tz")
    assert json.loads(first.content or "") == "UTC+2"
    assert json.loads(first.metadata or "") == {"kind": "fact", "characters": 5}
    assert (second.record_id, second.score, second.position) == ("name", 0.9, 1)
    assert first.time_ns == START + 2 * MS


def test_structured_records_and_a_bare_record_id() -> None:
    structured = span(
        "1" * 16,
        None,
        "update_memory",
        0,
        1,
        **{memory.RECORDS: [{"id": "tz", "content": {"offset": 2}}]},
    )
    [row] = memory.from_span(structured, received_ns=START)
    assert json.loads(row.content or "") == {"offset": 2}
    bare = span("1" * 16, None, "delete_memory", 0, 1, **{memory.RECORD_ID: "tz"})
    [row] = memory.from_span(bare, received_ns=START)
    assert (row.operation, row.record_id, row.content, row.store_id) == (
        "delete_memory",
        "tz",
        None,
        "",
    )


def test_spans_that_record_nothing() -> None:
    # Not a memory operation, a failed write, a store-wide delete and a write without an ID.
    assert memory.from_span(span("1" * 16, None, "chat", 0, 1), START) == []
    failed = span("1" * 16, None, "create_memory", 0, 1, **{memory.RECORD_ID: "tz"})
    assert memory.from_span(_failed(failed), START) == []
    assert memory.from_span(span("1" * 16, None, "delete_memory", 0, 1), START) == []
    no_id = span(
        "1" * 16, None, "create_memory", 0, 1, **{memory.RECORDS: records({"content": "x"})}
    )
    assert memory.from_span(no_id, START) == []
    # A search result without an ID is still something that was in context.
    search = span(
        "1" * 16, None, "search_memory", 0, 1, **{memory.RECORDS: records({"content": "x"})}
    )
    [row] = memory.from_span(search, START)
    assert row.record_id is None


def _failed(original: Span) -> Span:
    return Span(
        trace_id=original.trace_id,
        span_id=original.span_id,
        parent_span_id=original.parent_span_id,
        name=original.name,
        start_ns=original.start_ns,
        end_ns=original.end_ns,
        status_code=STATUS_ERROR,
        attributes=original.attributes,
        resource=original.resource,
    )


def test_oversized_content_is_cut_and_unreadable_records_ignored() -> None:
    big = span(
        "1" * 16,
        None,
        "create_memory",
        0,
        1,
        **{memory.RECORDS: records({"id": "x", "content": "y" * 200_000})},
    )
    [row] = memory.from_span(big, START)
    assert len((row.content or "").encode()) <= memory.MAX_CONTENT_BYTES
    assert json.loads(row.content or "").endswith("[cut by agentwatch]")
    unreadable = span("1" * 16, None, "create_memory", 0, 1, **{memory.RECORDS: "not json"})
    assert memory.from_span(unreadable, START) == []


def test_in_context_is_per_agent_and_only_after_the_read() -> None:
    store = Store()
    store.insert(agent_trace(), received_ns=START)
    found = memory.in_context(store, [s.span for s in store.trace(T)])
    # Both calls of the outer agent had both items; the nested agent's call had none.
    assert set(found) == {"3" * 16, "7" * 16}
    assert [e["record_id"] for e in found["3" * 16]] == ["tz", "name"]
    assert found["3" * 16][0]["characters"] == 5
    assert found["3" * 16][1]["characters"] == 3


def test_a_read_after_a_call_is_not_in_its_context() -> None:
    late = [
        span("1" * 16, None, "invoke_agent", 0, 100),
        span("3" * 16, "1" * 16, "chat", 1, 10),
        span("2" * 16, "1" * 16, "search_memory", 5, 11, **{memory.RECORD_ID: "tz"}),
        span("7" * 16, "1" * 16, "chat", 12, 20),
    ]
    store = Store()
    store.insert(late, received_ns=START)
    found = memory.in_context(store, [s.span for s in store.trace(T)])
    assert list(found) == ["7" * 16]
    # Without content or a size, the size is unknown rather than zero.
    assert found["7" * 16][0]["characters"] is None


def test_a_tool_search_inside_the_agent_counts_for_later_calls() -> None:
    trace = [
        span("1" * 16, None, "invoke_agent", 0, 100),
        span("4" * 16, "1" * 16, "execute_tool", 1, 10),
        span("2" * 16, "4" * 16, "search_memory", 2, 9, **{memory.RECORD_ID: "tz"}),
        span("7" * 16, "1" * 16, "chat", 12, 20),
    ]
    store = Store()
    store.insert(trace, received_ns=START)
    assert list(memory.in_context(store, [s.span for s in store.trace(T)])) == ["7" * 16]


def history_store() -> Store:
    store = Store()
    store.insert(
        [
            span(
                "8" * 16,
                None,
                "create_memory",
                0,
                1,
                **{
                    memory.STORE_ID: STORE,
                    memory.RECORDS: records({"id": "tz", "content": "UTC+1"}),
                },
            ),
            *agent_trace()[:2],
        ],
        received_ns=START,
    )
    learner = Span(
        trace_id=OTHER,
        span_id="9" * 16,
        parent_span_id=None,
        name="update_memory",
        start_ns=START + 200 * MS,
        end_ns=START + 201 * MS,
        attributes={
            "gen_ai.operation.name": "update_memory",
            memory.STORE_ID: STORE,
            memory.RECORDS: records({"id": "tz", "content": "UTC+3"}),
        },
        resource={"service.name": "learner"},
        links=(SpanLink(trace_id=T, span_id="1" * 16),),
    )
    store.insert([learner], received_ns=START + 1)
    return store


def test_items_and_their_history() -> None:
    store = history_store()
    found = memory.items(store)
    assert [(i["record_id"], i["writes"], i["reads"]) for i in found] == [
        ("tz", 2, 1),
        ("name", 0, 1),
    ]
    assert found[0]["content"] == "UTC+3"
    assert found[0]["services"] == ["learner", "svc"]
    detail = memory.item(store, STORE, "tz")
    assert detail is not None
    assert [(e["operation"], e["content"]) for e in detail["changes"]] == [
        ("update_memory", "UTC+3"),
        ("create_memory", "UTC+1"),
    ]
    assert [(e["operation"], e["content"]) for e in detail["reads"]] == [("search_memory", "UTC+2")]
    assert detail["changes"][0]["links"] == [{"trace_id": T, "span_id": "1" * 16}]
    assert not detail["changes_truncated"]
    assert not detail["reads_truncated"]
    assert memory.item(store, STORE, "missing") is None
    assert memory.item(store, "", "tz") is None


def test_filters_and_paging() -> None:
    store = history_store()
    assert [i["record_id"] for i in memory.items(store, query="utc+3")] == ["tz"]
    assert [i["record_id"] for i in memory.items(store, service="learner")] == ["tz"]
    assert memory.items(store, store_id="other") == []
    first = memory.items(store, limit=1)
    rest = memory.items(store, limit=1, before=memory.ItemCursor.decode(first[0]["cursor"]))
    assert [i["record_id"] for i in first + rest] == ["tz", "name"]
    with pytest.raises(ValueError, match="cursor"):
        memory.ItemCursor.decode("12:not-base64!")


def test_a_deleted_item_stays_deleted_after_a_stale_read() -> None:
    store = Store()
    store.insert(
        [
            span("1" * 16, None, "create_memory", 0, 1, **{memory.RECORD_ID: "tz"}),
            span("2" * 16, None, "delete_memory", 2, 3, **{memory.RECORD_ID: "tz"}),
            span("3" * 16, None, "search_memory", 4, 5, **{memory.RECORD_ID: "tz"}),
        ],
        received_ns=START,
    )
    [item] = memory.items(store)
    assert item["deleted"]


def test_records_outlast_pruned_traces_and_resending_replaces() -> None:
    store = history_store()
    # Re-sending the learner span with different records replaces them.
    store.insert(
        [
            Span(
                trace_id=OTHER,
                span_id="9" * 16,
                parent_span_id=None,
                name="update_memory",
                start_ns=START + 200 * MS,
                end_ns=START + 201 * MS,
                attributes={
                    "gen_ai.operation.name": "update_memory",
                    memory.STORE_ID: STORE,
                    memory.RECORDS: records({"id": "tz", "content": "UTC+4"}),
                },
                resource={"service.name": "learner"},
            )
        ],
        received_ns=START + 2,
    )
    assert memory.items(store)[0]["content"] == "UTC+4"
    assert store.prune(START + 10) == 2
    detail = memory.item(store, STORE, "tz")
    assert detail is not None
    assert (len(detail["changes"]), len(detail["reads"])) == (2, 1)
    assert not any(e["trace_stored"] for e in detail["changes"] + detail["reads"])


def test_an_upgraded_database_reads_memory_spans_it_already_had(tmp_path: Path) -> None:
    path = tmp_path / "db.duckdb"
    store = Store(path)
    store.insert(agent_trace(), received_ns=START)
    with store.transaction() as db:
        db.execute("DELETE FROM memory_records")
        db.execute("DELETE FROM meta WHERE key = 'memory_derivation'")
    store.close()
    reopened = Store(path)
    assert [i["record_id"] for i in memory.items(reopened)] == ["name", "tz"]
    reopened.close()


def test_the_api() -> None:
    store = history_store()
    client = TestClient(server.create_app(store, allowed_hosts=("testserver",)))
    listing = client.get("/api/memory", params={"limit": "1"}).json()
    assert listing["stores"] == [STORE]
    assert [i["record_id"] for i in listing["items"]] == ["tz"]
    following = client.get("/api/memory", params={"limit": "1", "before": listing["next"]}).json()
    assert [i["record_id"] for i in following["items"]] == ["name"]
    assert client.get("/api/memory", params={"before": "x"}).status_code == 400
    detail = client.get("/api/memory/item", params={"store": STORE, "id": "tz"})
    assert detail.status_code == 200
    assert detail.json()["item"]["writes"] == 2
    assert client.get("/api/memory/item", params={"store": STORE}).status_code == 400
    assert client.get("/api/memory/item", params={"store": STORE, "id": "nope"}).status_code == 404
    trace = client.get(f"/api/traces/{T}").json()
    assert trace["in_context"] == {}
    store.insert(agent_trace(), received_ns=START)
    trace = client.get(f"/api/traces/{T}").json()
    assert set(trace["in_context"]) == {"3" * 16, "7" * 16}


def test_memory_spans_arrive_over_otlp() -> None:
    store = Store()
    client = TestClient(server.create_app(store, allowed_hosts=("testserver",)))
    structured = span(
        "1" * 16,
        None,
        "create_memory",
        0,
        1,
        **{
            memory.STORE_ID: STORE,
            memory.RECORDS: [{"id": "tz", "content": "UTC+2", "metadata": {"kind": "fact"}}],
        },
    )
    response = client.post(
        "/v1/traces",
        content=encode([structured]).SerializeToString(),
        headers={"content-type": "application/x-protobuf"},
    )
    assert response.status_code == 200
    [item] = memory.items(store)
    assert (item["record_id"], item["content"], item["metadata"]) == (
        "tz",
        "UTC+2",
        {"kind": "fact"},
    )


def test_migration_from_schema_10(tmp_path: Path) -> None:
    path = tmp_path / "old.duckdb"
    Store(path).close()
    conn = duckdb.connect(str(path))
    conn.execute("DROP TABLE memory_records")
    conn.execute("DROP TABLE memory_deleted")
    conn.execute("DELETE FROM meta WHERE key = 'memory_derivation'")
    conn.execute("UPDATE meta SET value = '10' WHERE key = 'schema'")
    conn.close()
    upgraded = Store(path)
    assert upgraded.query_one("SELECT value FROM meta WHERE key = 'schema'")[0] == "11"
    upgraded.insert(agent_trace(), received_ns=START)
    assert len(memory.items(upgraded)) == 2
    upgraded.close()


def test_a_read_without_content_does_not_hide_captured_content() -> None:
    store = Store()
    store.insert(
        [
            span(
                "1" * 16,
                None,
                "create_memory",
                0,
                1,
                **{memory.RECORDS: records({"id": "tz", "content": "UTC+2"})},
            ),
            span(
                "2" * 16,
                None,
                "search_memory",
                2,
                3,
                **{memory.RECORDS: records({"id": "tz", "content": None})},
            ),
        ],
        received_ns=START,
    )
    [item] = memory.items(store)
    assert item["content"] == "UTC+2"
    detail = memory.item(store, "", "tz")
    assert detail is not None
    assert detail["reads"][0]["content"] is None


@pytest.mark.parametrize("text", ['"' * 70_000, "é" * 70_000, "a" * 70_000])
def test_cut_content_stays_within_the_cap(text: str) -> None:
    capped = span(
        "1" * 16, None, "create_memory", 0, 1, **{memory.RECORDS: [{"id": "x", "content": text}]}
    )
    [row] = memory.from_span(capped, START)
    assert len((row.content or "").encode()) <= memory.MAX_CONTENT_BYTES
    assert json.loads(row.content or "").endswith(memory.CUT)


def test_an_item_read_twice_is_in_a_call_once_and_embeddings_have_no_context() -> None:
    first = records({"id": "tz", "content": "UTC+1"}, {"id": "nm", "content": "Sam"})
    second = records({"id": "tz", "content": "UTC+2"}, {"content": "no id"}, {"content": "no id"})
    trace = [
        span("1" * 16, None, "invoke_agent", 0, 100),
        span("2" * 16, "1" * 16, "search_memory", 1, 2, **{memory.RECORDS: first}),
        span("3" * 16, "1" * 16, "search_memory", 3, 4, **{memory.RECORDS: second}),
        span("4" * 16, "1" * 16, "embeddings", 5, 6),
        span("7" * 16, "1" * 16, "chat", 7, 9),
    ]
    store = Store()
    store.insert(trace, received_ns=START)
    found = memory.in_context(store, [s.span for s in store.trace(T)])
    assert list(found) == ["7" * 16]
    assert [(e["record_id"], e["content"]) for e in found["7" * 16]] == [
        ("nm", "Sam"),
        ("tz", "UTC+2"),
        (None, "no id"),
        (None, "no id"),
    ]


def test_pruning_drops_a_reads_content_but_keeps_the_change() -> None:
    store = history_store()
    store.prune(START + 10)
    detail = memory.item(store, STORE, "tz")
    assert detail is not None
    assert [e["content"] for e in detail["changes"]] == ["UTC+3", "UTC+1"]
    assert [e["content"] for e in detail["reads"]] == [None]
    assert detail["item"]["reads"] == 1


def test_deleting_an_item_removes_everything_recorded_about_it() -> None:
    store = history_store()
    client = TestClient(server.create_app(store, allowed_hosts=("testserver",)))
    assert client.delete("/api/memory/item", params={"store": STORE, "id": "tz"}).status_code == 204
    assert client.delete("/api/memory/item", params={"store": STORE, "id": "tz"}).status_code == 404
    assert [i["record_id"] for i in memory.items(store)] == ["name"]
    assert store.query("SELECT count(*) FROM memory_records WHERE record_id = 'tz'") == [(0,)]


def test_a_rule_change_reads_stored_spans_again_in_chunks(tmp_path: Path) -> None:
    path = tmp_path / "db.duckdb"
    store = Store(path)
    many = [
        span(f"{n:016x}", None, "search_memory", n, n + 1, **{memory.RECORD_ID: f"item-{n % 7}"})
        for n in range(1, 501)
    ]
    store.insert(many, received_ns=START)
    with store.transaction() as db:
        db.execute("DELETE FROM memory_records")
        db.execute("UPDATE meta SET value = 'old' WHERE key = 'memory_derivation'")
    store.close()
    reopened = Store(path)
    assert reopened.query("SELECT count(*) FROM memory_records") == [(500,)]
    assert (
        reopened.query_one("SELECT value FROM meta WHERE key = 'memory_derivation'")[0]
        == memory.derivation()
    )
    reopened.close()


def test_a_deleted_item_stays_gone_from_spans_rebuilds_and_resends(tmp_path: Path) -> None:
    path = tmp_path / "db.duckdb"
    store = Store(path)
    secret = span(
        "1" * 16,
        None,
        "create_memory",
        0,
        1,
        **{
            memory.RECORDS: records(
                {"id": "secret", "content": "my diagnosis"}, {"id": "tz", "content": "UTC+2"}
            )
        },
    )
    store.insert([secret], received_ns=START)
    assert memory.delete_item(store, "", "secret", now_ns=START + 5 * MS)
    assert [i["record_id"] for i in memory.items(store)] == ["tz"]
    # The span no longer holds it, and the other item in it is untouched.
    assert store.query(
        "SELECT count(*) FROM spans WHERE contains(attributes::VARCHAR, 'diagnosis')"
    ) == [(0,)]
    # Re-sending the span, or re-reading stored spans, does not bring it back.
    store.insert([secret], received_ns=START + 1)
    assert store.query(
        "SELECT count(*) FROM spans WHERE contains(attributes::VARCHAR, 'diagnosis')"
    ) == [(0,)]
    with store.transaction() as db:
        db.execute("UPDATE meta SET value = 'old' WHERE key = 'memory_derivation'")
    store.close()
    store = Store(path)
    assert [i["record_id"] for i in memory.items(store)] == ["tz"]
    # A later change to the item is recorded as usual.
    store.insert(
        [
            span(
                "2" * 16,
                None,
                "create_memory",
                10,
                11,
                **{memory.RECORDS: records({"id": "secret", "content": "new"})},
            )
        ],
        received_ns=START + 2,
    )
    assert {i["record_id"]: i["content"] for i in memory.items(store)} == {
        "secret": "new",
        "tz": "UTC+2",
    }
    store.close()


def test_deleting_also_clears_failed_spans_that_named_the_item() -> None:
    store = Store()
    failed = _failed(
        span(
            "1" * 16,
            None,
            "update_memory",
            0,
            1,
            **{memory.RECORDS: records({"id": "secret", "content": "my diagnosis"})},
        )
    )
    store.insert(
        [failed, span("2" * 16, None, "create_memory", 2, 3, **{memory.RECORD_ID: "secret"})],
        received_ns=START,
    )
    assert memory.delete_item(store, "", "secret")
    assert store.query(
        "SELECT count(*) FROM spans WHERE contains(attributes::VARCHAR, 'diagnosis')"
    ) == [(0,)]
    # Only failed spans named it: it is still found and cleared.
    other = Store()
    other.insert([failed], received_ns=START)
    assert memory.delete_item(other, "", "secret")
    assert other.query(
        "SELECT count(*) FROM spans WHERE contains(attributes::VARCHAR, 'diagnosis')"
    ) == [(0,)]
    assert not memory.delete_item(other, "", "never-there")
