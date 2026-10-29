from __future__ import annotations

import json

import pytest
from google.protobuf import json_format
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord
from starlette.testclient import TestClient

from agentwatch import genai, metrics, scores, server
from agentwatch.spans import Attributes, Span, SpanEvent
from agentwatch.store import Store
from tests.builders import key_values

T = "c" * 32
S = "1" * 16


def root(events: tuple[SpanEvent, ...] = (), trace: str = T, start: int = 1_000) -> Span:
    return Span(
        trace_id=trace,
        span_id=S,
        parent_span_id=None,
        name="agent",
        start_ns=start,
        end_ns=start + 100,
        attributes={genai.OPERATION: "invoke_agent"},
        resource={"service.name": "svc"},
        events=events,
    )


@pytest.mark.parametrize(
    ("body", "data_type", "value", "label"),
    [
        ({"trace_id": T, "name": "ok", "value": True}, scores.BOOLEAN, 1.0, None),
        ({"trace_id": T, "name": "q", "value": 0.75}, scores.NUMERIC, 0.75, None),
        ({"trace_id": T, "name": "q", "value": 3, "label": "three"}, scores.NUMERIC, 3.0, "three"),
        ({"trace_id": T, "name": "topic", "label": "travel"}, scores.CATEGORICAL, None, "travel"),
        (
            {"trace_id": T, "name": "ok", "value": 0, "data_type": "BOOLEAN"},
            scores.BOOLEAN,
            0.0,
            None,
        ),
    ],
)
def test_api_scores(
    body: dict[str, object], data_type: str, value: float | None, label: str | None
) -> None:
    score = scores.from_api(body, now_ns=5)
    assert (score.data_type, score.value, score.label, score.source, score.created_ns) == (
        data_type,
        value,
        label,
        scores.API,
        5,
    )
    assert len(score.id) == 32


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ([], "JSON object"),
        ({"name": "x", "value": 1}, "trace_id"),
        ({"trace_id": "0" * 32, "name": "x", "value": 1}, "trace_id"),
        ({"trace_id": T, "span_id": "zz", "name": "x", "value": 1}, "span_id"),
        ({"trace_id": T, "value": 1}, "name is required"),
        ({"trace_id": T, "name": "  ", "value": 1}, "non-empty"),
        ({"trace_id": T, "name": "x"}, "value or a label"),
        ({"trace_id": T, "name": "x", "value": "5"}, "number or a boolean"),
        ({"trace_id": T, "name": "x", "value": float("nan")}, "finite"),
        ({"trace_id": T, "name": "x", "value": 2, "data_type": "BOOLEAN"}, "does not match"),
        ({"trace_id": T, "name": "x", "value": 2, "data_type": "TEXT"}, "data_type must be"),
        ({"trace_id": T, "name": "x" * 201, "value": 1}, "longer than"),
    ],
)
def test_invalid_api_scores(body: object, message: str) -> None:
    with pytest.raises(scores.ScoreError, match=message):
        scores.from_api(body)


def test_evaluation_events() -> None:
    attributes: Attributes = {
        scores.EVALUATION_NAME: "relevance",
        scores.EVALUATION_VALUE: 0.5,
        scores.EVALUATION_EXPLANATION: "why",
    }
    first = scores.from_evaluation(T, S, 10, attributes)
    again = scores.from_evaluation(T, S, 10, attributes)
    assert first is not None
    assert again is not None
    assert first.id == again.id  # the same event sent twice keeps one score
    assert (first.data_type, first.value, first.comment, first.source) == (
        scores.NUMERIC,
        0.5,
        "why",
        scores.EVAL,
    )
    flag = scores.from_evaluation(
        T, None, 10, {scores.EVALUATION_NAME: "pass", scores.EVALUATION_VALUE: False}
    )
    assert flag is not None
    assert (flag.data_type, flag.value) == (scores.BOOLEAN, 0.0)
    assert scores.from_evaluation(T, S, 10, {scores.EVALUATION_VALUE: 1}) is None
    assert scores.from_evaluation(T, S, 10, {scores.EVALUATION_NAME: "x"}) is None


