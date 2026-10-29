"""HTTP server: OTLP/HTTP trace ingest, a JSON read API, and the built UI."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict
from importlib import resources
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import duckdb
from google.protobuf import json_format
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceResponse
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

from agentwatch import comparisons, datasets, experiments, judges, metrics, otlp, prices, scores
from agentwatch.scores import Score
from agentwatch.spans import jsonable
from agentwatch.store import (
    Cursor,
    Price,
    SessionCursor,
    SessionSummary,
    Store,
    StoredSpan,
    TraceSummary,
)

# Largest compressed request accepted before decoding.
MAX_REQUEST_BYTES = 16 * 1024 * 1024

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
DAY_NS = 24 * 3600 * 1_000_000_000
MAX_SCORE_REQUEST_BYTES = 1024 * 1024
MAX_SCORES_PER_REQUEST = 1000
MAX_JSON_WRITE_BYTES = 16 * 1024 * 1024
MAX_ITEMS_PER_REQUEST = 1000
# Traces returned with one session.
SESSION_TRACES = 500


def create_app(
    store: Store,
    *,
    ui_dir: Path | None = None,
    allowed_hosts: Sequence[str] = LOOPBACK_HOSTS,
    judge_runner: judges.JudgeRunner | None = None,
) -> Starlette:
    """`allowed_hosts` are the Host header names served; "*" allows any (no rebinding guard).

    `judge_runner`, when given, is the background runner whose problems the judges API reports
    and whose model client a judge's trial uses.
    """

    async def ingest(request: Request) -> Response:
        content_type = request.headers.get("content-type", "")
        body = await _read_body(request)
        if body is None:
            return PlainTextResponse("request too large", status_code=413)
        encoding = request.headers.get("content-encoding")
        try:
            decoded = await run_in_threadpool(otlp.decode, body, content_type, encoding)
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

    async def ingest_logs(request: Request) -> Response:
        """OTLP logs: gen_ai.evaluation.result events become scores; other records are ignored."""
        content_type = request.headers.get("content-type", "")
        body = await _read_body(request)
        if body is None:
            return PlainTextResponse("request too large", status_code=413)
        encoding = request.headers.get("content-encoding")
        try:
            decoded = await run_in_threadpool(otlp.decode_logs, body, content_type, encoding)
        except otlp.UnsupportedMediaType as error:
            return PlainTextResponse(str(error), status_code=415)
        except otlp.OtlpError as error:
            return PlainTextResponse(str(error), status_code=400)
        skipped = await run_in_threadpool(store.insert_event_scores, decoded.scores)
        response = ExportLogsServiceResponse()
        rejected = decoded.rejected + skipped
        if rejected:
            response.partial_success.rejected_log_records = rejected
            response.partial_success.error_message = decoded.error or (
                f"{skipped} record(s) rejected; first: score id already used on another trace"
            )
        if content_type.split(";", 1)[0].strip().lower() == otlp.JSON:
            return JSONResponse(json_format.MessageToDict(response))
        return Response(response.SerializeToString(), media_type=otlp.PROTOBUF)

    async def post_scores(request: Request) -> Response:
        """One score object, or {"scores": [...]}; all are validated before any is stored."""
        document = await _json_body(request, MAX_SCORE_REQUEST_BYTES)
        if isinstance(document, Response):
            return document
        items: Any
        if isinstance(document, dict) and "scores" in document:
            items = cast(dict[str, Any], document)["scores"]
        else:
            items = [cast(Any, document)]
        if (
            not isinstance(items, list)
            or not 1 <= len(cast(list[Any], items)) <= MAX_SCORES_PER_REQUEST
        ):
            return PlainTextResponse(
                f'send one score or 1 to {MAX_SCORES_PER_REQUEST} under "scores"', status_code=400
            )
        source = (
            scores.ANNOTATION if request.query_params.get("source") == "annotation" else scores.API
        )
        try:
            found = [scores.from_api(item, source=source) for item in cast(list[Any], items)]
        except scores.ScoreError as error:
            return PlainTextResponse(str(error), status_code=400)
        try:
            await run_in_threadpool(store.upsert_scores, found)
        except scores.ScoreConflict as error:
            return PlainTextResponse(str(error), status_code=409)
        return JSONResponse(
            {"ids": [score.id for score in found], "scores": [_score_json(s) for s in found]},
            status_code=201,
        )

    async def datasets_index(request: Request) -> Response:
        if request.method == "POST":
            body = await _json_body(request)
            if isinstance(body, Response):
                return body
            return await _dataset_call(
                lambda: {"dataset": _dataset_json(datasets.create_dataset(store, body))}, status=201
            )
        found = await run_in_threadpool(datasets.list_datasets, store)
        return JSONResponse({"datasets": [_dataset_json(d) for d in found]})

    async def dataset_detail(request: Request) -> Response:
        dataset_id = str(request.path_params["dataset_id"])
        if request.method == "DELETE":
            return await _dataset_call(
                lambda: datasets.delete_dataset(store, dataset_id), status=204
            )
        return await _dataset_call(
            lambda: {"dataset": _dataset_json(datasets.get_dataset(store, dataset_id))}
        )

    async def dataset_items(request: Request) -> Response:
        dataset_id = str(request.path_params["dataset_id"])
        if request.method == "POST":
            body = await _json_body(request)
            if isinstance(body, Response):
                return body
            items: Any
            if isinstance(body, dict) and "items" in body:
                items = cast(dict[str, Any], body)["items"]
            else:
                items = [cast(Any, body)]
            if (
                not isinstance(items, list)
                or not 1 <= len(cast(list[Any], items)) <= MAX_ITEMS_PER_REQUEST
            ):
                return PlainTextResponse(
                    f'send one item or 1 to {MAX_ITEMS_PER_REQUEST} under "items"', status_code=400
                )
            added = cast(list[Any], items)
            return await _dataset_call(
                lambda: {
                    "items": [_item_json(i) for i in datasets.add_items(store, dataset_id, added)]
                },
                status=201,
            )
        archived = request.query_params.get("archived") in ("1", "true")
        return await _dataset_call(
            lambda: {
                "items": [
                    _item_json(i) for i in datasets.list_items(store, dataset_id, archived=archived)
                ]
            }
        )

    async def dataset_item_from_trace(request: Request) -> Response:
        dataset_id = str(request.path_params["dataset_id"])
        body = await _json_body(request)
        if isinstance(body, Response):
            return body
        return await _dataset_call(
            lambda: {"item": _item_json(datasets.item_from_trace(store, dataset_id, body))},
            status=201,
        )

    async def dataset_item(request: Request) -> Response:
        item_id = str(request.path_params["item_id"])
        body = await _json_body(request)
        if isinstance(body, Response):
            return body
        return await _dataset_call(
            lambda: datasets.set_item_status(store, item_id, body), status=204
        )

    async def dataset_runs(request: Request) -> Response:
        dataset_id = str(request.path_params["dataset_id"])
        if request.method == "POST":
            body = await _json_body(request)
            if isinstance(body, Response):
                return body
            return await _dataset_call(
                lambda: {"run": _run_json(experiments.create_run(store, dataset_id, body))},
                status=201,
            )
        return await _dataset_call(
            lambda: {
                "runs": [
                    summary | {"run": _run_json(summary["run"])}
                    for summary in experiments.list_runs(store, dataset_id)
                ]
            }
        )

    async def run_detail(request: Request) -> Response:
        run_id = str(request.path_params["run_id"])
        if request.method == "DELETE":
            return await _dataset_call(lambda: experiments.delete_run(store, run_id), status=204)
        return await _dataset_call(
            lambda: {
                "run": _run_json(experiments.get_run(store, run_id)),
                "items": experiments.run_items(store, run_id),
            }
        )

    async def run_items_post(request: Request) -> Response:
        run_id = str(request.path_params["run_id"])
        body = await _json_body(request)
        if isinstance(body, Response):
            return body
        items: Any
        if isinstance(body, dict) and "items" in body:
            items = cast(dict[str, Any], body)["items"]
        else:
            items = [cast(Any, body)]
        if (
            not isinstance(items, list)
            or not 1 <= len(cast(list[Any], items)) <= MAX_ITEMS_PER_REQUEST
        ):
            return PlainTextResponse(
                f'send one run item or 1 to {MAX_ITEMS_PER_REQUEST} under "items"', status_code=400
            )
        added = cast(list[Any], items)
        return await _dataset_call(
            lambda: {
                "items": [
                    {"item_id": i.item_id, "trace_id": i.trace_id}
                    for i in experiments.add_run_items(store, run_id, added)
                ]
            },
            status=201,
        )

    async def compare(request: Request) -> Response:
        query = request.query_params
        baseline, candidate = query.get("baseline", ""), query.get("candidate", "")
        if not baseline or not candidate:
            return PlainTextResponse("baseline and candidate run IDs are required", status_code=400)
        margins: dict[str, float] = {}
        for key in ("margin", "relative_margin"):
            try:
                margins[key] = float(query.get(key, "0.05"))
            except ValueError:
                margins[key] = -1.0
            if not 0 < margins[key] <= 1:
                return PlainTextResponse(f"{key} must be a number above 0 and at most 1", 400)
        return await _dataset_call(
            lambda: comparisons.compare_runs(
                store,
                baseline,
                candidate,
                margin=margins["margin"],
                relative_margin=margins["relative_margin"],
            )
        )

    async def list_prices(_: Request) -> Response:
        found = await run_in_threadpool(store.prices)
        return JSONResponse({"prices": [_price_json(price) for price in found]})

    async def post_price(request: Request) -> Response:
        document = await _json_body(request, MAX_SCORE_REQUEST_BYTES)
        if isinstance(document, Response):
            return document
        try:
            price = prices.from_api(document)
        except prices.PriceError as error:
            return PlainTextResponse(str(error), status_code=400)
        problem = await run_in_threadpool(store.check_pattern, price.match_pattern)
        if problem is not None:
            return PlainTextResponse(
                f"match_pattern is not a valid regular expression: {problem}", 400
            )
        await run_in_threadpool(store.upsert_price, price)
        return JSONResponse({"price": _price_json(price)}, status_code=201)

    def judge_json(judge: judges.Judge) -> dict[str, Any]:
        return asdict(judge) | {
            "created_ns": str(judge.created_ns),
            "activity": judges.activity(store, judge.id),
            "problem": judge_runner.problems.get(judge.id) if judge_runner else None,
        }

    async def judges_index(request: Request) -> Response:
        if request.method == "GET":
            return await _judge_call(
                lambda: {"judges": [judge_json(j) for j in judges.list_judges(store)]}
            )
        body = await _json_body(request, MAX_SCORE_REQUEST_BYTES)
        if isinstance(body, Response):
            return body
        return await _judge_call(
            lambda: {"judge": judge_json(judges.create_judge(store, body))}, status=201
        )

    async def judge_detail(request: Request) -> Response:
        judge_id = str(request.path_params["judge_id"])
        if request.method == "GET":
            return await _judge_call(
                lambda: {"judge": judge_json(judges.get_judge(store, judge_id))}
            )
        if request.method == "DELETE":
            return await _judge_call(lambda: judges.delete_judge(store, judge_id), status=204)
        body = await _json_body(request, MAX_SCORE_REQUEST_BYTES)
        if isinstance(body, Response):
            return body
        return await _judge_call(
            lambda: {"judge": judge_json(judges.update_judge(store, judge_id, body))}
        )

    async def judge_judgements(request: Request) -> Response:
        judge_id = str(request.path_params["judge_id"])
        try:
            limit = max(1, min(int(request.query_params.get("limit", "50")), 500))
        except ValueError:
            return PlainTextResponse("limit must be an integer", status_code=400)
        return await _judge_call(
            lambda: {
                "judgements": [
                    asdict(j) | {"created_ns": str(j.created_ns)}
                    for j in judges.judgements(store, judge_id, limit=limit)
                ]
            }
        )

    async def judge_try(request: Request) -> Response:
        """Judges one trace now and returns the verdict without storing it: one model call."""
        judge_id = str(request.path_params["judge_id"])
        body = await _json_body(request, MAX_SCORE_REQUEST_BYTES)
        if isinstance(body, Response):
            return body
        trace_id = body.get("trace_id") if isinstance(body, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if not isinstance(trace_id, str) or len(trace_id) != 32:
            return PlainTextResponse("trace_id must be 32 hex characters", status_code=400)
        post = judge_runner.post if judge_runner else judges.urllib_post
        env = judge_runner.env if judge_runner else os.environ

        def work() -> dict[str, Any]:
            judge = judges.get_judge(store, judge_id)
            spans = store.trace(trace_id)
            if not spans:
                raise datasets.NotFound("trace not found")
            content = judges.trace_content(spans)
            if content is None:
                raise judges.JudgeError("this trace captured no output to judge")
            try:
                verdict = judges.ask(judge, content, post=post, env=env)
            except judges.JudgeError as error:
                raise ModelFailure(str(error)) from error
            return {"verdict": asdict(verdict)}

        return await _judge_call(work)

    async def delete_price(request: Request) -> Response:
        removed = await run_in_threadpool(store.delete_price, str(request.path_params["price_id"]))
        return (
            Response(status_code=204)
            if removed
            else PlainTextResponse("price not found", status_code=404)
        )

    async def delete_score(request: Request) -> Response:
        removed = await run_in_threadpool(store.delete_score, str(request.path_params["score_id"]))
        return (
            Response(status_code=204)
            if removed
            else PlainTextResponse("score not found", status_code=404)
        )

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
            session=query.get("session") or None,
            user=query.get("user") or None,
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
        found = await run_in_threadpool(store.scores, trace_id)
        [summary] = await run_in_threadpool(store.traces, limit=1, trace=trace_id)
        return JSONResponse(
            {
                "trace_id": trace_id,
                "session_id": summary.session_id,
                "user_id": summary.user_id,
                "spans": [_span_json(s) for s in spans],
                "scores": [_score_json(score) for score in found],
            }
        )

    async def metrics_view(request: Request) -> Response:
        query = request.query_params
        service = query.get("service") or None
        now = time.time_ns()
        if query.get("range") == "all":
            window = await run_in_threadpool(metrics.window_for_all, store, service, now)
        else:
            raw_to, raw_from = query.get("to", ""), query.get("from", "")
            # At most 19 digits: anything longer is out of range (and too long for int()).
            if not all(
                v.isascii() and v.isdigit() and len(v) <= 19 for v in (raw_to, raw_from) if v
            ):
                return PlainTextResponse("from and to must be Unix nanoseconds", status_code=400)
            end = int(raw_to) if raw_to else now
            start = int(raw_from) if raw_from else end - DAY_NS
            if not 0 <= start < end <= otlp.MAX_TIME_NS:
                return PlainTextResponse("need 0 <= from < to", status_code=400)
            window = metrics.Window(start, end, service)
        return JSONResponse(await run_in_threadpool(metrics.metrics, store, window))

    async def sessions(request: Request) -> Response:
        query = request.query_params
        try:
            limit = max(1, min(int(query.get("limit", "50")), 500))
            before = query.get("before")
            cursor = None if before in (None, "") else SessionCursor.decode(before)
        except ValueError:
            return PlainTextResponse(
                "limit must be an integer and before a cursor from `next`", status_code=400
            )
        found = await run_in_threadpool(
            store.sessions,
            limit=limit,
            before=cursor,
            service=query.get("service") or None,
            user=query.get("user") or None,
        )
        following = found[-1].cursor.encode() if len(found) == limit else None
        return JSONResponse({"sessions": [_session_json(s) for s in found], "next": following})

    async def session(request: Request) -> Response:
        session_id = str(request.path_params["session_id"])
        found = await run_in_threadpool(store.sessions, limit=1, session=session_id)
        if not found:
            return PlainTextResponse("session not found", status_code=404)
        traces = await run_in_threadpool(store.traces, limit=SESSION_TRACES, session=session_id)
        return JSONResponse(
            {
                "session": _session_json(found[0]),
                # Oldest first, as a conversation reads; the most recent SESSION_TRACES if longer.
                "traces": [_summary_json(t) for t in reversed(traces)],
                "truncated": found[0].traces > len(traces),
            }
        )

    async def services(_: Request) -> Response:
        return JSONResponse({"services": await run_in_threadpool(store.services)})

    async def health(_: Request) -> Response:
        return JSONResponse({"ok": True, "spans": await run_in_threadpool(store.span_count)})

    routes: list[Route | Mount] = [
        Route("/v1/traces", ingest, methods=["POST"]),
        Route("/v1/logs", ingest_logs, methods=["POST"]),
        Route("/api/scores", post_scores, methods=["POST"]),
        Route("/api/scores/{score_id}", delete_score, methods=["DELETE"]),
        Route("/api/datasets", datasets_index, methods=["GET", "POST"]),
        Route("/api/datasets/{dataset_id}", dataset_detail, methods=["GET", "DELETE"]),
        Route("/api/datasets/{dataset_id}/items", dataset_items, methods=["GET", "POST"]),
        Route(
            "/api/datasets/{dataset_id}/items/from-trace", dataset_item_from_trace, methods=["POST"]
        ),
        Route("/api/dataset-items/{item_id}", dataset_item, methods=["PATCH"]),
        Route("/api/datasets/{dataset_id}/runs", dataset_runs, methods=["GET", "POST"]),
        Route("/api/compare", compare, methods=["GET"]),
        Route("/api/runs/{run_id}", run_detail, methods=["GET", "DELETE"]),
        Route("/api/runs/{run_id}/items", run_items_post, methods=["POST"]),
        Route("/api/judges", judges_index, methods=["GET", "POST"]),
        Route("/api/judges/{judge_id}", judge_detail, methods=["GET", "PATCH", "DELETE"]),
        Route("/api/judges/{judge_id}/judgements", judge_judgements, methods=["GET"]),
        Route("/api/judges/{judge_id}/try", judge_try, methods=["POST"]),
        Route("/api/prices", list_prices, methods=["GET"]),
        Route("/api/prices", post_price, methods=["POST"]),
        Route("/api/prices/{price_id}", delete_price, methods=["DELETE"]),
        Route("/api/traces", traces),
        Route("/api/traces/{trace_id}", trace),
        Route("/api/metrics", metrics_view),
        Route("/api/sessions", sessions),
        Route("/api/sessions/{session_id:path}", session),
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
        host = headers.get("host", "")
        if host_name(host) not in self.allowed or (
            origin is not None and not _same_origin(origin, host, self.allowed)
        ):
            await PlainTextResponse("host not allowed", status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _same_origin(origin: str, host: str, allowed: frozenset[str]) -> bool:
    """A browser Origin names an allowed host on the same port as the request's Host header.

    Comparing ports keeps a page served from another local port (another dev server, say)
    from writing here, although both are on localhost.
    """
    try:
        parts = urlsplit(origin)
        origin_port = parts.port or {"http": 80, "https": 443}.get(parts.scheme)
    except ValueError:
        return False
    if (parts.hostname or "") not in allowed:
        return False
    return origin_port == host_port(host, parts.scheme)


def host_port(authority: str, scheme: str = "http") -> int | None:
    """The port of a Host header, or the scheme's default when it has none."""
    authority = authority.strip()
    tail = authority.rsplit("]", 1)[-1] if authority.startswith("[") else authority
    if tail.count(":") == 1:
        port = tail.rsplit(":", 1)[1]
        return int(port) if port.isdigit() else None
    return {"http": 80, "https": 443}.get(scheme)


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


