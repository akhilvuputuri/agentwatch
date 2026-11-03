from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest
from starlette.testclient import TestClient

from agentwatch import drift, genai, metrics, monitors, server, store
from agentwatch.drift import Records, TraceRecord
from agentwatch.monitors import FIRING, OK, MonitorRunner
from agentwatch.scores import API, BOOLEAN, NUMERIC, Score
from agentwatch.spans import STATUS_ERROR, Attributes, Span
from agentwatch.stats import (
    CHANGED,
    NO_DIFFERENCE,
    UNDERPOWERED,
    Z_80,
    Z_95,
    Z_POWER,
    z_for,
)
from agentwatch.store import Store
from tests import drift_simulation

HOUR = 3600 * 10**9
DAY = 24 * HOUR
NOW = 20_000 * DAY
BASELINE = (NOW - 2 * DAY, NOW - DAY)
CURRENT = (NOW - DAY, NOW)
BEFORE = NOW - DAY - HOUR
AFTER = NOW - HOUR


def model_call(name: str = "m", cost: float | None = None) -> Attributes:
    attributes: Attributes = {
        genai.OPERATION: "chat",
        genai.REQUEST_MODEL: name,
        genai.INPUT_TOKENS: 10,
        genai.OUTPUT_TOKENS: 1,
    }
    if cost is not None:
        attributes[genai.COST_USD] = cost
    return attributes


TOOL: Attributes = {genai.OPERATION: "execute_tool", genai.TOOL_NAME: "t"}


class Traces:
    """Writes traces of a root span and any number of child calls, with fresh IDs."""

    def __init__(self, db: Store) -> None:
        self.db = db
        self.next = 0

    def add(
        self,
        at: int,
        *,
        ok: int = 0,
        failed: int = 0,
        attributes: Attributes | None = None,
        service: str = "app",
        duration: int = 10**6,
        resource: Attributes | None = None,
    ) -> str:
        self.next += 1
        trace_id = f"{self.next:032x}"
        shared: Attributes = {"service.name": service, **(resource or {})}
        spans = [
            Span(
                trace_id=trace_id,
                span_id=f"{1:016x}",
                parent_span_id=None,
                name="root",
                start_ns=at,
                end_ns=at + 1,
                resource=shared,
            )
        ]
        for n in range(ok + failed):
            spans.append(
                Span(
                    trace_id=trace_id,
                    span_id=f"{n + 2:016x}",
                    parent_span_id=f"{1:016x}",
                    name="call",
                    start_ns=at + n,
                    end_ns=at + n + duration,
                    status_code=STATUS_ERROR if n < failed else 0,
                    attributes=model_call() if attributes is None else attributes,
                    resource=shared,
                )
            )
        self.db.insert(spans, received_ns=at)
        return trace_id

    def many(self, count: int, at: int, **fields: Any) -> None:
        for _ in range(count):
            self.add(at, **fields)

    def score(self, name: str, value: float, *, at: int, data_type: str = NUMERIC) -> None:
        trace_id = self.add(at)
        self.db.upsert_scores(
            [
                Score(
                    id=f"s{self.next}",
                    trace_id=trace_id,
                    span_id=None,
                    name=name,
                    data_type=data_type,
                    value=value,
                    label=None,
                    source=API,
                    comment=None,
                    created_ns=at,
                )
            ]
        )


@pytest.fixture
def db() -> Store:
    return Store()


@pytest.fixture
def traces(db: Store) -> Traces:
    return Traces(db)


def compare(db: Store, metric: str, **fields: Any) -> drift.Comparison:
    return drift.compare(db, metric, baseline=BASELINE, current=CURRENT, **fields)


# --- Metrics -----------------------------------------------------------------------------


def test_error_rate_pools_failures_over_calls(db: Store, traces: Traces) -> None:
    traces.add(AFTER, ok=3, failed=1)
    traces.add(AFTER, ok=0, failed=2)
    traces.add(AFTER, ok=4, attributes=TOOL)
    traces.add(AFTER, failed=1, attributes=TOOL)
    window = metrics.Window(*CURRENT)
    found = drift.records(db, drift.MODEL_ERROR_RATE, window)
    assert sorted((r.numerator, r.denominator) for r in found.traces) == [(1, 4), (2, 2)]
    assert found.proportion
    result = compare(db, drift.MODEL_ERROR_RATE)
    # Pooled: 3 of 6, not the mean of each trace's rate (0.625).
    assert result.current_value == 0.5
    assert (result.current_traces, result.current_samples) == (2, 6)
    assert compare(db, drift.TOOL_ERROR_RATE).current_value == 0.2


def test_latency_matches_the_dashboard(db: Store, traces: Traces) -> None:
    for n in range(1, 8):
        traces.add(AFTER, ok=2, duration=n * 10**6)
    figures = metrics.monitor_figures(db, metrics.Window(*CURRENT))
    result = compare(db, drift.MODEL_P95_MS)
    assert result.current_value == pytest.approx(figures["model_p95_ms"])
    assert result.current_samples == 14
    assert compare(db, drift.MODEL_MEAN_MS).current_value == pytest.approx(4.0)


