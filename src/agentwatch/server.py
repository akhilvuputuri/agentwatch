"""HTTP server: OTLP/HTTP trace ingest, a JSON read API, and the built UI."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import asdict
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import duckdb
from google.protobuf import json_format
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceResponse,
)
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from agentwatch import metrics, otlp
from agentwatch.spans import jsonable
from agentwatch.store import Cursor, Store, StoredSpan, TraceSummary

# Largest compressed request accepted before decoding.
MAX_REQUEST_BYTES = 16 * 1024 * 1024

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
DAY_NS = 24 * 3600 * 1_000_000_000


def create_app(
    store: Store,
    *,
    ui_dir: Path | None = None,
    allowed_hosts: Sequence[str] = LOOPBACK_HOSTS,
) -> Starlette:
    """`allowed_hosts` are the Host header names served; "*" allows any (no rebinding guard)."""

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
        encoding = request.headers.get("content-encoding")
        try:
            decoded = await run_in_threadpool(otlp.decode, bytes(body), content_type, encoding)
        except otlp.UnsupportedMediaType as error:
            return PlainTextResponse(str(error), status_code=415)
        except otlp.OtlpError as error:
            return PlainTextResponse(str(error), status_code=400)
        try:
            await run_in_threadpool(store.insert, decoded.spans, time.time_ns())
        except duckdb.Error as error:
            # A value the decoder let through but the database refused; not worth a retry.
            return PlainTextResponse(f"could not store spans: {type(error).__name__}", 400)
        response = ExportTraceServiceResponse()
        if decoded.rejected:
            response.partial_success.rejected_spans = decoded.rejected
            response.partial_success.error_message = decoded.error
        if content_type.split(";", 1)[0].strip().lower() == otlp.JSON:
            return JSONResponse(json_format.MessageToDict(response))
        return Response(response.SerializeToString(), media_type=otlp.PROTOBUF)

    async def traces(request: Request) -> Response:
        query = request.query_params
        try:
            limit = max(1, min(int(query.get("limit", "50")), 500))
            before = query.get("before")
            cursor = None if before in (None, "") else Cursor.decode(before)
        except ValueError:
            return PlainTextResponse(
                "limit must be an integer and before a cursor from `next`", status_code=400
            )
        summaries = await run_in_threadpool(
            store.traces,
            limit=limit,
            before=cursor,
            service=query.get("service") or None,
            errors_only=query.get("errors") in ("1", "true"),
        )
        following = summaries[-1].cursor.encode() if len(summaries) == limit else None
        return JSONResponse(
            {"traces": [_summary_json(summary) for summary in summaries], "next": following}
        )

    async def trace(request: Request) -> Response:
        trace_id = str(request.path_params["trace_id"]).lower()
        if len(trace_id) != 32 or any(c not in "0123456789abcdef" for c in trace_id):
            return PlainTextResponse("trace ID must be 32 hex characters", status_code=400)
        spans = await run_in_threadpool(store.trace, trace_id)
        if not spans:
            return PlainTextResponse("trace not found", status_code=404)
        return JSONResponse({"trace_id": trace_id, "spans": [_span_json(s) for s in spans]})

    async def metrics_view(request: Request) -> Response:
        query = request.query_params
        service = query.get("service") or None
        now = time.time_ns()
        if query.get("range") == "all":
            window = await run_in_threadpool(metrics.window_for_all, store, service, now)
        else:
            raw_to, raw_from = query.get("to", ""), query.get("from", "")
            if not all(v.isascii() and v.isdigit() for v in (raw_to, raw_from) if v):
                return PlainTextResponse("from and to must be Unix nanoseconds", status_code=400)
            end = int(raw_to) if raw_to else now
            start = int(raw_from) if raw_from else end - DAY_NS
            if not 0 <= start < end <= otlp.MAX_TIME_NS:
                return PlainTextResponse("need 0 <= from < to", status_code=400)
            window = metrics.Window(start, end, service)
        return JSONResponse(await run_in_threadpool(metrics.metrics, store, window))

    async def services(_: Request) -> Response:
        return JSONResponse({"services": await run_in_threadpool(store.services)})

    async def health(_: Request) -> Response:
        return JSONResponse({"ok": True, "spans": await run_in_threadpool(store.span_count)})

    routes: list[Route | Mount] = [
        Route("/v1/traces", ingest, methods=["POST"]),
        Route("/api/traces", traces),
        Route("/api/traces/{trace_id}", trace),
        Route("/api/metrics", metrics_view),
        Route("/api/services", services),
        Route("/healthz", health),
    ]
    static = ui_dir if ui_dir is not None else _bundled_ui()
    if static is not None and (static / "index.html").is_file():
        routes.append(Mount("/", UiFiles(directory=static, html=True), name="ui"))
    else:
        routes.append(Route("/", _no_ui))
    middleware = [Middleware(HostGuard, allowed=tuple(allowed_hosts))]
    return Starlette(routes=routes, middleware=middleware)


class UiFiles(StaticFiles):
    """The built UI. Asset names carry a content hash, so they can be cached for good;
    the page itself must be revalidated, or an upgraded install keeps loading old assets."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        # `path` is relative to the mount, so this tests the URL, not the directory name.
        if path.replace("\\", "/").startswith("assets/") and response.status_code in (200, 304):
            response.headers["cache-control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["cache-control"] = "no-cache"
        return response


class HostGuard:
    """Refuse requests for other host names, so a web page cannot reach this local server
    by DNS rebinding, and cross-origin browser requests to it."""

    def __init__(self, app: ASGIApp, allowed: tuple[str, ...]) -> None:
        self.app = app
        self.any = "*" in allowed
        self.allowed = frozenset(host.lower().strip("[]") for host in allowed)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.any:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if host_name(headers.get("host", "")) not in self.allowed or (
            origin is not None and (urlsplit(origin).hostname or "") not in self.allowed
        ):
            await PlainTextResponse("host not allowed", status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def host_name(authority: str) -> str:
    """The host part of a Host header, lowercased, without port or brackets."""
    authority = authority.strip().lower()
    if authority.startswith("["):
        return authority[1 : authority.find("]")] if "]" in authority else ""
    return authority.split(":", 1)[0] if authority.count(":") <= 1 else authority


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
