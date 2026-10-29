"""LLM judges: a model scores sampled traces as they arrive.

A judge reads a trace's captured input and output, asks a model a question about them through
any OpenAI-compatible chat completions endpoint, and stores the answer as an EVAL score on
the trace's root span. Judges see only traces that start after the judge was created, judge a
sampled share of them, and stop for the day at a limit on model calls, so cost is bounded and
creating a judge never re-judges history. A new judge is disabled until it is switched on.

Judging is the one feature that sends trace content off this machine: the captured messages
go to the judge's endpoint. Traces without captured messages are skipped, not judged blind.
Trace content is untrusted, and a trace can try to talk the judge into a verdict; scores
from a judge are evidence, not ground truth.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any
from urllib.parse import urlsplit

import duckdb

from agentwatch.datasets import INPUT_ATTRIBUTES, OUTPUT_ATTRIBUTES, captured
from agentwatch.scores import BOOLEAN, EVAL, NUMERIC, Score, ScoreConflict
from agentwatch.store import Store, StoredSpan

log = logging.getLogger(__name__)

SCORE_TYPES = (BOOLEAN, NUMERIC)

SCORED = "SCORED"
FAILED = "FAILED"
# A model call made (or about to be) whose result is not recorded yet. It is written before the
# call, so a call whose result is lost (a crash, a reply that breaks the client) still counts.
PENDING = "PENDING"
NOT_SAMPLED = "NOT_SAMPLED"
NO_CONTENT = "NO_CONTENT"
OVER_LIMIT = "OVER_LIMIT"
# Outcomes that cost a model call, and so count toward the daily limit.
CALLED = (SCORED, FAILED, PENDING)

DAY_NS = 24 * 3600 * 1_000_000_000
# A trace is judged once no span of it has arrived for this long.
SETTLE_NS = 30 * 1_000_000_000
# Traces looked at per judge per pass.
BATCH = 20
MAX_CONTENT = 20_000
MAX_PROMPT = 20_000
MAX_DETAIL = 2000
TIMEOUT_S = 60.0

DEFAULT_PROMPT = """The user said:
{input}

The assistant answered:
{output}