def test_cost_per_trace_matches_the_dashboard(db: Store, traces: Traces) -> None:
    traces.add(AFTER, ok=2, attributes=model_call(cost=0.5))
    traces.add(AFTER, ok=1, attributes=model_call(cost=2.0))
    # No known cost: left out, as the dashboard's mean leaves it out.
    traces.add(AFTER, ok=1)
    result = compare(db, drift.COST_PER_TRACE)
    assert result.current_value == pytest.approx(1.5)
    assert result.current_traces == 2
    overview = metrics.metrics(db, metrics.Window(*CURRENT))["overview"]
    assert overview["trace_cost_mean"] == pytest.approx(1.5)


def test_score_mean(db: Store, traces: Traces) -> None:
    traces.score("q", 0.5, at=AFTER)
    traces.score("q", 1.0, at=AFTER)
    traces.score("other", 9.0, at=AFTER)
    result = compare(db, drift.SCORE_MEAN, score_name="q")
    assert (result.current_value, result.current_traces, result.score_name) == (0.75, 2, "q")
    assert not result.proportion


def test_a_score_is_a_proportion_only_when_all_boolean(db: Store, traces: Traces) -> None:
    for at in (BEFORE, AFTER):
        traces.score("q", 1.0, at=at, data_type=BOOLEAN)
    assert compare(db, drift.SCORE_MEAN, score_name="q").proportion
    traces.score("q", 0.5, at=AFTER)
    assert not compare(db, drift.SCORE_MEAN, score_name="q").proportion


def test_service_filter(db: Store, traces: Traces) -> None:
    traces.add(AFTER, failed=1, service="front")
    traces.add(AFTER, ok=1, service="back")
    assert compare(db, drift.MODEL_ERROR_RATE, service="front").current_value == 1.0
    assert compare(db, drift.MODEL_ERROR_RATE, service="back").current_value == 0.0
    assert compare(db, drift.MODEL_ERROR_RATE).current_value == 0.5


def test_model_filter(db: Store, traces: Traces) -> None:
    traces.add(AFTER, failed=2, attributes=model_call("bad", cost=1.0))
    traces.add(AFTER, ok=2, attributes=model_call("good", cost=3.0))
    assert compare(db, drift.MODEL_ERROR_RATE, model="bad").current_value == 1.0
    assert compare(db, drift.MODEL_ERROR_RATE, model="good").current_value == 0.0
    assert compare(db, drift.MODEL_MEAN_MS, model="good").current_samples == 2
    assert compare(db, drift.MODEL_P95_MS, model="bad").current_samples == 2
    # Each trace costs 2 x its price, and the other model's trace has no cost of this one.
    result = compare(db, drift.COST_PER_TRACE, model="bad")
    assert (result.current_value, result.current_traces) == (2.0, 1)


@pytest.mark.parametrize("metric", [drift.TOOL_ERROR_RATE, drift.SCORE_MEAN])
def test_a_model_filter_is_refused_where_it_means_nothing(db: Store, metric: str) -> None:
    with pytest.raises(drift.DriftError, match="model filter"):
        compare(db, metric, model="m", score_name="q")


def test_refusals(db: Store) -> None:
    with pytest.raises(drift.DriftError, match="metric must be"):
        compare(db, "trace_count")
    with pytest.raises(drift.DriftError, match="score_name"):
        compare(db, drift.SCORE_MEAN)


def test_window_edges(db: Store, traces: Traces) -> None:
    traces.add(CURRENT[0], failed=1)
    traces.add(CURRENT[1], failed=1)
    traces.add(CURRENT[0] - 1, ok=1)
    result = compare(db, drift.MODEL_ERROR_RATE)
    assert (result.current_traces, result.baseline_traces) == (1, 1)
    assert (result.current_value, result.baseline_value) == (1.0, 0.0)


# --- Verdicts ----------------------------------------------------------------------------


def rate_records(*traces: tuple[int, int]) -> Records:
    return Records(
        tuple(TraceRecord(f"{n:032x}", f, c) for n, (f, c) in enumerate(traces)), proportion=True
    )


def result_of(baseline: Records, current: Records, **fields: Any) -> drift.Comparison:
    return drift.compare_records(drift.MODEL_ERROR_RATE, baseline, current, **fields)


@pytest.mark.parametrize(
    ("before", "after", "verdict"),
    [(9, 30, UNDERPOWERED), (30, 9, UNDERPOWERED), (10, 30, CHANGED), (30, 10, CHANGED)],
)
def test_underpowered_at_the_edge(before: int, after: int, verdict: str) -> None:
    result = result_of(rate_records(*[(0, 2)] * before), rate_records(*[(2, 2)] * after))
    assert result.verdict == verdict
    if verdict == UNDERPOWERED:
        assert result.interval is None
        assert result.detectable is None
        assert result.direction == "none"
        # The values are still reported.
        assert (result.baseline_value, result.current_value) == (0.0, 1.0)


def test_changed_and_its_direction() -> None:
    low = rate_records(*[(0, 2)] * 25, *[(1, 2)] * 5)
    high = rate_records(*[(2, 2)] * 25, *[(1, 2)] * 5)
    rose = result_of(low, high)
    assert (rose.verdict, rose.direction) == (CHANGED, "higher")
    assert rose.interval is not None
    assert rose.interval[0] > 0
    fell = result_of(high, low)
    assert (fell.verdict, fell.direction) == (CHANGED, "lower")


