from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from starlette.testclient import TestClient

from agentwatch import client as aw_client
from agentwatch import experiments, server
from agentwatch.store import Store


def http_client(db: Store) -> TestClient:
    return TestClient(server.create_app(db, allowed_hosts=("testserver",)))


def setup(http: TestClient) -> tuple[str, list[str]]:
    dataset_id = http.post("/api/datasets", json={"name": "math"}).json()["dataset"]["id"]
    items = http.post(
        f"/api/datasets/{dataset_id}/items",
        json={
            "items": [
                {"id": "a", "input": "2+2", "expected_output": "4"},
                {"id": "b", "input": "3+3", "expected_output": "6"},
            ]
        },
    ).json()["items"]
    return dataset_id, [i["id"] for i in items]


def test_runs_record_outputs_traces_and_scores() -> None:
    db = Store()
    http = http_client(db)
    dataset_id, _ = setup(http)
    run = http.post(
        f"/api/datasets/{dataset_id}/runs", json={"name": "v1", "metadata": {"model": "m"}}
    )
    assert run.status_code == 201
    run_id = run.json()["run"]["id"]
    assert http.post(f"/api/datasets/{dataset_id}/runs", json={"name": "v1"}).status_code == 409
    added = http.post(
        f"/api/runs/{run_id}/items",
        json={
            "items": [
                {
                    "item_id": "a",
                    "output": "4",
                    "start_ns": 1000,
                    "end_ns": 3_001_000,
                    "scores": [{"name": "exact", "value": True}],
                },
                {
                    "item_id": "b",
                    "error": "TimeoutError: slow",
                    "start_ns": 2000,
                    "end_ns": 1_002_000,
                },
            ]
        },
    )
    assert added.status_code == 201
    traces = {i["item_id"]: i["trace_id"] for i in added.json()["items"]}
    # Each untraced item got a one-span trace holding its input and output.
    [span] = db.trace(traces["a"])
    assert span.span.attributes["input.value"] == '"2+2"'
    assert span.span.service_name == experiments.SERVICE
    [failed] = db.trace(traces["b"])
    assert failed.span.status_code == 2
    detail = http.get(f"/api/runs/{run_id}").json()
    by_item = {i["item_id"]: i for i in detail["items"]}
    assert by_item["a"]["expected_output"] == "4"
    assert by_item["a"]["latency_ms"] == pytest.approx(3.0)
    assert by_item["a"]["scores"][0]["name"] == "exact"
    assert by_item["b"]["error"].startswith("TimeoutError")
    [summary] = http.get(f"/api/datasets/{dataset_id}/runs").json()["runs"]
    assert (summary["items"], summary["errors"]) == (2, 1)
    assert summary["mean_latency_ms"] == pytest.approx(2.0)
    assert summary["scores"] == [{"name": "exact", "count": 1, "mean": 1.0}]
    assert summary["run"]["metadata"] == {"model": "m"}


def test_rerunning_an_item_replaces_it_and_traced_items_get_no_synthetic_span() -> None:
    db = Store()
    http = http_client(db)
    dataset_id, _ = setup(http)
    run_id = http.post(f"/api/datasets/{dataset_id}/runs", json={"name": "v1"}).json()["run"]["id"]
    http.post(f"/api/runs/{run_id}/items", json={"item_id": "a", "output": "5"})
    traced = "f" * 32
    http.post(f"/api/runs/{run_id}/items", json={"item_id": "a", "output": "4", "trace_id": traced})
    [item] = http.get(f"/api/runs/{run_id}").json()["items"]
    assert (item["output"], item["trace_id"]) == ("4", traced)
    assert db.trace(traced) == []  # the caller's own spans will arrive by OTLP


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"item_id": "zzz", "output": 1}, 404),
        ({"output": 1}, 400),
        ({"item_id": "a", "trace_id": "nope"}, 400),
        ({"item_id": "a", "start_ns": 10, "end_ns": 5}, 400),
        ({"item_id": "a", "scores": [{"name": "x"}]}, 400),
        ({"item_id": "a", "scores": "x"}, 400),
        ({"items": [{"item_id": "a"}, {"item_id": "a"}]}, 409),
    ],
)
def test_invalid_run_items(body: dict[str, object], status: int) -> None:
    http = http_client(Store())
    dataset_id, _ = setup(http)
    run_id = http.post(f"/api/datasets/{dataset_id}/runs", json={"name": "v1"}).json()["run"]["id"]
    assert http.post(f"/api/runs/{run_id}/items", json=body).status_code == status
    assert http.get(f"/api/runs/{run_id}").json()["items"] == []


def test_items_from_another_dataset_are_refused_and_runs_delete() -> None:
    http = http_client(Store())
    dataset_id, _ = setup(http)
    other = http.post("/api/datasets", json={"name": "other"}).json()["dataset"]["id"]
    http.post(f"/api/datasets/{other}/items", json={"id": "z", "input": 1})
    run_id = http.post(f"/api/datasets/{dataset_id}/runs", json={"name": "v1"}).json()["run"]["id"]
    assert http.post(f"/api/runs/{run_id}/items", json={"item_id": "z"}).status_code == 404
    assert http.post("/api/datasets/nope/runs", json={"name": "x"}).status_code == 404
    assert http.delete(f"/api/runs/{run_id}").status_code == 204
    assert http.get(f"/api/runs/{run_id}").status_code == 404


@pytest.fixture
def live_server() -> Iterator[str]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = uvicorn.Config(
        server.create_app(Store()), host="127.0.0.1", port=port, log_level="warning"
    )
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not instance.started and time.time() < deadline:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    instance.should_exit = True
    thread.join(timeout=5)


def test_run_experiment_end_to_end(live_server: str) -> None:
    client = aw_client.Client(live_server)
    dataset_id = client.request("POST", "/api/datasets", {"name": "math"})["dataset"]["id"]
    client.request(
        "POST",
        f"/api/datasets/{dataset_id}/items",
        {
            "items": [
                {"input": "2+2", "expected_output": "4"},
                {"input": "3+3", "expected_output": "6"},
                {"input": "boom"},
            ]
        },
    )

    def task(question: str) -> str:
        if question == "boom":
            raise ValueError("cannot parse")
        return str(eval(question))

    def exact(_input: object, output: object, expected: object) -> bool:
        return output == expected

    def length(_input: object, output: object, _expected: object) -> dict[str, object]:
        return {"name": "length", "value": len(str(output)), "comment": "characters"}

    def broken(_input: object, _output: object, _expected: object) -> object:
        raise RuntimeError("evaluator bug")

    result = aw_client.run_experiment(
        dataset="math",
        name="eval v1",
        task=task,
        evaluators=[exact, length, broken],
        base_url=live_server,
        concurrency=2,
    )
    assert (result.items, result.errors) == (3, 1)
    assert result.scores == {"exact": 1.0, "length": 1.0}
    assert len(result.evaluator_errors) == 2
    detail = client.request("GET", f"/api/runs/{result.run_id}")
    assert sorted(i["output"] or "" for i in detail["items"]) == ["", "4", "6"]
    with pytest.raises(aw_client.AgentwatchError, match="409"):
        aw_client.run_experiment(dataset="math", name="eval v1", task=task, base_url=live_server)
    with pytest.raises(aw_client.AgentwatchError, match="no dataset"):
        aw_client.run_experiment(dataset="missing", name="x", task=task, base_url=live_server)