def _reject_constant(token: str) -> Any:
    raise ValueError(f"{token} is not valid JSON")


def _parse_json(body: bytes) -> Any:
    """Strict JSON: no NaN or Infinity (which Python accepts but nothing can return)."""
    return json.loads(body, parse_constant=_reject_constant)


async def _json_body(request: Request, limit: int | None = None) -> Any:
    """The parsed JSON body of a write, or the error Response to return instead.

    Writes must be application/json, which forces a preflight on any cross-origin browser
    request; this server never answers one.
    """
    media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media != otlp.JSON:
        return PlainTextResponse("send application/json", status_code=415)
    body = await _read_body(request, MAX_JSON_WRITE_BYTES if limit is None else limit)
    if body is None:
        return PlainTextResponse("request too large", status_code=413)
    try:
        return _parse_json(body)
    except (UnicodeDecodeError, ValueError, RecursionError):
        # ValueError covers JSONDecodeError and NaN/Infinity; RecursionError, absurd nesting.
        return PlainTextResponse("body must be standard JSON", status_code=400)


async def _dataset_call(work: Callable[[], Any], *, status: int = 200) -> Response:
    """Run a datasets operation in the threadpool and map its errors to HTTP statuses."""
    try:
        result = await run_in_threadpool(work)
    except datasets.NotFound as error:
        return PlainTextResponse(str(error), status_code=404)
    except datasets.Conflict as error:
        return PlainTextResponse(str(error), status_code=409)
    except datasets.DatasetError as error:
        return PlainTextResponse(str(error), status_code=400)
    if status == 204:
        return Response(status_code=204)
    return JSONResponse(result, status_code=status)


