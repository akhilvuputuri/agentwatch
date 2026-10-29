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
    app = server.create_app(store, ui_dir=tmp_path, allowed_hosts=("testserver",))
    return TestClient(app), store


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
    page = http.get("/api/traces", params={"limit": 1}).json()
    assert page["next"] == f"{listing[0]['start_ns']}:{TRACE}"
    assert http.get("/api/traces", params={"before": page["next"]}).json() == {
        "traces": [],
        "next": None,
    }
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
    assert http.get("/api/traces", params={"before": "12"}).status_code == 400


def test_serves_built_ui(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<p>ui</p>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-abc123.js").write_text("1")
    http, _ = client(tmp_path)
    page = http.get("/")
    assert page.text == "<p>ui</p>"
    # The page is revalidated so an upgrade picks up new asset names; assets are immutable.
    assert page.headers["cache-control"] == "no-cache"
    asset = http.get("/assets/index-abc123.js")
    assert "immutable" in asset.headers["cache-control"]


def test_without_ui_root_explains() -> None:
    http, _ = client(Path("/nonexistent"))
    assert "no UI" in http.get("/").text


def test_partial_success_is_reported() -> None:
    http, store = client()
    document = to_otlp_json(encode(spans()))
    bad = dict(document["resourceSpans"][0]["scopeSpans"][0]["spans"][0])
    bad["spanId"] = "0" * 16
    document["resourceSpans"][0]["scopeSpans"][0]["spans"].append(bad)
    response = http.post("/v1/traces", json=document)
    assert response.status_code == 200
    assert response.json()["partialSuccess"]["rejectedSpans"] == "1"
    assert store.span_count() == 2


def test_unsupported_media_type_is_415() -> None:
    http, _ = client()
    response = http.post("/v1/traces", content=b"{}", headers={"content-type": "text/plain"})
    assert response.status_code == 415


def test_nan_attribute_does_not_break_the_trace_view() -> None:
    http, _ = client()
    document = to_otlp_json(encode(spans()))
    attributes = document["resourceSpans"][0]["scopeSpans"][0]["spans"][1]["attributes"]
    attributes.append({"key": "score", "value": {"doubleValue": "NaN"}})
    assert http.post("/v1/traces", json=document).status_code == 200
    detail = http.get(f"/api/traces/{TRACE}")
    assert detail.status_code == 200
    assert detail.json()["spans"][1]["attributes"]["score"] == "nan"


def test_other_host_names_are_refused() -> None:
    store = Store()
    http = TestClient(server.create_app(store), base_url="http://127.0.0.1:4318")
    assert http.get("/healthz").status_code == 200
    assert http.get("/healthz", headers={"host": "localhost:4318"}).status_code == 200
    assert http.get("/healthz", headers={"host": "[::1]:4318"}).status_code == 200
    assert http.get("/api/traces", headers={"host": "evil.example:4318"}).status_code == 403
    response = http.post(
        "/v1/traces",
        content=b"{}",
        headers={"content-type": "application/json", "origin": "http://evil.example"},
    )
    assert response.status_code == 403
    same = http.post(
        "/v1/traces",
        content=b"{}",
        headers={"content-type": "application/json", "origin": "http://127.0.0.1:4318"},
    )
    assert same.status_code == 200


def test_any_host_when_allowed() -> None:
    http = TestClient(server.create_app(Store(), allowed_hosts=("*",)))
    assert http.get("/healthz", headers={"host": "box.lan"}).status_code == 200


def test_host_name_parsing() -> None:
    assert server.host_name("LocalHost:4318") == "localhost"
    assert server.host_name("[::1]:4318") == "::1"
    assert server.host_name("::1") == "::1"
    assert server.host_name("evil.com:80@127.0.0.1") != "127.0.0.1"
    assert server.host_name("[broken") == ""


def test_unknown_enum_values_are_rejected_per_span() -> None:
    http, store = client()
    document = to_otlp_json(encode(spans()))
    document["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["kind"] = 1000
    document["resourceSpans"][0]["scopeSpans"][0]["spans"][1]["status"] = {"code": 300}
    response = http.post("/v1/traces", json=document)
    assert response.status_code == 200
    assert response.json()["partialSuccess"]["rejectedSpans"] == "2"
    assert store.span_count() == 0


def test_cache_headers_follow_the_url_not_the_directory_name(tmp_path: Path) -> None:
    ui = tmp_path / "assets"
    ui.mkdir()
    (ui / "index.html").write_text("<p>ui</p>")
    http, _ = client(ui)
    assert http.get("/").headers["cache-control"] == "no-cache"
    assert http.head("/").headers["cache-control"] == "no-cache"


def test_metrics_rejects_loose_integers() -> None:
    http, _ = client()
    for bad in ("+5", "1_000", " 5", "-1"):
        assert http.get("/api/metrics", params={"from": bad, "to": "10"}).status_code == 400