def test_store_scores_and_trace_summaries() -> None:
    db = Store()
    event = SpanEvent(
        name=scores.EVALUATION_EVENT,
        time_ns=7,
        attributes={scores.EVALUATION_NAME: "helpful", scores.EVALUATION_VALUE: 0.8},
    )
    db.insert([root((event,))], received_ns=1)
    db.upsert_scores(
        [
            scores.from_api({"trace_id": T, "name": "helpful", "value": 0.4}),
            scores.from_api({"id": "fixed", "trace_id": T, "name": "topic", "label": "a"}),
        ]
    )
    db.upsert_scores(
        [scores.from_api({"id": "fixed", "trace_id": T, "name": "topic", "label": "b"})]
    )
    stored = db.scores(T)
    assert sorted((s.name, s.value, s.label) for s in stored) == [
        ("helpful", 0.4, None),
        ("helpful", 0.8, None),
        ("topic", None, "b"),
    ]
    [summary] = db.traces()
    by_name = {s.name: s for s in summary.scores}
    assert by_name["helpful"].count == 2
    assert by_name["helpful"].mean == pytest.approx(0.6)
    assert by_name["topic"].label == "b"
    assert db.delete_score("fixed")
    assert not db.delete_score("fixed")
    # Re-sending the span keeps one copy of its event score.
    db.insert([root((event,))], received_ns=2)
    assert len([s for s in db.scores(T) if s.source == scores.EVAL]) == 1


def test_scores_may_arrive_before_their_trace() -> None:
    db = Store()
    db.upsert_scores([scores.from_api({"trace_id": T, "name": "early", "value": 1})])
    assert db.traces() == []
    db.insert([root()], received_ns=1)
    assert db.traces()[0].scores[0].name == "early"


def client(db: Store) -> TestClient:
    return TestClient(server.create_app(db, allowed_hosts=("testserver",)))


def test_score_api() -> None:
    db = Store()
    db.insert([root()], received_ns=1)
    http = client(db)
    one = http.post("/api/scores", json={"trace_id": T, "span_id": S, "name": "ok", "value": True})
    assert one.status_code == 201
    batch = http.post(
        "/api/scores?source=annotation",
        json={
            "scores": [
                {"trace_id": T, "name": "n", "value": 2},
                {"trace_id": T, "name": "t", "label": "x"},
            ]
        },
    )
    assert batch.status_code == 201
    assert len(batch.json()["ids"]) == 2
    detail = http.get(f"/api/traces/{T}").json()
    assert {s["source"] for s in detail["scores"]} == {"API", "ANNOTATION"}
    assert detail["scores"][0]["created_ns"].isdigit()
    # A bad item fails the whole batch.
    bad = http.post(
        "/api/scores", json={"scores": [{"trace_id": T, "name": "n", "value": 2}, {"trace_id": T}]}
    )
    assert bad.status_code == 400
    assert len(db.scores(T)) == 3
    assert (
        http.post(
            "/api/scores", content=b"{", headers={"content-type": "application/json"}
        ).status_code
        == 400
    )
    assert http.post("/api/scores", json={"scores": []}).status_code == 400
    removed = http.delete(f"/api/scores/{one.json()['ids'][0]}")
    assert removed.status_code == 204
    assert http.delete("/api/scores/nope").status_code == 404
    listing = http.get("/api/traces").json()["traces"][0]
    assert {s["name"] for s in listing["scores"]} == {"n", "t"}


def logs_request() -> ExportLogsServiceRequest:
    request = ExportLogsServiceRequest()
    records = request.resource_logs.add().scope_logs.add().log_records
    evaluation = LogRecord(
        event_name=scores.EVALUATION_EVENT,
        time_unix_nano=99,
        trace_id=bytes.fromhex(T),
        span_id=bytes.fromhex(S),
    )
    evaluation.attributes.extend(
        key_values({scores.EVALUATION_NAME: "accuracy", scores.EVALUATION_LABEL: "correct"})
    )
    records.append(evaluation)
    # The older form: event name as an attribute.
    legacy = LogRecord(observed_time_unix_nano=100, trace_id=bytes.fromhex(T))
    legacy.attributes.extend(
        key_values(
            {
                "event.name": scores.EVALUATION_EVENT,
                scores.EVALUATION_NAME: "accuracy",
                scores.EVALUATION_VALUE: 1.0,
            }
        )
    )
    records.append(legacy)
    records.append(LogRecord(event_name="something.else"))
    orphan = LogRecord(event_name=scores.EVALUATION_EVENT)
    orphan.attributes.extend(
        key_values({scores.EVALUATION_NAME: "accuracy", scores.EVALUATION_VALUE: 1.0})
    )
    records.append(orphan)
    return request