class ModelFailure(Exception):
    """The judge's model endpoint failed or answered something unusable."""


async def _judge_call(work: Callable[[], Any], *, status: int = 200) -> Response:
    """Run a judges operation in the threadpool and map its errors to HTTP statuses."""
    try:
        result = await run_in_threadpool(work)
    except (judges.NotFound, datasets.NotFound) as error:
        return PlainTextResponse(str(error), status_code=404)
    except judges.Conflict as error:
        return PlainTextResponse(str(error), status_code=409)
    except judges.JudgeError as error:
        return PlainTextResponse(str(error), status_code=400)
    except ModelFailure as error:
        return PlainTextResponse(str(error), status_code=502)
    if status == 204:
        return Response(status_code=204)
    return JSONResponse(result, status_code=status)


def _dataset_json(dataset: datasets.Dataset) -> dict[str, Any]:
    return asdict(dataset) | {"created_ns": str(dataset.created_ns)}


def _run_json(run: experiments.Run) -> dict[str, Any]:
    return asdict(run) | {"created_ns": str(run.created_ns)}


def _item_json(item: datasets.Item) -> dict[str, Any]:
    return asdict(item) | {"created_ns": str(item.created_ns)}


async def _read_body(request: Request, limit: int | None = None) -> bytes | None:
    """The request body, or None when it is larger than `limit` (default MAX_REQUEST_BYTES)."""
    limit = MAX_REQUEST_BYTES if limit is None else limit
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return None
    return bytes(body)


def _price_json(price: Price) -> dict[str, Any]:
    data = asdict(price)
    data["created_ns"] = str(price.created_ns)
    data["start_ns"] = None if price.start_ns is None else str(price.start_ns)
    return data


def _score_json(score: Score) -> dict[str, Any]:
    data = asdict(score)
    data["created_ns"] = str(score.created_ns)
    return data


def _session_json(summary: SessionSummary) -> dict[str, Any]:
    data = asdict(summary)
    for key in ("first_start_ns", "last_start_ns", "last_end_ns"):
        data[key] = str(data[key])
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
            "cost_usd": stored.cost_usd,
            "cost_source": stored.cost_source,
            "finish_reasons": list(fields.finish_reasons),
            "tool_name": fields.tool_name,
            "agent_name": fields.agent_name,
            "error_type": fields.error_type,
            "conversation_id": fields.conversation_id,
            "user_id": fields.user_id,
        },
    }