def test_no_difference_says_what_it_could_have_shown() -> None:
    same = rate_records(*[(1, 2)] * 15, *[(0, 2)] * 15)
    result = result_of(same, same)
    assert (result.verdict, result.direction) == (NO_DIFFERENCE, "none")
    assert result.interval is not None
    half = (result.interval[1] - result.interval[0]) / 2
    # A true change of the half-width clears the interval only half the time; 80% power takes more.
    assert result.detectable == pytest.approx(half * Z_POWER / Z_95)
    wider = result_of(same, same, confidence=0.99)
    assert wider.interval is not None
    half = (wider.interval[1] - wider.interval[0]) / 2
    assert wider.detectable == pytest.approx(half * (z_for(0.99) + Z_80) / z_for(0.99))


def test_two_clean_windows_do_not_claim_certainty() -> None:
    # Every resample of no failures is no failures; the interval must still have width.
    clean = rate_records(*[(0, 2)] * 20)
    result = result_of(clean, clean)
    assert result.verdict == NO_DIFFERENCE
    assert result.detectable is not None
    assert result.detectable > 0.1


@pytest.mark.parametrize("metric", [drift.COST_PER_TRACE, drift.MODEL_MEAN_MS])
def test_no_spread_cannot_say_what_would_show(metric: str) -> None:
    alike = Records(tuple(TraceRecord(f"{n:032x}", 2.0, 1) for n in range(12)))
    varied = Records(tuple(TraceRecord(f"{n:032x}", float(n), 1) for n in range(12)))
    for baseline, current in ((alike, alike), (alike, varied), (varied, alike)):
        result = drift.compare_records(metric, baseline, current)
        assert result.detectable is None
    assert drift.compare_records(metric, varied, varied).detectable is not None


def test_no_spread_in_a_percentile_cannot_say() -> None:
    alike = Records(tuple(TraceRecord(f"{n:032x}", 0.0, 2, (5.0, 5.0)) for n in range(12)))
    result = drift.compare_records(drift.MODEL_P95_MS, alike, alike)
    assert result.verdict == NO_DIFFERENCE
    assert result.detectable is None


def test_one_trace_of_many_failures_is_not_a_change() -> None:
    baseline = rate_records(*[(0, 5)] * 20)
    bad_trace = rate_records(*[(0, 1)] * 19, (100, 100))
    result = result_of(baseline, bad_trace)
    # 100 failures in 119 calls, and all of it from one trace.
    assert result.current_value == pytest.approx(100 / 119)
    assert result.verdict == NO_DIFFERENCE
    # Treated as independent calls, the same failures look decisive.
    independent = rate_records(*[(0, 1)] * 19, *[(1, 1)] * 100)
    assert result_of(rate_records(*[(0, 1)] * 100), independent).verdict == CHANGED


def test_the_same_seed_and_data_give_the_same_result(db: Store, traces: Traces) -> None:
    for n in range(15):
        traces.add(BEFORE, ok=3, failed=n % 2)
        traces.add(AFTER, ok=2, failed=n % 3)
    first = compare(db, drift.MODEL_ERROR_RATE)
    assert compare(db, drift.MODEL_ERROR_RATE) == first
    assert first.interval != compare(db, drift.MODEL_ERROR_RATE, seed=8).interval