def test_otlp_logs_protobuf() -> None:
    db = Store()
    response = client(db).post(
        "/v1/logs",
        content=logs_request().SerializeToString(),
        headers={"content-type": "application/x-protobuf"},
    )
    assert response.status_code == 200
    stored = db.scores(T)
    assert sorted((s.label or "", s.value or 0.0, s.span_id or "") for s in stored) == [
        ("", 1.0, ""),
        ("correct", 0.0, S),
    ]
    assert all(s.source == scores.EVAL for s in stored)


def test_otlp_logs_json_reports_rejections() -> None:
    db = Store()
    document = json_format.MessageToDict(logs_request())
    for record in document["resourceLogs"][0]["scopeLogs"][0]["logRecords"]:
        for key in ("traceId", "spanId"):
            if key in record:
                import base64

                record[key] = base64.b64decode(record[key]).hex()
    response = client(db).post(
        "/v1/logs", content=json.dumps(document), headers={"content-type": "application/json"}
    )
    assert response.status_code == 200
    assert response.json()["partialSuccess"]["rejectedLogRecords"] == "1"
    assert len(db.scores(T)) == 2
    assert (
        client(db)
        .post("/v1/logs", content=b"x", headers={"content-type": "text/plain"})
        .status_code
        == 415
    )


def test_score_metrics() -> None:
    db = Store()
    db.insert([root()], received_ns=1)
    db.upsert_scores(
        [
            scores.from_api({"trace_id": T, "name": "q", "value": 1}, now_ns=2_000),
            scores.from_api({"trace_id": T, "name": "q", "value": 0}, now_ns=2_000),
            scores.from_api({"trace_id": T, "name": "topic", "label": "a"}, now_ns=2_000),
            scores.from_api({"trace_id": T, "name": "late", "value": 1}, now_ns=10**12),
        ]
    )
    rows = {r["name"]: r for r in metrics.metrics(db, metrics.Window(0, 10**9))["scores"]}
    assert set(rows) == {"q", "topic"}
    assert (rows["q"]["count"], rows["q"]["mean"], rows["q"]["min"], rows["q"]["max"]) == (
        2,
        0.5,
        0.0,
        1.0,
    )
    assert rows["topic"]["labels"] == {"a": 1}
    assert rows["q"]["sources"] == {"API": 2}


def test_an_id_cannot_move_a_score_to_another_trace() -> None:
    other = "d" * 32
    db = Store()
    http = client(db)
    assert (
        http.post(
            "/api/scores", json={"id": "k1", "trace_id": T, "name": "n", "value": 1}
        ).status_code
        == 201
    )
    moved = http.post("/api/scores", json={"id": "k1", "trace_id": other, "name": "n", "value": 2})
    assert moved.status_code == 409
    assert [s.value for s in db.scores(T)] == [1.0]
    assert db.scores(other) == []
    # The same ID on the same trace still replaces.
    assert (
        http.post(
            "/api/scores", json={"id": "k1", "trace_id": T, "name": "n", "value": 3}
        ).status_code
        == 201
    )
    assert [s.value for s in db.scores(T)] == [3.0]
    # Two traces for one ID within a batch fail the whole batch.
    batch = {
        "scores": [
            {"id": "k2", "trace_id": T, "name": "n", "value": 1},
            {"id": "k2", "trace_id": other, "name": "n", "value": 1},
        ]
    }
    assert http.post("/api/scores", json=batch).status_code == 409
    assert len(db.scores(T)) == 1


@pytest.mark.parametrize("bad_id", ["a/b", "..", ".", "x y", "", "ü", "a" * 201])
def test_score_ids_must_be_url_safe(bad_id: str) -> None:
    with pytest.raises(scores.ScoreError, match="id must be"):
        scores.from_api({"id": bad_id, "trace_id": T, "name": "n", "value": 1})


