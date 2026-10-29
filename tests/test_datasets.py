from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from agentwatch import datasets, genai, server
from agentwatch.spans import Span
from agentwatch.store import Store

T = "a" * 32


def client(db: Store) -> TestClient:
    return TestClient(server.create_app(db, allowed_hosts=("testserver",)))


def make(http: TestClient, name: str = "regressions") -> str:
    response = http.post(
        "/api/datasets",
        json={"name": name, "description": "bad answers", "metadata": {"owner": "me"}},
    )
    assert response.status_code == 201
    return response.json()["dataset"]["id"]


def test_create_list_and_delete_datasets() -> None:
    http = client(Store())
    dataset_id = make(http)
    assert http.post("/api/datasets", json={"name": "regressions"}).status_code == 409
    assert http.post("/api/datasets", json={"name": "  "}).status_code == 400
    listing = http.get("/api/datasets").json()["datasets"]
    assert [(d["name"], d["items"], d["metadata"]) for d in listing] == [
        ("regressions", 0, {"owner": "me"})
    ]
    assert http.get(f"/api/datasets/{dataset_id}").json()["dataset"]["description"] == "bad answers"
    assert http.delete(f"/api/datasets/{dataset_id}").status_code == 204
    assert http.get(f"/api/datasets/{dataset_id}").status_code == 404
    assert http.delete(f"/api/datasets/{dataset_id}").status_code == 404


def test_items_add_replace_archive() -> None:
    db = Store()
    http = client(db)
    dataset_id = make(http)
    added = http.post(
        f"/api/datasets/{dataset_id}/items",
        json={
            "items": [
                {"id": "q1", "input": {"question": "2+2"}, "expected_output": "4"},
                {"input": "hello"},
            ]
        },
    )
    assert added.status_code == 201
    assert len(added.json()["items"]) == 2
    # Same ID in the same dataset replaces.
    assert (
        http.post(
            f"/api/datasets/{dataset_id}/items",
            json={"id": "q1", "input": {"question": "3+3"}, "expected_output": "6"},
        ).status_code
        == 201
    )
    items = http.get(f"/api/datasets/{dataset_id}/items").json()["items"]
    assert [i["input"] for i in items] == [{"question": "3+3"}, "hello"]
    # An ID owned by another dataset is refused, and the batch stores nothing.
    other = make(http, "other")
    batch = {"items": [{"input": "x"}, {"id": "q1", "input": "y"}]}
    assert http.post(f"/api/datasets/{other}/items", json=batch).status_code == 409
    assert http.get(f"/api/datasets/{other}/items").json()["items"] == []
    assert http.patch("/api/dataset-items/q1", json={"status": "ARCHIVED"}).status_code == 204
    assert [i["id"] for i in http.get(f"/api/datasets/{dataset_id}/items").json()["items"]] != [
        "q1"
    ]
    archived = http.get(f"/api/datasets/{dataset_id}/items", params={"archived": "1"}).json()[
        "items"
    ]
    assert {i["status"] for i in archived} == {"ACTIVE", "ARCHIVED"}
    assert http.get("/api/datasets").json()["datasets"][1]["archived_items"] == 1
    assert http.patch("/api/dataset-items/q1", json={"status": "GONE"}).status_code == 400
    assert http.patch("/api/dataset-items/nope", json={"status": "ACTIVE"}).status_code == 404


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"expected_output": "x"}, 400),
        ({"input": "x", "id": "a/b"}, 400),
        ({"input": "x", "metadata": []}, 400),
        ({"input": "x", "source_trace_id": "xyz"}, 400),
        ({"input": "x" * (600 * 1024)}, 400),
        ({"items": []}, 400),
        ({"items": [{"input": 1}, {"input": 2, "id": "d"}, {"input": 3, "id": "d"}]}, 409),
    ],
)
def test_invalid_items(body: dict[str, object], status: int) -> None:
    http = client(Store())
    dataset_id = make(http)
    assert http.post(f"/api/datasets/{dataset_id}/items", json=body).status_code == status
    assert http.post("/api/datasets/nope/items", json={"input": 1}).status_code == 404


def trace(db: Store) -> None:
    messages_in = json.dumps(
        [{"role": "user", "parts": [{"type": "text", "content": "Plan my day"}]}]
    )
    messages_out = json.dumps(
        [{"role": "assistant", "parts": [{"type": "text", "content": "Here is a plan"}]}]
    )
    db.insert(
        [
            Span(
                trace_id=T,
                span_id="1" * 16,
                parent_span_id=None,
                name="agent",
                start_ns=1,
                end_ns=9,
                attributes={
                    genai.OPERATION: "invoke_agent",
                    "gen_ai.input.messages": messages_in,
                    "gen_ai.output.messages": messages_out,
                },
            ),
            Span(
                trace_id=T,
                span_id="2" * 16,
                parent_span_id="1" * 16,
                name="tool",
                start_ns=2,
                end_ns=3,
                attributes={
                    genai.OPERATION: "execute_tool",
                    "input.value": "not json {",
                    "output.value": '{"ok": true}',
                },
            ),
        ],
        received_ns=1,
    )


def test_item_from_a_trace() -> None:
    db = Store()
    trace(db)
    http = client(db)
    dataset_id = make(http)
    root = http.post(f"/api/datasets/{dataset_id}/items/from-trace", json={"trace_id": T}).json()[
        "item"
    ]
    assert root["input"][0]["parts"][0]["content"] == "Plan my day"
    assert root["expected_output"][0]["role"] == "assistant"
    assert (root["source_trace_id"], root["source_span_id"]) == (T, "1" * 16)
    tool = http.post(
        f"/api/datasets/{dataset_id}/items/from-trace", json={"trace_id": T, "span_id": "2" * 16}
    ).json()["item"]
    assert (tool["input"], tool["expected_output"]) == ("not json {", {"ok": True})
    assert (
        http.post(
            f"/api/datasets/{dataset_id}/items/from-trace", json={"trace_id": "b" * 32}
        ).status_code
        == 404
    )
    assert (
        http.post(
            f"/api/datasets/{dataset_id}/items/from-trace",
            json={"trace_id": T, "span_id": "3" * 16},
        ).status_code
        == 404
    )
    assert (
        http.post(
            f"/api/datasets/{dataset_id}/items/from-trace", json={"trace_id": "x"}
        ).status_code
        == 400
    )


def test_writes_need_json() -> None:
    http = client(Store())
    assert (
        http.post(
            "/api/datasets", content=b'{"name": "x"}', headers={"content-type": "text/plain"}
        ).status_code
        == 415
    )
    assert (
        http.post(
            "/api/datasets", content=b"{", headers={"content-type": "application/json"}
        ).status_code
        == 400
    )


def test_module_errors_are_typed() -> None:
    db = Store()
    with pytest.raises(datasets.NotFound):
        datasets.get_dataset(db, "missing")
    with pytest.raises(datasets.DatasetError):
        datasets.create_dataset(db, [])