def test_truncation_keeps_the_most_recent_traces(
    db: Store, traces: Traces, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(drift, "MAX_TRACES", 3)
    for n in range(3):
        traces.add(AFTER - n - 10, ok=1)
    assert not compare(db, drift.MODEL_ERROR_RATE).truncated
    traces.add(AFTER - 100, failed=1)
    result = compare(db, drift.MODEL_ERROR_RATE)
    assert result.truncated
    # The oldest trace, with the only failure, is the one left out.
    assert (result.current_traces, result.current_value) == (3, 0.0)


def test_percentile_windows_are_bounded_by_samples(
    db: Store, traces: Traces, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(drift, "MAX_QUANTILE_SAMPLES", 5)
    for n in range(4):
        traces.add(AFTER - n, ok=2)
    result = compare(db, drift.MODEL_P95_MS)
    assert (result.truncated, result.current_traces, result.current_samples) == (True, 2, 4)


# --- API ---------------------------------------------------------------------------------


@pytest.fixture
def client(db: Store) -> TestClient:
    return TestClient(server.create_app(db, ui_dir=None), base_url="http://127.0.0.1")


def query(**fields: Any) -> dict[str, Any]:
    return {
        "metric": drift.MODEL_ERROR_RATE,
        "baseline_from": BASELINE[0],
        "baseline_to": BASELINE[1],
        "current_from": CURRENT[0],
        "current_to": CURRENT[1],
    } | fields


def test_api_compares_one_metric(client: TestClient, traces: Traces) -> None:
    traces.many(12, BEFORE, ok=2)
    traces.many(12, AFTER, failed=2)
    reply = client.get("/api/drift", params=query())
    assert reply.status_code == 200
    comparison = reply.json()["comparison"]
    assert comparison["verdict"] == "changed"
    assert comparison["direction"] == "higher"
    assert comparison["baseline_traces"] == 12
    assert comparison["proportion"] is True
    assert len(comparison["interval"]) == 2


@pytest.mark.parametrize(
    "fields",
    [
        {"metric": "trace_count"},
        {"metric": ""},
        {"metric": drift.SCORE_MEAN},
        {"metric": drift.TOOL_ERROR_RATE, "model": "m"},
        {"baseline_from": "soon"},
        {"current_to": ""},
        {"current_to": "9" * 20},
        {"baseline_from": BASELINE[1]},
        {"current_from": CURRENT[1]},
        # Overlapping windows are not two samples.
        {"current_from": BASELINE[1] - 1},
        {"current_to": str(2**63)},
    ],
)
def test_api_refuses(client: TestClient, fields: dict[str, Any]) -> None:
    assert client.get("/api/drift", params=query(**fields)).status_code == 400


def test_summary_covers_every_call_metric_and_score(client: TestClient, traces: Traces) -> None:
    traces.score("helpful", 1.0, at=time_now_minus_days(1))
    reply = client.get("/api/drift/summary", params={"days": 7})
    assert reply.status_code == 200
    body = reply.json()
    assert body["days"] == 7
    assert isinstance(body["end_ns"], str)
    assert [c["metric"] for c in body["comparisons"]] == [*drift.CALL_METRICS, drift.SCORE_MEAN]
    assert body["comparisons"][-1]["score_name"] == "helpful"
    assert {c["verdict"] for c in body["comparisons"]} == {"underpowered"}
    for days in ("0", "366", "x", "-1"):
        assert client.get("/api/drift/summary", params={"days": days}).status_code == 400


def time_now_minus_days(days: int) -> int:
    return time.time_ns() - days * DAY


def test_the_single_metric_api_stays_at_95(client: TestClient, traces: Traces) -> None:
    traces.many(12, BEFORE, ok=2)
    traces.many(12, AFTER, failed=2)
    assert client.get("/api/drift", params=query()).json()["comparison"]["confidence"] == 0.95


def test_the_summary_corrects_for_how_many_it_compares(db: Store, traces: Traces) -> None:
    traces.score("first", 1.0, at=NOW - DAY)
    traces.score("second", 1.0, at=NOW - DAY)
    found = drift.summary(db, service=None, days=7, now_ns=NOW)
    assert len(found) == 7
    assert all(c.confidence == pytest.approx(1 - 0.05 / 7) for c in found)


def test_the_summary_takes_two_windows_with_scores_and_service(
    client: TestClient, traces: Traces
) -> None:
    traces.add(AFTER, ok=1, service="other")
    traces.score("helpful", 1.0, at=AFTER)
    window = {k: v for k, v in query().items() if k != "metric"}
    body = client.get("/api/drift/summary", params=window).json()
    assert body["days"] is None
    assert body["confidence"] == pytest.approx(1 - 0.05 / 6)
    assert [c["metric"] for c in body["comparisons"]][-1] == drift.SCORE_MEAN
    other = client.get("/api/drift/summary", params=window | {"service": "other"}).json()
    # A trace of the other service, and the score is not on one.
    assert [c["metric"] for c in other["comparisons"]] == list(drift.CALL_METRICS)
    assert client.get("/api/drift/summary", params=window | {"current_to": "x"}).status_code == 400


def test_one_comparison_runs_at_a_time(
    client: TestClient, traces: Traces, monkeypatch: pytest.MonkeyPatch
) -> None:
    traces.add(AFTER, ok=1)
    started, finish = threading.Event(), threading.Event()
    real = drift.compare
    replies: list[int] = []

    def slow(*args: Any, **kwargs: Any) -> Any:
        started.set()
        finish.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(drift, "compare", slow)

    def first() -> None:
        replies.append(client.get("/api/drift", params=query()).status_code)

    thread = threading.Thread(target=first)
    thread.start()
    assert started.wait(10)
    for path, params in (
        ("/api/drift", query(metric=drift.MODEL_MEAN_MS)),
        ("/api/drift/summary", {"days": 7}),
    ):
        busy = client.get(path, params=params)
        assert busy.status_code == 429
        assert busy.headers["retry-after"] == str(server.DRIFT_RETRY_AFTER_S)
    finish.set()
    thread.join(10)
    assert replies == [200]
    # Free again, and a refused request was not remembered.
    assert client.get("/api/drift", params=query(metric=drift.MODEL_MEAN_MS)).status_code == 200


def test_a_recent_identical_request_is_served_from_the_cache(
    client: TestClient, traces: Traces, monkeypatch: pytest.MonkeyPatch
) -> None:
    traces.add(AFTER, ok=1)
    calls: list[str] = []
    real = drift.compare

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(args[1])
        return real(*args, **kwargs)

    monkeypatch.setattr(drift, "compare", counted)
    first = client.get("/api/drift", params=query())
    # The same request, with an empty filter that means no filter.
    again = client.get("/api/drift", params=query(service=""))
    assert first.json() == again.json()
    assert len(calls) == 1
    client.get("/api/drift", params=query(service="other"))
    assert len(calls) == 2
    # An error is not kept, and costs the gate nothing.
    assert client.get("/api/drift", params=query(metric="x")).status_code == 400
    assert client.get("/api/drift", params=query()).status_code == 200
    monkeypatch.setattr(server, "DRIFT_CACHE_S", -1.0)
    client.get("/api/drift", params=query(model="m"))
    client.get("/api/drift", params=query(model="m"))
    assert len(calls) == 5


def test_summary_compares_the_last_days_with_those_before(db: Store, traces: Traces) -> None:
    traces.many(12, NOW - 10 * DAY, ok=2)
    traces.many(12, NOW - 2 * DAY, failed=2)
    found = drift.summary(db, service=None, days=7, now_ns=NOW)
    assert found[0].metric == drift.MODEL_ERROR_RATE
    assert (found[0].baseline_value, found[0].current_value) == (0.0, 1.0)
    assert found[0].verdict == CHANGED


def test_changes_list_first_appearances_newest_first(client: TestClient, traces: Traces) -> None:
    now = time_now_minus_days(0)
    old, recent = now - 30 * DAY, now - DAY
    version: dict[str, Any] = {"service.version": "1.0"}
    traces.add(old, ok=1, attributes=model_call("a"), resource=version)
    traces.add(recent, ok=1, attributes=model_call("a"), resource=version)
    traces.add(recent + 5, ok=1, attributes=model_call("b"), resource={"service.version": "2.0"})
    traces.add(recent + 9, ok=1, attributes=model_call("c"), service="other")
    changes = client.get("/api/changes").json()["changes"]
    assert [(c["kind"], c["name"]) for c in changes] == [
        ("model", "c"),
        ("model", "b"),
        ("version", "2.0"),
        ("model", "a"),
        ("version", "1.0"),
    ]
    # First seen is the earliest, not the latest, and a string.
    assert changes[-1]["first_seen_ns"] == str(old)
    assert changes[2]["service"] == "app"
    assert changes[0]["service"] is None
    # A first appearance before the period stays out, though it was seen again within it.
    recently = client.get("/api/changes", params={"days": 7}).json()["changes"]
    assert [c["name"] for c in recently] == ["c", "b", "2.0"]
    scoped = client.get("/api/changes", params={"service": "other"}).json()["changes"]
    assert [c["name"] for c in scoped] == ["c"]
    assert client.get("/api/changes", params={"days": "x"}).status_code == 400


# --- Drift monitors ----------------------------------------------------------------------


def make(db: Store, **fields: Any) -> monitors.Monitor:
    body = {
        "name": "drifting",
        "mode": "drift",
        "metric": drift.MODEL_ERROR_RATE,
        "window_minutes": 24 * 60,
        "comparison": "above",
        "threshold": 0.1,
        "cooldown_minutes": 60,
    }
    return monitors.create_monitor(db, body | fields, now_ns=NOW)


def comparison(
    verdict: str = CHANGED,
    direction: str = "higher",
    difference: float = 0.3,
) -> drift.Comparison:
    return drift.Comparison(
        metric=drift.MODEL_ERROR_RATE,
        score_name=None,
        baseline_value=0.1,
        current_value=0.1 + difference,
        difference=difference,
        interval=(difference - 0.1, difference + 0.1),
        baseline_traces=20,
        current_traces=20,
        baseline_samples=40,
        current_samples=40,
        direction=direction,
        verdict=verdict,
        detectable=0.1,
        truncated=False,
        proportion=True,
        confidence=0.995,
    )


def test_a_drift_monitors_cooldown_defaults_to_its_window(db: Store) -> None:
    body = {
        "name": "defaults",
        "mode": "drift",
        "metric": drift.MODEL_ERROR_RATE,
        "window_minutes": 500,
        "comparison": "above",
        "threshold": 0.1,
    }
    assert monitors.create_monitor(db, body).cooldown_minutes == 500
    given = monitors.create_monitor(db, body | {"name": "given", "cooldown_minutes": 7})
    assert given.cooldown_minutes == 7
    level = monitors.create_monitor(db, body | {"name": "level", "mode": "threshold"})
    assert level.cooldown_minutes == monitors.DEFAULT_COOLDOWN_MINUTES
    with pytest.raises(monitors.MonitorError, match="window_minutes"):
        monitors.create_monitor(db, {k: v for k, v in body.items() if k != "window_minutes"})


def test_a_drift_monitor_defaults_and_refusals(db: Store) -> None:
    monitor = make(db)
    assert monitor.baseline_minutes == monitor.window_minutes
    assert make(db, name="b", baseline_minutes=30).baseline_minutes == 30
    for fields, message in (
        ({"metric": "trace_count"}, "metric must be"),
        ({"metric": "cost_usd"}, "metric must be"),
        ({"threshold": -0.1}, "at least 0|from 0 to 1"),
        ({"threshold": 2}, "0.05 means 5%"),
        ({"baseline_minutes": 0}, "baseline_minutes"),
        ({"mode": "other"}, "mode must be"),
    ):
        with pytest.raises(monitors.MonitorError, match=message):
            make(db, name="bad", **fields)
    with pytest.raises(monitors.MonitorError, match="metric must be"):
        make(db, name="bad", mode="threshold", metric=drift.COST_PER_TRACE)
    negative = make(db, name="cost", metric=drift.COST_PER_TRACE, threshold=5)
    assert negative.threshold == 5.0
    with pytest.raises(monitors.MonitorError, match="at least 0"):
        make(db, name="bad", metric=drift.COST_PER_TRACE, threshold=-1)


def test_only_the_bad_direction_at_the_smallest_change_breaches(db: Store) -> None:
    above = make(db)
    below = make(db, name="below", comparison="below")
    assert monitors.drift_breach(above, comparison())
    assert not monitors.drift_breach(above, comparison(direction="lower", difference=-0.3))
    assert monitors.drift_breach(below, comparison(direction="lower", difference=-0.3))
    assert not monitors.drift_breach(below, comparison())
    assert monitors.drift_breach(above, comparison(difference=0.1))
    assert not monitors.drift_breach(above, comparison(difference=0.09))
    assert not monitors.drift_breach(above, comparison(verdict=NO_DIFFERENCE, direction="none"))


def step(
    monitor: monitors.Monitor,
    result: drift.Comparison,
    state: str,
    breaches: int = 0,
    good_since: int | None = None,
    last: int | None = None,
    now: int = NOW,
) -> tuple[str, int, int | None]:
    return monitors.next_drift_state(monitor, result, state, breaches, good_since, last, now)


def test_it_fires_only_on_consecutive_breaches(db: Store) -> None:
    monitor = make(db)
    breaches = 0
    for _ in range(monitors.DRIFT_BREACHES - 1):
        state, breaches, _ = step(monitor, comparison(), OK, breaches)
        assert state == OK
    # One clean evaluation starts the count again.
    assert step(monitor, comparison(NO_DIFFERENCE, "none"), OK, breaches)[:2] == (OK, 0)
    assert step(monitor, comparison(), OK, breaches)[:2] == (FIRING, monitors.DRIFT_BREACHES)


def test_a_long_gap_ends_a_run_of_breaches(db: Store) -> None:
    monitor = make(db)
    limit = monitors.DRIFT_MAX_GAP_NS
    almost = monitors.DRIFT_BREACHES - 1
    assert step(monitor, comparison(), OK, almost, None, NOW - limit)[:2] == (
        FIRING,
        monitors.DRIFT_BREACHES,
    )
    assert step(monitor, comparison(), OK, almost, None, NOW - limit - 1)[:2] == (OK, 1)


def test_underpowered_neither_fires_nor_recovers(db: Store) -> None:
    monitor = make(db)
    thin = comparison(UNDERPOWERED, "none")
    # It does not fire, and it ends a run, or breaches either side of it could combine.
    assert step(monitor, thin, OK, 5) == (OK, 0, None)
    # While firing it adds no good time: good-since moves along with the clock.
    assert step(monitor, thin, FIRING, 6, NOW - 10, NOW - HOUR) == (
        FIRING,
        6,
        NOW - 10 + HOUR,
    )


def test_recovery_uses_the_cooldown(db: Store) -> None:
    monitor = make(db)
    quiet = comparison(NO_DIFFERENCE, "none")
    assert step(monitor, quiet, FIRING, 6) == (FIRING, 6, NOW)
    minutes = monitors.MINUTE_NS
    assert step(monitor, quiet, FIRING, 6, NOW - 59 * minutes)[0] == FIRING
    assert step(monitor, quiet, FIRING, 6, NOW - 60 * minutes) == (OK, 0, None)
    # A new breach starts the cooldown again.
    assert step(monitor, comparison(), FIRING, 6, NOW - 59 * minutes) == (FIRING, 6, None)


def shifted(traces: Traces) -> None:
    traces.many(15, BEFORE, ok=2)
    traces.many(15, AFTER, failed=2)


def test_hourly_cadence_and_the_alert(db: Store, traces: Traces) -> None:
    traces.many(15, BEFORE - 12 * HOUR, ok=2)
    traces.many(15, AFTER - 12 * HOUR, failed=2)
    monitor = make(db)
    clock = [NOW - 12 * HOUR + HOUR]
    runner = MonitorRunner(db, clock=lambda: clock[0])
    assert runner.run_once() == 1
    first = monitors.get_state(db, monitor.id)
    assert first.breaches == 1
    assert first.baseline_value == 0.0
    assert first.value == 1.0
    # Not due: nothing is compared or recorded, however often the runner passes.
    clock[0] += HOUR - 1
    assert runner.run_once() == 0
    assert monitors.get_state(db, monitor.id) == first
    clock[0] = first.evaluated_ns or 0
    for _ in range(monitors.DRIFT_BREACHES - 1):
        clock[0] += HOUR
        assert runner.run_once() == 1
    state = monitors.get_state(db, monitor.id)
    assert state.state == FIRING
    [alert] = monitors.list_alerts(db)
    assert (alert.kind, alert.mode, alert.metric) == ("firing", "drift", drift.MODEL_ERROR_RATE)
    assert (alert.baseline_value, alert.value) == (0.0, 1.0)
    # The bound is the interval's edge nearest no change.
    assert alert.bound is not None
    assert 0 < alert.bound < 1
    assert alert.samples == 30
    assert alert.baseline_minutes == monitor.baseline_minutes
    assert alert.muted is False


def test_a_resolved_drift_alert_shows_the_windows_agree_at_the_new_level(
    db: Store, traces: Traces
) -> None:
    monitor = make(db, cooldown_minutes=0)
    traces.many(15, NOW - 2 * DAY - 12 * HOUR, ok=2)
    traces.many(15, NOW - 2 * DAY + 10 * HOUR, failed=2)
    for hours in range(monitors.DRIFT_BREACHES):
        monitors.evaluate(db, monitor, NOW - DAY + hours * HOUR)
    assert monitors.get_state(db, monitor.id).state == FIRING
    # A day on, the same high failure rate fills both windows.
    traces.many(15, NOW - HOUR, failed=2)
    monitors.evaluate(db, monitor, NOW)
    [resolved, fired] = monitors.list_alerts(db)
    assert (fired.baseline_value, fired.value) == (0.0, 1.0)
    assert resolved.kind == "resolved"
    assert resolved.reason is None
    # The level did not recover: both windows are bad, and a client can see that.
    assert (resolved.baseline_value, resolved.value) == (1.0, 1.0)
    assert resolved.baseline_minutes == monitor.baseline_minutes


def test_a_muted_drift_monitor_is_delivered_at_once(db: Store, traces: Traces) -> None:
    shifted(traces)
    monitor = make(db, muted=True)
    for hours in range(monitors.DRIFT_BREACHES):
        monitors.evaluate(db, monitor, NOW + hours * HOUR)
    [alert] = monitors.list_alerts(db)
    assert alert.muted
    assert alert.delivered_ns == alert.created_ns
    assert monitors.list_alerts(db, undelivered=True) == []


def test_underpowered_evaluation_is_recorded_as_insufficient(db: Store, traces: Traces) -> None:
    traces.many(9, BEFORE, ok=2)
    traces.many(15, AFTER, failed=2)
    monitor = make(db)
    state = monitors.evaluate(db, monitor, NOW)
    assert state is not None
    assert (state.state, state.insufficient, state.breaches, state.bound) == (OK, True, 0, None)
    assert monitors.list_alerts(db) == []


def test_the_bootstrap_holds_no_lock(
    db: Store, traces: Traces, monkeypatch: pytest.MonkeyPatch
) -> None:
    shifted(traces)
    seen: list[bool] = []
    real = drift.unpaired_cluster_bootstrap

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(db._lock.locked())  # pyright: ignore[reportPrivateUsage]
        return real(*args, **kwargs)

    monkeypatch.setattr(drift, "unpaired_cluster_bootstrap", spy)
    monitors.evaluate(db, make(db), NOW)
    assert seen == [False]


def test_a_rule_change_while_firing_resolves_with_the_mode(db: Store, traces: Traces) -> None:
    shifted(traces)
    monitor = make(db, cooldown_minutes=0)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO monitor_states (monitor_id, state, samples, evaluated_ns, since_ns, "
            "insufficient) VALUES (?, 'FIRING', 1, 0, 0, false)",
            [monitor.id],
        )
    monitors.update_monitor(db, monitor.id, {"threshold": 0.2}, now_ns=NOW)
    [alert] = monitors.list_alerts(db)
    assert (alert.kind, alert.reason, alert.mode, alert.value) == (
        "resolved",
        "changed",
        "drift",
        None,
    )
    assert alert.baseline_value is None


def test_threshold_monitors_are_unchanged(db: Store, traces: Traces) -> None:
    traces.many(20, AFTER, failed=1)
    monitor = monitors.create_monitor(
        db,
        {
            "name": "level",
            "metric": monitors.MODEL_ERROR_RATE,
            "window_minutes": 60 * 2,
            "comparison": "above",
            "threshold": 0.2,
            "cooldown_minutes": 0,
        },
    )
    assert (monitor.mode, monitor.baseline_minutes) == ("threshold", None)
    state = monitors.evaluate(db, monitor, NOW)
    assert state is not None
    assert state.state == FIRING
    assert (state.baseline_value, state.breaches) == (None, 0)
    # Evaluated again at once: threshold monitors have no hourly limit.
    assert monitors.evaluate(db, monitor, NOW + 1) is not None
    [alert] = monitors.list_alerts(db)
    assert (alert.mode, alert.baseline_value, alert.baseline_minutes) == ("threshold", None, None)
    assert alert.muted is False
    assert alert.delivered_ns is None
    assert (alert.value, alert.samples) == (1.0, 20)
    assert alert.bound is not None
    assert alert.bound > 0.8


def test_migration_from_schema_9(tmp_path: Path) -> None:
    path = tmp_path / "old.duckdb"
    Store(path).close()
    # Back to how version 9 left it, with rows in each table.
    conn = duckdb.connect(str(path))
    conn.execute("DROP TABLE memory_records")
    for table, column in (
        ("monitors", "mode"),
        ("monitors", "baseline_minutes"),
        ("monitors", "muted"),
        ("monitor_states", "alert_muted"),
        ("monitor_states", "baseline_value"),
        ("monitor_states", "breaches"),
        ("alerts", "mode"),
        ("alerts", "baseline_value"),
        ("alerts", "baseline_minutes"),
        ("alerts", "muted"),
    ):
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    conn.execute(
        "INSERT INTO monitors (id, name, metric, window_minutes, comparison, threshold, "
        "min_samples, cooldown_minutes, enabled, created_ns) "
        "VALUES ('m', 'old', 'cost_usd', 5, 'above', 1, 10, 60, true, 1)"
    )
    conn.execute(
        "INSERT INTO monitor_states (monitor_id, state, samples, evaluated_ns, since_ns, "
        "insufficient) VALUES ('m', 'FIRING', 1, 2, 2, false)"
    )
    conn.execute(
        "INSERT INTO alerts (id, monitor_id, monitor_name, kind, metric, comparison, threshold, "
        "samples, window_minutes, created_ns) "
        "VALUES ('a', 'm', 'old', 'firing', 'cost_usd', 'above', 1, 1, 5, 2)"
    )
    conn.execute("UPDATE meta SET value = '9' WHERE key = 'schema'")
    conn.close()

    upgraded = Store(path)
    assert upgraded.query_one("SELECT value FROM meta WHERE key = 'schema'")[0] == str(
        store.SCHEMA_VERSION
    )
    assert store.SCHEMA_VERSION == 11
    [monitor] = monitors.list_monitors(upgraded)
    assert (monitor.mode, monitor.baseline_minutes, monitor.muted) == ("threshold", None, False)
    state = monitors.get_state(upgraded, "m")
    assert (state.state, state.baseline_value, state.breaches) == (FIRING, None, 0)
    [alert] = monitors.list_alerts(upgraded)
    assert (alert.mode, alert.baseline_value, alert.baseline_minutes) == ("threshold", None, None)
    assert alert.muted is False
    upgraded.close()


# --- Simulations -------------------------------------------------------------------------


def test_no_real_change_rarely_fires() -> None:
    """A regression pin on seeded months of the easiest traffic. It is not a rate: harder traffic
    fires far more often, and docs/design.md has the larger runs of each model."""
    fired = sum(
        drift_simulation.false_alarms(seed, 3, drift_simulation.INDEPENDENT) for seed in (0, 1)
    )
    assert fired <= 1


def test_a_real_shift_fires_within_days() -> None:
    delay = drift_simulation.detection_delay(1000, 3, drift_simulation.INDEPENDENT)
    assert delay is not None
    assert delay <= 96


@pytest.mark.parametrize("model", drift_simulation.MODELS)
def test_each_traffic_model_keeps_about_the_same_base_rate(model: str) -> None:
    traffic = drift_simulation.Traffic(seed=3, days=400, model=model)
    _, found = drift_simulation.generate(traffic)
    failures = sum(r.numerator for r in found)
    calls = sum(r.denominator for r in found)
    # Incidents add to the base rate by design; everything else averages out to it.
    expected = (0.08, 0.2) if model == drift_simulation.INCIDENTS else (0.08, 0.125)
    assert expected[0] < failures / calls < expected[1]
    assert 5 * 400 <= len(found) <= 20 * 400


def test_one_shift_fires_once_with_the_window_as_cooldown() -> None:
    traffic = drift_simulation.Traffic(seed=1000, days=7 + 6, shifted_rate=0.6, shift_day=7)
    fired = drift_simulation.firings(traffic, drift_simulation.monitor(3), first_day=7)
    assert len(fired) == 1


def _monitor() -> monitors.Monitor:
    return monitors.Monitor(
        id="m",
        name="m",
        metric=drift.TOOL_ERROR_RATE,
        score_name=None,
        service=None,
        window_minutes=7 * 24 * 60,
        comparison=monitors.ABOVE,
        threshold=0.0,
        min_samples=1,
        cooldown_minutes=7 * 24 * 60,
        enabled=True,
        created_ns=0,
        mode=monitors.DRIFT,
        baseline_minutes=7 * 24 * 60,
    )


def test_time_without_evaluations_does_not_count_toward_the_cooldown() -> None:
    monitor = _monitor()
    good = _no_change()
    hour = monitors.DRIFT_INTERVAL_NS
    start = 1_000 * hour
    # Good for an hour, then down for 8 days: one good reading afterwards must not resolve it.
    state, _, good_since = monitors.next_drift_state(
        monitor, good, monitors.FIRING, 0, start, start, start + hour
    )
    later = start + hour + 8 * 24 * hour
    state, _, good_since = monitors.next_drift_state(
        monitor, good, state, 0, good_since, start + hour, later
    )
    assert state == monitors.FIRING
    assert good_since == later - hour


def test_a_gap_before_an_underpowered_reading_is_not_counted_twice() -> None:
    monitor = _monitor()
    hour = monitors.DRIFT_INTERVAL_NS
    start = 1_000 * hour
    later = start + 48 * hour
    _, _, good_since = monitors.next_drift_state(
        monitor,
        replace(_no_change(), verdict=drift.UNDERPOWERED),
        monitors.FIRING,
        0,
        start - hour,
        start,
        later,
    )
    assert good_since == later - hour


def _no_change() -> drift.Comparison:
    return drift.Comparison(
        metric=drift.TOOL_ERROR_RATE,
        score_name=None,
        baseline_value=0.1,
        current_value=0.1,
        difference=0.0,
        interval=(-0.1, 0.1),
        baseline_traces=50,
        current_traces=50,
        baseline_samples=50,
        current_samples=50,
        direction="none",
        verdict=drift.NO_DIFFERENCE,
        detectable=0.14,
        truncated=False,
        proportion=True,
        confidence=0.995,
    )


def test_switching_a_monitor_to_drift_takes_the_longer_cooldown(db: Store) -> None:
    monitor = monitors.create_monitor(
        db,
        {
            "name": "m",
            "metric": drift.TOOL_ERROR_RATE,
            "window_minutes": 10_080,
            "comparison": monitors.ABOVE,
            "threshold": 0.1,
        },
    )
    assert monitor.cooldown_minutes == monitors.DEFAULT_COOLDOWN_MINUTES
    changed = monitors.update_monitor(db, monitor.id, {"mode": monitors.DRIFT, "threshold": 0})
    assert changed.cooldown_minutes == 10_080
