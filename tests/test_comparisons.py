from __future__ import annotations

from starlette.testclient import TestClient

from agentwatch import server, stats
from agentwatch.store import Store


def setup(n: int = 40) -> tuple[TestClient, str, str]:
    http = TestClient(server.create_app(Store(), allowed_hosts=("testserver",)))
    dataset_id = http.post("/api/datasets", json={"name": "d"}).json()["dataset"]["id"]
    http.post(
        f"/api/datasets/{dataset_id}/items",
        json={"items": [{"id": f"i{k}", "input": k} for k in range(n)]},
    )
    a = http.post(f"/api/datasets/{dataset_id}/runs", json={"name": "a"}).json()["run"]["id"]
    b = http.post(f"/api/datasets/{dataset_id}/runs", json={"name": "b"}).json()["run"]["id"]
    return http, a, b


def record(http: TestClient, run_id: str, items: list[dict[str, object]]) -> None:
    assert http.post(f"/api/runs/{run_id}/items", json={"items": items}).status_code == 201


def test_a_clear_improvement_is_better_and_flips_are_listed() -> None:
    http, a, b = setup()
    record(
        http,
        a,
        [
            {
                "item_id": f"i{k}",
                "scores": [{"name": "exact", "value": k < 20}, {"name": "quality", "value": 0.5}],
            }
            for k in range(40)
        ],
    )
    record(
        http,
        b,
        [
            {
                "item_id": f"i{k}",
                "scores": [{"name": "exact", "value": k < 38}, {"name": "quality", "value": 0.9}],
            }
            for k in range(40)
        ],
    )
    result = http.get("/api/compare", params={"baseline": a, "candidate": b}).json()
    assert result["pairs"] == 40
    by_name = {c["name"]: c for c in result["comparisons"]}
    exact = by_name["exact"]
    assert (exact["kind"], exact["only_candidate"], exact["only_baseline"]) == ("binary", 18, 0)
    assert exact["verdict"] == stats.BETTER
    assert exact["baseline_rate"] == 0.5
    assert by_name["quality"]["kind"] == "numeric"
    assert by_name["quality"]["verdict"] == stats.BETTER
    assert by_name["task succeeded"]["verdict"] == stats.NO_DIFFERENCE
    flipped = [i for i in result["items"] if i["flips"].get("exact")]
    assert len(flipped) == 18
    assert {i["flips"]["exact"] for i in flipped} == {"improved"}
    assert result["latency"]["unit"] == "ms"


def test_a_crash_counts_as_failing_binary_scores() -> None:
    http, a, b = setup(12)
    record(
        http,
        a,
        [{"item_id": f"i{k}", "scores": [{"name": "exact", "value": True}]} for k in range(12)],
    )
    record(http, b, [{"item_id": f"i{k}", "error": "Boom: x"} for k in range(12)])
    result = http.get("/api/compare", params={"baseline": a, "candidate": b}).json()
    by_name = {c["name"]: c for c in result["comparisons"]}
    assert by_name["exact"]["only_baseline"] == 12
    assert by_name["exact"]["verdict"] == stats.WORSE
    assert by_name["task succeeded"]["verdict"] == stats.WORSE


def test_only_shared_items_are_paired_and_small_sets_are_underpowered() -> None:
    http, a, b = setup(6)
    record(
        http,
        a,
        [
            {"item_id": f"i{k}", "scores": [{"name": "exact", "value": k % 2 == 0}]}
            for k in range(6)
        ],
    )
    record(
        http,
        b,
        [
            {"item_id": f"i{k}", "scores": [{"name": "exact", "value": k % 3 == 0}]}
            for k in range(4)
        ],
    )
    result = http.get("/api/compare", params={"baseline": a, "candidate": b}).json()
    assert (result["pairs"], result["only_baseline"], result["only_candidate"]) == (4, 2, 0)
    assert {c["verdict"] for c in result["comparisons"]} == {stats.UNDERPOWERED}


def test_compare_errors() -> None:
    http, a, b = setup(2)
    other = http.post("/api/datasets", json={"name": "other"}).json()["dataset"]["id"]
    c = http.post(f"/api/datasets/{other}/runs", json={"name": "c"}).json()["run"]["id"]
    assert http.get("/api/compare", params={"baseline": a, "candidate": c}).status_code == 400
    assert http.get("/api/compare", params={"baseline": a, "candidate": a}).status_code == 400
    assert http.get("/api/compare", params={"baseline": a}).status_code == 400
    assert http.get("/api/compare", params={"baseline": a, "candidate": "nope"}).status_code == 404
    assert (
        http.get("/api/compare", params={"baseline": a, "candidate": b, "margin": "2"}).status_code
        == 400
    )
    assert (
        http.get("/api/compare", params={"baseline": a, "candidate": b, "margin": "x"}).status_code
        == 400
    )
    empty = http.get("/api/compare", params={"baseline": a, "candidate": b}).json()
    assert empty["pairs"] == 0
    assert empty["comparisons"][0]["baseline_rate"] is None


def test_small_numeric_changes_are_not_flips() -> None:
    http, a, b = setup(12)
    record(
        http, a, [{"item_id": f"i{k}", "scores": [{"name": "q", "value": 0.8}]} for k in range(12)]
    )
    record(
        http,
        b,
        [
            {"item_id": f"i{k}", "scores": [{"name": "q", "value": 0.81 if k else 0.2}]}
            for k in range(12)
        ],
    )
    result = http.get("/api/compare", params={"baseline": a, "candidate": b}).json()
    changed = [i["item_id"] for i in result["items"] if i["flips"]]
    assert changed == ["i0"]
