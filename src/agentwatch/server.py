"""HTTP server: OTLP/HTTP trace ingest, a JSON read API, and the built UI."""

from __future__ import annotations

import time
from dataclasses import asdict
from importlib import resources
from pathlib import Path
from typing import Any

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceResponse,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from agentwatch import otlp
from agentwatch.spans import jsonable
from agentwatch.store import Store, StoredSpan, TraceSummary

# Largest compressed request accepted before decoding.
MAX_REQUEST_BYTES = 16 * 1024 * 1024


def create_app(store: Store, *, ui_dir: Path | None = None) -> Starlette:
    async def ingest(request: Request) -> Response:
        content_type = request.headers.get("content-type", "")
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_REQUEST_BYTES:
            return PlainTextResponse("request too large", status_code=413)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_REQUEST_BYTES:
                return PlainTextResponse("request too large", status_code=413)
        try:
            spans = otlp.decode(bytes(body), content_type, request.headers.get("content-encoding"))
        except otlp.OtlpError as error:
            return PlainTextResponse(str(error), status_code=400)
        store.insert(spans, time.time_ns())
        if content_type.split(";", 1)[0].strip().lower() == otlp.JSON:
            return JSONResponse({})
        return Response(ExportTraceServiceResponse().SerializeToString(), media_type=otlp.PROTOBUF)

    async def traces(request: Request) -> Response:
        query = request.query_params
        try:
            limit = int(query.get("limit", "50"))
            before = query.get("before")
            before_ns = None if before in (None, "") else int(before)
        except ValueError:
            return PlainTextResponse("limit and before must be integers", status_code=400)
        summaries = store.traces(
            limit=limit,
            before_ns=before_ns,
            service=query.get("service") or None,
            errors_only=query.get("errors") in ("1", "true"),
        )
        return JSONResponse({"traces": [_summary_json(summary) for summary in summaries]})

    async def trace(request: Request) -> Response:
        trace_id = str(request.path_params["trace_id"]).lower()
        if len(trace_id) != 32 or any(c not in "0123456789abcdef" for c in trace_id):
            return PlainTextResponse("trace ID must be 32 hex characters", status_code=400)
        spans = store.trace(trace_id)
        if not spans:
            return PlainTextResponse("trace not found", status_code=404)
        return JSONResponse({"trace_id": trace_id, "spans": [_span_json(s) for s in spans]})

    async def services(_: Request) -> Response:
        return JSONResponse({"services": store.services()})

    async def health(_: Request) -> Response:
        return JSONResponse({"ok": True, "spans": store.span_count()})

    routes: list[Route | Mount] = [
        Route("/v1/traces", ingest, methods=["POST"]),
        Route("/api/traces", traces),
        Route("/api/traces/{trace_id}", trace),
        Route("/api/services", services),
        Route("/healthz", health),
    ]
    static = ui_dir if ui_dir is not None else _bundled_ui()
    if static is not None and (static / "index.html").is_file():
        routes.append(Mount("/", StaticFiles(directory=static, html=True), name="ui"))
    else:
        routes.append(Route("/", _no_ui))
    return Starlette(routes=routes)


async def _no_ui(_: Request) -> Response:
    return PlainTextResponse(
        "agentwatch is running, but this build has no UI. "
        "Build it with `npm run build` in ui/, or use the JSON API under /api.\n"
    )


def _bundled_ui() -> Path | None:
    try:
        path = Path(str(resources.files("agentwatch") / "static"))
    except (ModuleNotFoundError, TypeError):
        return None
    return path if path.is_dir() else None


# Absolute Unix-nanosecond timestamps exceed JavaScript's exact integer range, so the
# API sends them as decimal strings. Durations stay numbers.
def _summary_json(summary: TraceSummary) -> dict[str, Any]:
    data = asdict(summary)
    data["start_ns"] = str(summary.start_ns)
    return data


def _span_json(stored: StoredSpan) -> dict[str, Any]:
    span = stored.span
    fields = stored.fields
    return {
        "trace_id": span.trace_id,
        "span_id": span.span_id,
        "parent_span_id": span.parent_span_id,
        "name": span.name,
        "kind": span.kind,
        "start_ns": str(span.start_ns),
        "end_ns": str(span.end_ns),
        "status_code": span.status_code,
        "status_message": span.status_message,
        "service_name": span.service_name,
        "scope_name": span.scope_name,
        "attributes": jsonable(span.attributes),
        "resource": jsonable(span.resource),
        "events": [
            {"name": e.name, "time_ns": str(e.time_ns), "attributes": jsonable(e.attributes)}
            for e in span.events
        ],
        "links": [
            {"trace_id": k.trace_id, "span_id": k.span_id, "attributes": jsonable(k.attributes)}
            for k in span.links
        ],
        "genai": {
            "operation": fields.operation,
            "provider": fields.provider,
            "model": fields.model,
            "input_tokens": fields.input_tokens,
            "output_tokens": fields.output_tokens,
            "cache_read_tokens": fields.cache_read_tokens,
            "cache_write_tokens": fields.cache_write_tokens,
            "reasoning_tokens": fields.reasoning_tokens,
            "cost_usd": fields.cost_usd,
            "finish_reasons": list(fields.finish_reasons),
            "tool_name": fields.tool_name,
            "agent_name": fields.agent_name,
            "error_type": fields.error_type,
        },
    }