Does the answer do what the user asked, correctly and completely?"""

_FORMAT = {
    BOOLEAN: 'Reply with only a JSON object: {"score": true or false, "explanation": "<one '
    'sentence>"}.',
    NUMERIC: 'Reply with only a JSON object: {"score": <a number from 0 to 1>, "explanation": '
    '"<one sentence>"}.',
}
SYSTEM = "You grade an AI assistant's work. Judge only what the transcript shows. {format}"

# A variable that holds an API key; anything else is refused, so a judge cannot be pointed at
# an unrelated secret in the server's environment.
_KEY_ENV = re.compile(r"[A-Z][A-Z0-9_]{0,99}_API_KEY")
_LOOPBACK = ("127.0.0.1", "localhost", "::1")
_PLACEHOLDER = re.compile(r"\{(input|output)\}")

# (url, headers, body, timeout) -> the parsed JSON reply. Raises on HTTP or network errors.
Post = Callable[[str, dict[str, str], bytes, float], Any]


class JudgeError(ValueError):
    """A judge definition that cannot be stored, with the reason."""


class NotFound(JudgeError):
    pass


class Conflict(JudgeError):
    pass


@dataclass(frozen=True, slots=True)
class Judge:
    id: str
    name: str
    model: str
    base_url: str
    api_key_env: str | None
    prompt: str
    score_type: str
    sample_rate: float
    daily_limit: int
    service: str | None
    enabled: bool
    created_ns: int
    # When it was last switched on; it judges traces that start after this.
    enabled_ns: int | None


@dataclass(frozen=True, slots=True)
class Judgement:
    trace_id: str
    outcome: str
    value: float | None
    detail: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    created_ns: int


@dataclass(frozen=True, slots=True)
class Verdict:
    value: float
    explanation: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None


_COLUMNS = (
    "id, name, model, base_url, api_key_env, prompt, score_type, sample_rate, daily_limit, "
    "service, enabled, created_ns, enabled_ns"
)
_JUDGEMENT_COLUMNS = (
    "trace_id, outcome, value, detail, input_tokens, output_tokens, cost_usd, created_ns"
)


# --- Definitions -------------------------------------------------------------------------


def create_judge(store: Store, body: Any, *, now_ns: int | None = None) -> Judge:
    data = _object(body)
    unknown = [key for key in data if key not in (*_FIXED, *_EDITABLE)]
    if unknown:
        raise JudgeError(f"unknown field(s): {', '.join(sorted(unknown))}")
    defaults: dict[str, Any] = {
        "prompt": DEFAULT_PROMPT,
        "score_type": BOOLEAN,
        "sample_rate": 0.1,
        "daily_limit": 100,
        "enabled": False,
    }
    created = time.time_ns() if now_ns is None else now_ns
    judge = _validated(defaults | data, judge_id=uuid.uuid4().hex, created_ns=created)
    judge = replace(judge, enabled_ns=created if judge.enabled else None)
    with store.transaction() as db:
        if db.execute("SELECT 1 FROM judges WHERE name = ?", [judge.name]).fetchone():
            raise Conflict(f"a judge named {judge.name!r} already exists")
        db.execute(
            f"INSERT INTO judges ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _values(judge),
        )
    return judge


# Fields a judge keeps for life: its name is the score name, and its type is the score's.
_FIXED = ("name", "score_type")
_EDITABLE = (
    "model",
    "base_url",
    "api_key_env",
    "prompt",
    "sample_rate",
    "daily_limit",
    "service",
    "enabled",
)


def update_judge(store: Store, judge_id: str, body: Any, *, now_ns: int | None = None) -> Judge:
    data = _object(body)
    fixed = [key for key in data if key in _FIXED]
    unknown = [key for key in data if key not in _EDITABLE and key not in _FIXED]
    if fixed:
        raise JudgeError(f"{', '.join(fixed)} cannot change; create another judge")
    if unknown:
        raise JudgeError(f"unknown field(s): {', '.join(sorted(unknown))}")
    with store.transaction() as db:
        current = _get(db, judge_id)
        judge = _validated(
            asdict(current) | data, judge_id=current.id, created_ns=current.created_ns
        )
        if judge.enabled and not current.enabled:
            # Switched on: from now, never the backlog since it was created or last on.
            judge = replace(judge, enabled_ns=time.time_ns() if now_ns is None else now_ns)
        else:
            judge = replace(judge, enabled_ns=current.enabled_ns)
        db.execute(
            "UPDATE judges SET model = ?, base_url = ?, api_key_env = ?, prompt = ?, "
            "sample_rate = ?, daily_limit = ?, service = ?, enabled = ?, enabled_ns = ? "
            "WHERE id = ?",
            [
                judge.model,
                judge.base_url,
                judge.api_key_env,
                judge.prompt,
                judge.sample_rate,
                judge.daily_limit,
                judge.service,
                judge.enabled,
                judge.enabled_ns,
                judge.id,
            ],
        )
    return judge


def delete_judge(store: Store, judge_id: str) -> None:
    """Deletes the judge and its record of judged traces; the scores it stored stay."""
    with store.transaction() as db:
        if not db.execute("DELETE FROM judges WHERE id = ? RETURNING id", [judge_id]).fetchone():
            raise NotFound("judge not found")
        db.execute("DELETE FROM judgements WHERE judge_id = ?", [judge_id])


def list_judges(store: Store) -> list[Judge]:
    rows = store.query(f"SELECT {_COLUMNS} FROM judges ORDER BY created_ns, id")
    return [_judge(row) for row in rows]


def get_judge(store: Store, judge_id: str) -> Judge:
    row = store.query_one(f"SELECT {_COLUMNS} FROM judges WHERE id = ?", [judge_id])
    if row is None:
        raise NotFound("judge not found")
    return _judge(row)


def activity(store: Store, judge_id: str, *, now_ns: int | None = None) -> dict[str, Any]:
    """Outcome counts and spend over the last 24 hours."""
    since = (time.time_ns() if now_ns is None else now_ns) - DAY_NS
    rows = store.query(
        "SELECT outcome, count(*), sum(cost_usd), count(*) FILTER (WHERE cost_usd IS NULL) "
        "FROM judgements WHERE judge_id = ? AND created_ns >= ? GROUP BY outcome",
        [judge_id, since],
    )
    counts = dict.fromkeys((SCORED, FAILED, PENDING, NOT_SAMPLED, NO_CONTENT, OVER_LIMIT), 0)
    cost = 0.0
    calls_without_cost = 0
    for outcome, count, spent, unpriced in rows:
        counts[outcome] = count
        if outcome in CALLED:
            cost += spent or 0.0
            calls_without_cost += unpriced
    return {
        "counts": counts,
        "calls": sum(counts[outcome] for outcome in CALLED),
        "cost_usd": cost,
        "calls_without_cost": calls_without_cost,
    }


def judgements(store: Store, judge_id: str, *, limit: int = 50) -> list[Judgement]:
    get_judge(store, judge_id)
    rows = store.query(
        f"SELECT {_JUDGEMENT_COLUMNS} FROM judgements WHERE judge_id = ? "
        "ORDER BY created_ns DESC, trace_id LIMIT ?",
        [judge_id, limit],
    )
    return [Judgement(*row) for row in rows]


def _validated(fields: dict[str, Any], *, judge_id: str, created_ns: int) -> Judge:
    """A judge from untrusted field values, or JudgeError naming the first bad one."""
    prompt = _text(fields.get("prompt"), MAX_PROMPT, "prompt")
    if "{output}" not in prompt:
        raise JudgeError("prompt must contain {output}, where the trace's output goes")
    score_type = fields.get("score_type")
    if score_type not in SCORE_TYPES:
        raise JudgeError(f"score_type must be one of {', '.join(SCORE_TYPES)}")
    rate = fields.get("sample_rate")
    if isinstance(rate, bool) or not isinstance(rate, int | float) or not 0 < rate <= 1:
        raise JudgeError("sample_rate must be a number above 0 and at most 1")
    limit = fields.get("daily_limit")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
        raise JudgeError("daily_limit must be a whole number from 1 to 10000")
    enabled = fields.get("enabled")
    if not isinstance(enabled, bool):
        raise JudgeError("enabled must be true or false")
    key_env = fields.get("api_key_env")
    if key_env in (None, ""):
        key_env = None
    elif not isinstance(key_env, str) or not _KEY_ENV.fullmatch(key_env):
        raise JudgeError(
            "api_key_env must name a variable ending in _API_KEY, such as OPENROUTER_API_KEY"
        )
    service = fields.get("service")
    return Judge(
        id=judge_id,
        name=_text(fields.get("name"), 200, "name"),
        model=_text(fields.get("model"), 200, "model"),
        base_url=_base_url(fields.get("base_url")),
        api_key_env=key_env,
        prompt=prompt,
        score_type=score_type,
        sample_rate=float(rate),
        daily_limit=limit,
        service=None if service in (None, "") else _text(service, 200, "service"),
        enabled=enabled,
        created_ns=created_ns,
        enabled_ns=fields.get("enabled_ns"),
    )


def _base_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise JudgeError("base_url is required, such as https://openrouter.ai/api/v1")
    try:
        parts = urlsplit(value.strip())
        host = parts.hostname or ""
    except ValueError as error:
        raise JudgeError("base_url is not a URL") from error
    if parts.username or parts.password:
        raise JudgeError("base_url must not contain credentials; use api_key_env")
    if parts.scheme == "http" and host not in _LOOPBACK:
        raise JudgeError("base_url must be https, or http on this machine")
    if parts.scheme not in ("http", "https") or not host or parts.query or parts.fragment:
        raise JudgeError("base_url must be an http(s) URL without a query")
    return value.strip().rstrip("/")


def _text(value: Any, limit: int, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise JudgeError(f"{field} is required")
    if len(value.strip()) > limit:
        raise JudgeError(f"{field} is longer than {limit} characters")
    return value.strip()


def _object(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise JudgeError("the request must be a JSON object")
    return body  # pyright: ignore[reportUnknownVariableType]


def _get(db: duckdb.DuckDBPyConnection, judge_id: str) -> Judge:
    row = db.execute(f"SELECT {_COLUMNS} FROM judges WHERE id = ?", [judge_id]).fetchone()
    if row is None:
        raise NotFound("judge not found")
    return _judge(row)


def _judge(row: Any) -> Judge:
    return Judge(*row)


def _values(judge: Judge) -> list[Any]:
    return [
        judge.id,
        judge.name,
        judge.model,
        judge.base_url,
        judge.api_key_env,
        judge.prompt,
        judge.score_type,
        judge.sample_rate,
        judge.daily_limit,
        judge.service,
        judge.enabled,
        judge.created_ns,
        judge.enabled_ns,
    ]


# --- Judging -----------------------------------------------------------------------------


def trace_content(spans: list[StoredSpan]) -> tuple[str, str, str] | None:
    """(input, output, root span ID) as text, or None when the trace captured no output.

    The root's captured messages when it has them; otherwise the earliest captured input and
    the latest captured output, since frameworks often record messages on model-call spans.
    """
    if not spans:
        return None
    root = sorted(spans, key=lambda s: (s.span.parent_span_id is not None, s.span.start_ns))[0]
    by_start = sorted(spans, key=lambda s: (s.span.start_ns, s.span.span_id))
    by_end = sorted(spans, key=lambda s: (s.span.end_ns, s.span.span_id), reverse=True)
    found_input = captured(root.span.attributes, INPUT_ATTRIBUTES)
    if found_input is None:
        found_input = next(
            (
                v
                for s in by_start
                if (v := captured(s.span.attributes, INPUT_ATTRIBUTES)) is not None
            ),
            None,
        )
    found_output = captured(root.span.attributes, OUTPUT_ATTRIBUTES)
    if found_output is None:
        found_output = next(
            (
                v
                for s in by_end
                if (v := captured(s.span.attributes, OUTPUT_ATTRIBUTES)) is not None
            ),
            None,
        )
    if found_output is None:
        return None
    return (_as_text(found_input), _as_text(found_output), root.span.span_id)


def _as_text(value: Any) -> str:
    if value is None:
        return "(not recorded)"
    text = value if isinstance(value, str) else json.dumps(value, indent=1, ensure_ascii=False)
    if len(text) > MAX_CONTENT:
        return text[:MAX_CONTENT] + f"\n[… {len(text) - MAX_CONTENT} more characters cut]"
    return text


def ask(
    judge: Judge,
    content: tuple[str, str, str],
    *,
    post: Post,
    env: Mapping[str, str],
) -> Verdict:
    """One model call for one trace. Raises JudgeError with a reason on any failure."""
    # One pass, so a trace whose input contains "{output}" is not filled in twice.
    question = _PLACEHOLDER.sub(
        lambda m: content[0] if m.group(1) == "input" else content[1], judge.prompt
    )
    body = {
        "model": judge.model,
        "temperature": 0,
        "max_tokens": 400,
        "messages": [
            {"role": "system", "content": SYSTEM.format(format=_FORMAT[judge.score_type])},
            {"role": "user", "content": question},
        ],
    }
    headers = {"content-type": "application/json"}
    if judge.api_key_env:
        key = env.get(judge.api_key_env)
        if not key:
            raise JudgeError(f"{judge.api_key_env} is not set")
        headers["authorization"] = f"Bearer {key}"
    try:
        reply = post(
            f"{judge.base_url}/chat/completions", headers, json.dumps(body).encode(), TIMEOUT_S
        )
    except urllib.error.HTTPError as error:
        raise JudgeError(f"the model endpoint answered HTTP {error.code}") from error
    except Exception as error:  # any client failure (a truncated reply raises IncompleteRead)
        raise JudgeError(f"could not reach the model endpoint: {type(error).__name__}") from error
    try:
        text = reply["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise JudgeError("the reply had no message content") from error
    value, explanation = parse_verdict(text, judge.score_type)
    usage: dict[str, Any] = reply.get("usage") or {}
    if not isinstance(usage, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
        usage = {}
    return Verdict(
        value=value,
        explanation=explanation,
        input_tokens=_count(usage.get("prompt_tokens")),
        output_tokens=_count(usage.get("completion_tokens")),
        cost_usd=_cost(usage.get("cost")),
    )


def parse_verdict(text: Any, score_type: str) -> tuple[float, str | None]:
    """The score and explanation from a model's reply: a JSON object, possibly fenced."""
    if not isinstance(text, str):
        raise JudgeError("the reply had no text")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise JudgeError("the reply was not a JSON object")
    try:
        data = json.loads(text[start : end + 1])
    except (ValueError, RecursionError) as error:
        raise JudgeError("the reply was not a JSON object") from error
    if not isinstance(data, dict) or "score" not in data:
        raise JudgeError("the reply had no score")
    raw = data["score"]  # pyright: ignore[reportUnknownVariableType]
    if score_type == BOOLEAN:
        if not isinstance(raw, bool):
            raise JudgeError("the score was not true or false")
        value = 1.0 if raw else 0.0
    else:
        if (
            isinstance(raw, bool)
            or not isinstance(raw, int | float)
            or not math.isfinite(raw)
            or not 0 <= raw <= 1
        ):
            raise JudgeError("the score was not a number from 0 to 1")
        value = float(raw)
    explanation = data.get("explanation")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return value, explanation[:MAX_DETAIL] if isinstance(explanation, str) and explanation else None


