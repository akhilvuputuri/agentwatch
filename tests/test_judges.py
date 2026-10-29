from __future__ import annotations

import json
import urllib.error
from typing import Any

import pytest
from starlette.testclient import TestClient

from agentwatch import judges, server
from agentwatch.scores import BOOLEAN, EVAL, NUMERIC
from agentwatch.spans import Span
from agentwatch.store import Store

SECOND = 1_000_000_000
CREATED = 1_000 * SECOND
JUDGE = {
    "name": "helpful",
    "model": "judge/model",
    "base_url": "https://models.example/v1/",
    "api_key_env": "JUDGE_API_KEY",
    "sample_rate": 1,
    "daily_limit": 3,
    "enabled": True,
}


class FakeModel:
    """An OpenAI-compatible endpoint that answers from a queue and records each request."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    def __call__(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> Any:
        self.requests.append((url, headers, json.loads(body)))
        reply = self.replies.pop(0) if self.replies else _reply('{"score": true}')
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _reply(content: str, **usage: Any) -> dict[str, Any]:
    return {"choices": [{"message": {"content": content}}], "usage": usage}


def trace(
    db: Store,
    n: int,
    *,
    start: int = CREATED + SECOND,
    received: int = CREATED + 2 * SECOND,
    output: str | None = "It is 4.",
    service: str = "app",
) -> str:
    trace_id = f"{n:032x}"
    attributes: dict[str, Any] = {"gen_ai.input.messages": json.dumps([{"content": "2+2?"}])}
    if output is not None:
        attributes["gen_ai.output.messages"] = json.dumps([{"content": output}])
    root = Span(
        trace_id=trace_id,
        span_id="1" * 16,
        parent_span_id=None,
        name="agent",
        start_ns=start,
        end_ns=start + SECOND,
        attributes=attributes,
        resource={"service.name": service},
    )
    db.insert([root], received_ns=received)
    return trace_id


def make(db: Store, **fields: Any) -> judges.Judge:
    return judges.create_judge(db, JUDGE | fields, now_ns=CREATED)


def runner(db: Store, model: FakeModel, *, now: int = CREATED + 60 * SECOND) -> judges.JudgeRunner:
    return judges.JudgeRunner(db, post=model, env={"JUDGE_API_KEY": "k"}, clock=lambda: now)


def test_a_judge_scores_new_traces_on_their_root_span() -> None:
    db = Store()
    judge = make(db)
    first = trace(db, 1)
    model = FakeModel(
        _reply(
            '```json\n{"score": false, "explanation": "Wrong sum."}\n```',
            cost=0.002,
            prompt_tokens=50,
            completion_tokens=9,
        )
    )
    assert runner(db, model).run_once() == 1

    url, headers, body = model.requests[0]
    assert url == "https://models.example/v1/chat/completions"
    assert headers["authorization"] == "Bearer k"
    assert body["model"] == "judge/model"
    assert body["temperature"] == 0
    assert "2+2?" in body["messages"][1]["content"]
    assert "It is 4." in body["messages"][1]["content"]
    [score] = db.scores(first)
    assert (score.name, score.data_type, score.value, score.source) == (
        "helpful",
        BOOLEAN,
        0.0,
        EVAL,
    )
    assert score.span_id == "1" * 16
    assert score.comment == "Wrong sum."
    [seen] = judges.judgements(db, judge.id)
    assert (seen.outcome, seen.cost_usd, seen.input_tokens, seen.output_tokens) == (
        judges.SCORED,
        0.002,
        50,
        9,
    )
    # A trace is judged once.
    assert runner(db, model).run_once() == 0
    assert len(model.requests) == 1


def test_old_unsettled_and_other_service_traces_wait_or_are_skipped() -> None:
    db = Store()
    make(db, service="app")
    trace(db, 1, start=CREATED - SECOND)  # before the judge existed: never judged
    trace(db, 2, received=CREATED + 59 * SECOND)  # still arriving
    trace(db, 3, service="other")
    model = FakeModel()
    assert runner(db, model).run_once() == 0
    # Once settled, the waiting trace is judged.
    assert runner(db, model, now=CREATED + 120 * SECOND).run_once() == 1
    assert [s.trace_id for s in db.scores(f"{2:032x}")] == [f"{2:032x}"]


def test_sampling_content_and_the_daily_limit_bound_model_calls() -> None:
    db = Store()
    judge = make(db, daily_limit=2, sample_rate=0.5)
    ids = [trace(db, n) for n in range(1, 41)]
    blind = trace(db, 99, output=None)
    model = FakeModel()
    run = runner(db, model)
    while run.run_once():
        pass
    outcomes = {j.trace_id: j.outcome for j in judges.judgements(db, judge.id, limit=500)}
    assert len(model.requests) == 2
    assert sum(o == judges.SCORED for o in outcomes.values()) == 2
    sampled = [t for t in ids if judges.sampled(judge.id, t, 0.5)]
    assert 10 < len(sampled) < 30
    not_sampled = {t for t, o in outcomes.items() if o == judges.NOT_SAMPLED} - {blind}
    assert not_sampled == set(ids) - set(sampled)
    assert sum(o == judges.OVER_LIMIT for o in outcomes.values()) == len(sampled) - 2
    # Traces without a captured output are never sent (when sampled at all).
    assert outcomes[blind] in (judges.NO_CONTENT, judges.NOT_SAMPLED)
    activity = judges.activity(db, judge.id, now_ns=CREATED + 60 * SECOND)
    assert activity["calls"] == 2
    assert activity["counts"][judges.SCORED] == 2
    # A day later, the limit allows calls again.
    trace(db, 100, start=CREATED + 25 * 3600 * SECOND, received=CREATED + 25 * 3600 * SECOND)
    later = runner(db, model, now=CREATED + 26 * 3600 * SECOND)
    later.run_once()
    if judges.sampled(judge.id, f"{100:032x}", 0.5):
        assert len(model.requests) == 3


def test_failures_are_recorded_and_count_as_calls() -> None:
    db = Store()
    judge = make(db, score_type=NUMERIC)
    trace(db, 1)
    trace(db, 2)
    trace(db, 3)
    model = FakeModel(
        _reply('{"score": 1.5}'),
        urllib.error.URLError("down"),
        _reply('{"score": 0.75, "explanation": "Mostly."}'),
    )
    assert runner(db, model).run_once() == 3
    found = sorted(judges.judgements(db, judge.id), key=lambda j: j.trace_id)
    assert [j.outcome for j in found] == [judges.FAILED, judges.FAILED, judges.SCORED]
    assert found[0].detail == "the score was not a number from 0 to 1"
    assert found[1].detail == "could not reach the model endpoint: URLError"
    assert db.scores(f"{3:032x}")[0].value == 0.75
    assert judges.activity(db, judge.id, now_ns=CREATED + 60 * SECOND)["calls"] == 3


def test_a_missing_key_pauses_the_judge_without_marking_traces() -> None:
    db = Store()
    judge = make(db)
    trace(db, 1)
    model = FakeModel()
    run = judges.JudgeRunner(db, post=model, env={}, clock=lambda: CREATED + 60 * SECOND)
    assert run.run_once() == 0
    assert run.problems == {judge.id: "JUDGE_API_KEY is not set"}
    assert judges.judgements(db, judge.id) == []
    assert model.requests == []


def test_disabled_and_deleted_judges_do_nothing() -> None:
    db = Store()
    judge = make(db, enabled=False)
    trace(db, 1)
    model = FakeModel()
    assert runner(db, model).run_once() == 0
    judges.update_judge(db, judge.id, {"enabled": True})
    judges.delete_judge(db, judge.id)
    assert runner(db, model).run_once() == 0
    assert model.requests == []


@pytest.mark.parametrize(
    ("text", "score_type", "expected"),
    [
        ('{"score": true}', BOOLEAN, (1.0, None)),
        ('Sure! {"score": false, "explanation": "No."} Done', BOOLEAN, (0.0, "No.")),
        ('{"score": 0}', NUMERIC, (0.0, None)),
    ],
)
def test_parse_verdict(text: str, score_type: str, expected: tuple[float, str | None]) -> None:
    assert judges.parse_verdict(text, score_type) == expected


@pytest.mark.parametrize(
    ("text", "score_type"),
    [
        ("yes", BOOLEAN),
        ('{"score": "true"}', BOOLEAN),
        ('{"score": 1}', BOOLEAN),
        ('{"score": true}', NUMERIC),
        ('{"score": NaN}', NUMERIC),
        ('{"verdict": 1}', NUMERIC),
        (None, BOOLEAN),
    ],
)
def test_unusable_verdicts_are_refused(text: Any, score_type: str) -> None:
    with pytest.raises(judges.JudgeError):
        judges.parse_verdict(text, score_type)


@pytest.mark.parametrize(
    "fields",
    [
        {"base_url": "http://models.example/v1"},  # plain http off this machine
        {"base_url": "https://user:pass@models.example/v1"},
        {"base_url": "ftp://models.example"},
        {"api_key_env": "AWS_SECRET_ACCESS_KEY"},  # not a model API key
        {"prompt": "Is {input} good?"},  # no {output}
        {"sample_rate": 0},
        {"sample_rate": True},
        {"daily_limit": 0},
        {"daily_limit": 2.5},
        {"score_type": "CATEGORICAL"},
        {"enabled": "yes"},
        {"temperature": 1},
        {"name": " "},
    ],
)
def test_bad_definitions_are_refused(fields: dict[str, Any]) -> None:
    with pytest.raises(judges.JudgeError):
        make(Store(), **fields)


def test_local_http_endpoints_and_keyless_judges_are_allowed() -> None:
    judge = make(Store(), base_url="http://127.0.0.1:11434/v1", api_key_env=None)
    assert judge.base_url == "http://127.0.0.1:11434/v1"
    assert judge.api_key_env is None


def test_long_content_is_cut() -> None:
    db = Store()
    trace_id = trace(db, 1, output="x" * (judges.MAX_CONTENT + 10))
    content = judges.trace_content(db.trace(trace_id))
    assert content is not None
    assert content[1].endswith("more characters cut]")
    assert len(content[1]) < judges.MAX_CONTENT + 100


def test_the_api_manages_judges_and_tries_one_trace() -> None:
    db = Store()
    model = FakeModel(_reply('{"score": true, "explanation": "Right."}'))
    run = runner(db, model)
    http = TestClient(server.create_app(db, allowed_hosts=("testserver",), judge_runner=run))
    created = http.post("/api/judges", json=JUDGE | {"enabled": False})
    assert created.status_code == 201
    judge = created.json()["judge"]
    assert judge["enabled"] is False
    assert judge["activity"]["calls"] == 0
    assert http.post("/api/judges", json=JUDGE).status_code == 409
    assert http.post("/api/judges", content=b"{}").status_code == 415

    path = f"/api/judges/{judge['id']}"
    assert http.patch(path, json={"name": "renamed"}).status_code == 400
    updated = http.patch(path, json={"enabled": True, "sample_rate": 0.25})
    assert updated.json()["judge"]["sample_rate"] == 0.25

    trace_id = trace(db, 1)
    tried = http.post(f"{path}/try", json={"trace_id": trace_id})
    assert tried.status_code == 200
    assert tried.json()["verdict"]["value"] == 1.0
    assert db.scores(trace_id) == []  # a trial stores nothing
    assert http.post(f"{path}/try", json={"trace_id": "f" * 32}).status_code == 404
    model.replies.append(urllib.error.HTTPError("u", 401, "no", {}, None))  # pyright: ignore[reportArgumentType]
    assert http.post(f"{path}/try", json={"trace_id": trace_id}).status_code == 502

    assert http.get(f"{path}/judgements").json() == {"judgements": []}
    assert [j["name"] for j in http.get("/api/judges").json()["judges"]] == ["helpful"]
    assert http.delete(path).status_code == 204
    assert http.get(path).status_code == 404