def test_every_accepted_id_can_be_deleted() -> None:
    db = Store()
    http = client(db)
    for score_id in ("a.b", "x:1", "A_-9", "..."):
        assert (
            http.post(
                "/api/scores", json={"id": score_id, "trace_id": T, "name": "n", "value": 1}
            ).status_code
            == 201
        )
        assert http.delete(f"/api/scores/{score_id}").status_code == 204


def test_huge_and_blank_values() -> None:
    with pytest.raises(scores.ScoreError, match="too large"):
        scores.from_api({"trace_id": T, "name": "n", "value": 10**400})
    with pytest.raises(scores.ScoreError, match="value or a label"):
        scores.from_api({"trace_id": T, "name": "n", "label": "   "})
    assert scores.from_api({"trace_id": T, "name": " n ", "label": " a "}).label == "a"


def test_scores_need_json_and_a_same_origin_browser() -> None:
    db = Store()
    http = TestClient(server.create_app(db), base_url="http://127.0.0.1:4318")
    body = json.dumps({"trace_id": T, "name": "n", "value": 1})
    form = http.post("/api/scores", content=body, headers={"content-type": "text/plain"})
    assert form.status_code == 415
    for origin, expected in [
        ("http://127.0.0.1:4318", 201),
        ("http://localhost:4318", 201),
        ("http://localhost:9999", 403),
        ("https://127.0.0.1:4318", 201),
        ("http://evil.example:4318", 403),
    ]:
        response = http.post(
            "/api/scores",
            content=body,
            headers={"content-type": "application/json", "origin": origin},
        )
        assert response.status_code == expected, origin
    assert server.host_port("[::1]:4318") == 4318
    assert server.host_port("localhost") == 80
    assert server.host_port("localhost", "https") == 443


def test_post_returns_stored_scores() -> None:
    http = client(Store())
    body = http.post("/api/scores", json={"trace_id": T, "name": "n", "value": 1}).json()
    assert body["scores"][0]["id"] == body["ids"][0]
    assert body["scores"][0]["created_ns"].isdigit()


def test_label_ties_break_alphabetically() -> None:
    db = Store()
    db.insert([root()], received_ns=1)
    db.upsert_scores(
        [
            scores.from_api({"trace_id": T, "name": "t", "label": label})
            for label in ("y", "x", "y", "x")
        ]
    )
    assert db.traces()[0].scores[0].label == "x"


def test_zero_span_id_in_logs_means_unset() -> None:
    request = ExportLogsServiceRequest()
    record = LogRecord(
        event_name=scores.EVALUATION_EVENT,
        time_unix_nano=5,
        trace_id=bytes.fromhex(T),
        span_id=bytes(8),
    )
    record.attributes.extend(
        key_values({scores.EVALUATION_NAME: "a", scores.EVALUATION_VALUE: 1.0})
    )
    request.resource_logs.add().scope_logs.add().log_records.append(record)
    db = Store()
    client(db).post(
        "/v1/logs",
        content=request.SerializeToString(),
        headers={"content-type": "application/x-protobuf"},
    )
    [score] = db.scores(T)
    assert score.span_id is None


def test_logs_reject_an_event_whose_score_id_is_taken() -> None:
    request = ExportLogsServiceRequest()
    record = LogRecord(
        event_name=scores.EVALUATION_EVENT, time_unix_nano=5, trace_id=bytes.fromhex(T)
    )
    record.attributes.extend(
        key_values({scores.EVALUATION_NAME: "a", scores.EVALUATION_VALUE: 1.0})
    )
    request.resource_logs.add().scope_logs.add().log_records.append(record)
    taken = scores.from_evaluation(
        T, None, 5, {scores.EVALUATION_NAME: "a", scores.EVALUATION_VALUE: 1.0}
    )
    assert taken is not None
    db = Store()
    other = "d" * 32
    db.upsert_scores(
        [scores.from_api({"id": taken.id, "trace_id": other, "name": "x", "value": 0})]
    )
    response = client(db).post(
        "/v1/logs",
        content=request.SerializeToString(),
        headers={"content-type": "application/x-protobuf"},
    )
    assert response.status_code == 200
    reply = ExportLogsServiceResponse()
    reply.ParseFromString(response.content)
    assert reply.partial_success.rejected_log_records == 1
    assert db.scores(T) == []