def sampled(judge_id: str, trace_id: str, rate: float) -> bool:
    """A stable draw per judge and trace, so a re-run samples the same traces."""
    digest = hashlib.sha256(f"{judge_id}:{trace_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 < rate


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error: following one could send the key to another host or scheme."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> Any:
    request = urllib.request.Request(url, data=body, method="POST")
    for name, value in headers.items():
        # Never copied to a redirect, should one ever be followed.
        request.add_unredirected_header(name.capitalize(), value)
    with _OPENER.open(request, timeout=timeout) as response:
        return json.loads(response.read())


class JudgeRunner:
    """Judges new traces in the background, one pass every `interval_s` seconds."""

    def __init__(
        self,
        store: Store,
        *,
        post: Post = urllib_post,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], int] = time.time_ns,
        settle_ns: int = SETTLE_NS,
    ) -> None:
        self.store = store
        self.post = post
        self.env: Mapping[str, str] = os.environ if env is None else env
        self.clock = clock
        self.settle_ns = settle_ns
        # The last reason each judge could not run, until it runs again.
        self.problems: dict[str, str] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, interval_s: float = 30.0) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.wait(interval_s):
                try:
                    self.run_once()
                except Exception:  # a bad pass must not end the loop
                    log.exception("judge pass failed")

        self._thread = threading.Thread(target=loop, name="agentwatch-judges", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=TIMEOUT_S + 5)
            self._thread = None

    def run_once(self) -> int:
        """One pass over every enabled judge; returns the traces recorded."""
        recorded = 0
        for judge in list_judges(self.store):
            if judge.enabled and not self._stop.is_set():
                recorded += self._run_judge(judge)
        return recorded

    def _run_judge(self, judge: Judge) -> int:
        if judge.api_key_env and not self.env.get(judge.api_key_env):
            # Traces wait until the key is available, rather than being marked failed.
            self.problems[judge.id] = f"{judge.api_key_env} is not set"
            return 0
        self.problems.pop(judge.id, None)
        recorded = 0
        for trace_id in self._candidates(judge, self.clock()):
            if self._stop.is_set():
                break
            # Switched off, deleted or given a lower limit while this pass ran: obey it now.
            try:
                judge = get_judge(self.store, judge.id)
            except NotFound:
                break
            if not judge.enabled:
                break
            if not sampled(judge.id, trace_id, judge.sample_rate):
                self._record(judge, trace_id, NOT_SAMPLED)
            elif (content := trace_content(self.store.trace(trace_id))) is None:
                self._record(judge, trace_id, NO_CONTENT)
            elif self._calls(judge) >= judge.daily_limit:
                self._record(judge, trace_id, OVER_LIMIT)
            elif self._record(judge, trace_id, PENDING):
                try:
                    verdict = ask(judge, content, post=self.post, env=self.env)
                except JudgeError as error:
                    self._finish(judge, trace_id, detail=str(error))
                else:
                    self._finish(judge, trace_id, verdict=verdict, span_id=content[2])
            recorded += 1
        return recorded

    def _calls(self, judge: Judge) -> int:
        """Model calls in the last 24 hours, including any whose result was lost."""
        placeholders = ", ".join("?" for _ in CALLED)
        return self.store.query_one(
            "SELECT count(*) FROM judgements WHERE judge_id = ? AND created_ns >= ? "
            f"AND outcome IN ({placeholders})",
            [judge.id, self.clock() - DAY_NS, *CALLED],
        )[0]

    def _candidates(self, judge: Judge, now: int) -> list[str]:
        """Settled traces that started after the judge was switched on and it has not seen."""
        rows = self.store.query(
            """
            WITH t AS (
                SELECT
                    trace_id,
                    min(start_ns) AS start_ns,
                    max(received_ns) AS last_received,
                    bool_or(parent_span_id IS NULL) AS has_root,
                    arg_min(service_name, (parent_span_id IS NOT NULL, start_ns, span_id))
                        AS service
                FROM spans
                GROUP BY trace_id
            )
            SELECT trace_id FROM t
            WHERE has_root AND start_ns >= ? AND last_received <= ?
              AND (CAST(? AS VARCHAR) IS NULL OR service = ?)
              AND trace_id NOT IN (SELECT trace_id FROM judgements WHERE judge_id = ?)
            ORDER BY start_ns, trace_id
            LIMIT ?
            """,
            [
                judge.created_ns if judge.enabled_ns is None else judge.enabled_ns,
                now - self.settle_ns,
                judge.service,
                judge.service,
                judge.id,
                BATCH,
            ],
        )
        return [row[0] for row in rows]

    def _record(self, judge: Judge, trace_id: str, outcome: str) -> bool:
        """Records an outcome for a trace; False when the judge was deleted or the trace seen."""
        with self.store.transaction() as db:
            if db.execute("SELECT 1 FROM judges WHERE id = ?", [judge.id]).fetchone() is None:
                return False
            inserted = db.execute(
                "INSERT OR IGNORE INTO judgements (judge_id, trace_id, outcome, created_ns) "
                "VALUES (?, ?, ?, ?) RETURNING trace_id",
                [judge.id, trace_id, outcome, self.clock()],
            ).fetchone()
        return inserted is not None

    def _finish(
        self,
        judge: Judge,
        trace_id: str,
        *,
        detail: str | None = None,
        verdict: Verdict | None = None,
        span_id: str | None = None,
    ) -> None:
        """Turns a PENDING judgement into SCORED, with its score, or FAILED."""
        if verdict is not None:
            try:
                with self.store.transaction() as db:
                    self._update(db, judge, trace_id, SCORED, verdict.explanation, verdict)
                    self.store.write_scores(
                        [
                            Score(
                                id=f"judge.{judge.id}.{trace_id}",
                                trace_id=trace_id,
                                span_id=span_id,
                                name=judge.name,
                                data_type=judge.score_type,
                                value=verdict.value,
                                label=None,
                                source=EVAL,
                                comment=verdict.explanation,
                                created_ns=self.clock(),
                            )
                        ]
                    )
                return
            except ScoreConflict:
                detail = "the score ID is already used on another trace"
        with self.store.transaction() as db:
            self._update(db, judge, trace_id, FAILED, detail, verdict)

    @staticmethod
    def _update(
        db: duckdb.DuckDBPyConnection,
        judge: Judge,
        trace_id: str,
        outcome: str,
        detail: str | None,
        verdict: Verdict | None,
    ) -> None:
        db.execute(
            "UPDATE judgements SET outcome = ?, value = ?, detail = ?, input_tokens = ?, "
            "output_tokens = ?, cost_usd = ? WHERE judge_id = ? AND trace_id = ?",
            [
                outcome,
                verdict.value if verdict is not None and outcome == SCORED else None,
                detail,
                None if verdict is None else verdict.input_tokens,
                None if verdict is None else verdict.output_tokens,
                None if verdict is None else verdict.cost_usd,
                judge.id,
                trace_id,
            ],
        )


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _cost(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None
