from __future__ import annotations

import gzip
import json
from pathlib import Path

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceResponse,
)
from starlette.testclient import TestClient

from agentwatch import server
from agentwatch.spans import Span
from agentwatch.store import Store
from tests.builders import encode, to_otlp_json

TRACE = "c" * 32


def spans() -> list[Span]:
    return [
        Span(
            trace_id=TRACE,
            span_id="1" * 16,
            parent_span_id=None,
            name="invoke_agent a",
            start_ns=1_761_000_000_123_456_789,
            end_ns=1_761_000_001_000_000_000,
            attributes={"gen_ai.operation.name": "invoke_agent"},
            resource={"service.name": "svc"},
        ),
        Span(
            trace_id=TRACE,
            span_id="2" * 16,
            parent_span_id="1" * 16,
            name="chat m",
            start_ns=1_761_000_000_200_000_000,
            end_ns=1_761_000_000_900_000_000,
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": "m",
                "gen_ai.usage.input_tokens": 50,
                "agentwatch.usage.cost_usd": 0.01,
            },
            resource={"service.name": "svc"},
        ),
    ]


def client(tmp_path: Path | None = None) -> tuple[TestClient, Store]:
    store = Store()
    return TestClient(server.create_app(store, ui_dir=tmp_path)), store


def test_protobuf_ingest_answers_in_protobuf() -> None:
    http, store = client()
    response = http.post(
        "/v1/traces",
        content=encode(spans()).SerializeToString(),
        headers={"content-type": "application/x-protobuf"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-protobuf"
    ExportTraceServiceResponse().ParseFromString(response.content)
    assert store.span_count() == 2


def test_gzip_json_ingest() -> None:
    http, store = client()
    body = gzip.compress(json.dumps(to_otlp_json(encode(spans()))).encode())
    response = http.post(
        "/v1/traces",
        content=body,
        headers={"content-type": "application/json", "content-encoding": "gzip"},
    )
    assert response.status_code == 200
    assert response.json() == {}
    assert store.span_count() == 2


def test_bad_bodies_are_400_and_store_nothing() -> None:
    http, store = client()
    response = http.post(
        "/v1/traces", content=b"nope", headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert store.span_count() == 0


def test_oversized_request_is_413() -> None:
    http, _ = client()
    original = server.MAX_REQUEST_BYTES
    server.MAX_REQUEST_BYTES = 10
    try:
        response = http.post(
            "/v1/traces", content=b"x" * 11, headers={"content-type": "application/json"}
        )
    finally:
        server.MAX_REQUEST_BYTES = original
    assert response.status_code == 413


def test_read_api() -> None:
    http, store = client()
    store.insert(spans(), received_ns=1)
    listing = http.get("/api/traces").json()["traces"]
    assert len(listing) == 1
    # Absolute timestamps are strings so JavaScript keeps every digit.
    assert listing[0]["start_ns"] == "1761000000123456789"
    assert listing[0]["cost_usd"] == 0.01
    before = http.get("/api/traces", params={"before": listing[0]["start_ns"]}).json()
    assert before == {"traces": []}
    detail = http.get(f"/api/traces/{TRACE}").json()
    assert [s["span_id"] for s in detail["spans"]] == ["1" * 16, "2" * 16]
    assert detail["spans"][1]["genai"]["input_tokens"] == 50
    assert detail["spans"][1]["start_ns"] == "1761000000200000000"
    assert http.get("/api/services").json() == {"services": ["svc"]}
    assert http.get("/healthz").json() == {"ok": True, "spans": 2}


def test_read_api_errors() -> None:
    http, _ = client()
    assert http.get("/api/traces/xyz").status_code == 400
    assert http.get(f"/api/traces/{'d' * 32}").status_code == 404
    assert http.get("/api/traces", params={"limit": "many"}).status_code == 400


def test_serves_built_ui(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<p>ui</p>")
    http, _ = client(tmp_path)
    assert http.get("/").text == "<p>ui</p>"


def test_without_ui_root_explains() -> None:
    http, _ = client(Path("/nonexistent"))
    assert "no UI" in http.get("/").text
