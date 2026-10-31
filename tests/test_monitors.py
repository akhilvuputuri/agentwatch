from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import duckdb
import pytest
from starlette.testclient import TestClient

from agentwatch import genai, metrics, monitors, server, store
from agentwatch.monitors import FIRING, OK, MonitorRunner
from agentwatch.retention import DAY_NS
from agentwatch.scores import API, BOOLEAN, CATEGORICAL, NUMERIC, Score
from agentwatch.spans import STATUS_ERROR, Attributes, Span
from agentwatch.stats import wilson
from agentwatch.store import Store

MINUTE = monitors.MINUTE_NS
NOW = 20_000 * DAY_NS
# Inside any window of ten minutes or more.
RECENT = NOW - 5 * MINUTE

MODEL: Attributes = {
    genai.OPERATION: "chat",
    genai.REQUEST_MODEL: "m",
    genai.INPUT_TOKENS: 10,
    genai.OUTPUT_TOKENS: 1,
}
TOOL: Attributes = {genai.OPERATION: "execute_tool", genai.TOOL_NAME: "t"}


class Traces:
    """Writes one-trace-per-call spans with fresh IDs."""

    def __init__(self, db: Store) -> None:
        self.db = db
        self.next = 0

    def add(
        self,
        attributes: Attributes,
        *,
        start: int = RECENT,
        error: bool = False,
        service: str = "app",
        duration: int = 1_000_000,
    ) -> str:
        self.next += 1
        trace_id = f"{self.next:032x}"
        self.db.insert(
            [
                Span(
                    trace_id=trace_id,
                    span_id="1" * 16,
                    parent_span_id=None,
                    name="span",
                    start_ns=start,
                    end_ns=start + duration,
                    status_code=STATUS_ERROR if error else 0,
                    attributes=attributes,
                    resource={"service.name": service},
                )
            ],
            received_ns=start,
        )
        return trace_id

    def calls(self, attributes: Attributes, failed: int, total: int, **fields: Any) -> None:
        for n in range(total):
            self.add(attributes, error=n < failed, **fields)

    def score(
        self,
        name: str,
        value: float | None,
        *,
        data_type: str = NUMERIC,
        created: int = RECENT,
        service: str = "app",
    ) -> None:
        trace_id = self.add({}, service=service)
        self.db.upsert_scores(
            [
                Score(
                    id=f"s{self.next}",
                    trace_id=trace_id,
                    span_id=None,
                    name=name,
                    data_type=data_type,
                    value=value,
                    label=None if value is not None else "label",
                    source=API,
                    comment=None,
                    created_ns=created,
                )
            ]
        )


@pytest.fixture
def db() -> Store:
    return Store()


@pytest.fixture
def traces(db: Store) -> Traces:
    return Traces(db)


def make(db: Store, metric: str = monitors.MODEL_ERROR_RATE, **fields: Any) -> monitors.Monitor:
    body = {
        "name": fields.pop("name", "watch"),
        "metric": metric,
        "window_minutes": 10,
        "comparison": "above",
        "threshold": 0.2,
        "min_samples": 1,
    }
    return monitors.create_monitor(db, body | fields, now_ns=NOW)


def alerts(db: Store) -> list[monitors.Alert]:
    return list(reversed(monitors.list_alerts(db, limit=500)))


# --- Metrics -----------------------------------------------------------------------------


def test_each_metric_value_and_samples(db: Store, traces: Traces) -> None:
    traces.calls({**MODEL, "agentwatch.usage.cost_usd": 0.5}, failed=1, total=4)
    traces.calls(TOOL, failed=2, total=5)
    assert_reading(db, monitors.MODEL_ERROR_RATE, 0.25, 4)
    assert_reading(db, monitors.TOOL_ERROR_RATE, 0.4, 5)
    assert_reading(db, monitors.COST_USD, 2.0, 4)
    assert_reading(db, monitors.TRACE_COUNT, 9.0, 1)
    p95 = monitors.read(db, make(db, monitors.MODEL_P95_MS, name="p95"), NOW)
    assert (p95.value, p95.samples, p95.bound) == (1.0, 4, None)


def assert_reading(db: Store, metric: str, value: float, samples: int) -> None:
    reading = monitors.read(db, make(db, metric, name=metric), NOW)
    assert (reading.value, reading.samples) == (pytest.approx(value), samples)


def test_figures_match_the_dashboard(db: Store, traces: Traces) -> None:
    traces.calls({**MODEL, "agentwatch.usage.cost_usd": 0.25}, failed=1, total=3, duration=7000)
    traces.calls(TOOL, failed=1, total=2)
    traces.add(MODEL, start=NOW - 11 * MINUTE, error=True)
    window = metrics.Window(NOW - 10 * MINUTE, NOW)
    overview = metrics.metrics(db, window)["overview"]
    figures = metrics.monitor_figures(db, window)
    for key, value in figures.items():
        assert overview[key] == value, key


def test_a_score_mean_of_mixed_types_is_not_a_rate(db: Store, traces: Traces) -> None:
    traces.score("q", 1.0, data_type=BOOLEAN)
    traces.score("q", 0.5)
    traces.score("q", None, data_type=CATEGORICAL)
    traces.score("other", 9.0)
    reading = monitors.read(db, make(db, monitors.SCORE_MEAN, score_name="q"), NOW)
    assert (reading.value, reading.samples, reading.bound) == (0.75, 2, None)


def test_a_score_mean_of_booleans_is_a_rate(db: Store, traces: Traces) -> None:
    for value in (1.0, 1.0, 0.0, 0.0):
        traces.score("q", value, data_type=BOOLEAN)
    reading = monitors.read(db, make(db, monitors.SCORE_MEAN, score_name="q"), NOW)
    assert (reading.value, reading.samples) == (0.5, 4)
    interval = wilson(2, 4)
    assert interval is not None
    assert reading.bound == pytest.approx(interval[0])


def test_the_service_filter_applies_to_every_metric(db: Store, traces: Traces) -> None:
    traces.calls(MODEL, failed=1, total=2, service="front")
    traces.calls(MODEL, failed=0, total=3, service="back")
    traces.score("q", 1.0, service="front")
    traces.score("q", 0.0, service="back")
    front = make(db, service="front")
    assert monitors.read(db, front, NOW).value == 0.5
    count = make(db, monitors.TRACE_COUNT, name="count", service="back")
    assert monitors.read(db, count, NOW).value == 4.0
    score = make(db, monitors.SCORE_MEAN, name="score", score_name="q", service="back")
    assert monitors.read(db, score, NOW).value == 0.0


def test_window_edges(db: Store, traces: Traces) -> None:
    # The window includes its start and excludes its end.
    traces.add(MODEL, start=NOW - 10 * MINUTE)
    traces.add(MODEL, start=NOW - 10 * MINUTE - 1)
    traces.add(MODEL, start=NOW)
    traces.score("q", 1.0, created=NOW - 10 * MINUTE)
    traces.score("q", 5.0, created=NOW - 10 * MINUTE - 1)
    traces.score("q", 5.0, created=NOW)
    assert monitors.read(db, make(db), NOW).samples == 1
    # The one call inside the window, and the three traces that hold the scores, at RECENT.
    count = monitors.read(db, make(db, monitors.TRACE_COUNT, name="count"), NOW)
    assert count.value == 4.0
    score = monitors.read(db, make(db, monitors.SCORE_MEAN, name="s", score_name="q"), NOW)
    assert (score.value, score.samples) == (1.0, 1)


# --- Evaluation --------------------------------------------------------------------------


def test_too_few_samples_keeps_the_state_and_says_so(db: Store, traces: Traces) -> None:
    traces.calls(MODEL, failed=30, total=30)
    monitor = make(db, min_samples=30)
    state = monitors.evaluate(db, monitor, NOW)
    assert state is not None
    assert state.state == FIRING
    # Later, the window holds only a few calls: still firing, with no alert either way.
    later = NOW + 11 * MINUTE
    traces.calls(MODEL, failed=0, total=5, start=later - MINUTE)
    state = monitors.evaluate(db, monitor, later)
    assert state is not None
    assert (state.state, state.insufficient, state.samples) == (FIRING, True, 5)
    assert state.since_ns == NOW
    assert [a.kind for a in alerts(db)] == ["firing"]
    stored = monitors.get_state(db, monitor.id)
    assert stored == state


def test_no_samples_is_insufficient_from_ok(db: Store) -> None:
    monitor = make(db)
    state = monitors.evaluate(db, monitor, NOW)
    assert state is not None
    assert (state.state, state.insufficient, state.value, state.samples) == (OK, True, None, 0)
    assert alerts(db) == []


def test_wilson_low_volume_does_not_fire(db: Store, traces: Traces) -> None:
    traces.calls(MODEL, failed=1, total=3)
    state = monitors.evaluate(db, make(db), NOW)
    assert state is not None
    assert state.state == OK
    # The point value, a third, is past the threshold; the lower bound is not.
    assert state.value == pytest.approx(1 / 3)
    assert state.bound is not None
    assert state.bound < 0.2
    assert alerts(db) == []


def test_wilson_real_volume_fires_and_the_point_value_recovers(db: Store, traces: Traces) -> None:
    traces.calls(MODEL, failed=30, total=50)
    monitor = make(db)
    state = monitors.evaluate(db, monitor, NOW)
    assert state is not None
    assert state.state == FIRING
    assert state.bound is not None
    assert state.bound > 0.2
    # Another window: a point value of 0.25 is still on the bad side, so it stays firing,
    # though its lower bound is under the threshold and would not have fired it.
    later = NOW + 11 * MINUTE
    traces.calls(MODEL, failed=25, total=100, start=later - MINUTE)
    state = monitors.evaluate(db, monitor, later)
    assert state is not None
    assert state.state == FIRING
    assert state.bound is not None
    assert state.bound < 0.2
    # Then a point value on the threshold: back on the good side, with no bound to clear.
    last = later + 11 * MINUTE
    traces.calls(MODEL, failed=2, total=10, start=last - MINUTE)
    state = monitors.evaluate(db, monitor, last)
    assert state is not None
    assert state.state == OK
    assert state.value == pytest.approx(0.2)
    assert [a.kind for a in alerts(db)] == ["firing", "resolved"]


def test_a_below_monitor_on_a_boolean_score_uses_the_upper_bound(db: Store, traces: Traces) -> None:
    monitor = make(
        db, monitors.SCORE_MEAN, score_name="q", comparison="below", threshold=0.8, min_samples=3
    )
    # Two of three true: the point value is under 0.8, the upper bound is not.
    for value in (1.0, 1.0, 0.0):
        traces.score("q", value, data_type=BOOLEAN)
    state = monitors.evaluate(db, monitor, NOW)
    assert state is not None
    assert state.state == OK
    assert state.value == pytest.approx(2 / 3)
    assert state.bound is not None
    assert state.bound > 0.8
    # Five of forty true: the upper bound is under it too.
    for n in range(37):
        traces.score("q", 1.0 if n < 3 else 0.0, data_type=BOOLEAN)
    state = monitors.evaluate(db, monitor, NOW)
    assert state is not None
    assert state.state == FIRING
    assert state.bound is not None
    assert state.bound < 0.8


def test_a_non_boolean_score_compares_its_point_value(db: Store, traces: Traces) -> None:
    traces.score("q", 0.5)
    monitor = make(db, monitors.SCORE_MEAN, score_name="q", threshold=0.4)
    state = monitors.evaluate(db, monitor, NOW)
    assert state is not None
    assert (state.state, state.value, state.bound) == (FIRING, 0.5, None)
    later = NOW + 11 * MINUTE
    traces.score("q", 0.4, created=later - MINUTE)
    state = monitors.evaluate(db, monitor, later)
    assert state is not None
    assert state.state == OK


def test_cost_ignores_min_samples(db: Store, traces: Traces) -> None:
    traces.add({**MODEL, "agentwatch.usage.cost_usd": 5.0})
    monitor = make(db, monitors.COST_USD, threshold=1.0, min_samples=1000)
    state = monitors.evaluate(db, monitor, NOW)
    assert state is not None
    assert (state.state, state.value, state.samples, state.insufficient) == (FIRING, 5.0, 1, False)


def test_trace_count_catches_silence_whatever_min_samples_says(db: Store) -> None:
    monitor = make(db, monitors.TRACE_COUNT, comparison="below", threshold=1, min_samples=1000)
    state = monitors.evaluate(db, monitor, NOW)
    assert state is not None
    assert (state.state, state.value, state.samples, state.insufficient) == (FIRING, 0.0, 1, False)


def test_firing_then_resolved_make_one_alert_each(db: Store, traces: Traces) -> None:
    traces.calls(MODEL, failed=20, total=20)
    monitor = make(db)
    runner = MonitorRunner(db, clock=lambda: NOW)
    for _ in range(3):
        assert runner.run_once() == 1
    assert [a.kind for a in alerts(db)] == ["firing"]
    later = NOW + 11 * MINUTE
    traces.calls(MODEL, failed=0, total=5, start=later - MINUTE)
    runner = MonitorRunner(db, clock=lambda: later)
    for _ in range(3):
        runner.run_once()
    fired, resolved = alerts(db)
    assert (fired.kind, resolved.kind) == ("firing", "resolved")
    assert fired.monitor_name == monitor.name
    assert (fired.metric, fired.comparison, fired.threshold) == (monitor.metric, "above", 0.2)
    assert (fired.value, fired.samples, fired.window_minutes) == (1.0, 20, 10)
    assert fired.bound is not None
    assert fired.created_ns == NOW
    assert fired.delivered_ns is None
    assert (resolved.value, resolved.created_ns) == (0.0, later)
    state = monitors.get_state(db, monitor.id)
    assert (state.state, state.since_ns) == (OK, later)


def test_a_change_starts_again_from_ok(db: Store, traces: Traces) -> None:
    traces.calls(MODEL, failed=20, total=20)
    monitor = make(db)
    monitors.evaluate(db, monitor, NOW)
    assert monitors.get_state(db, monitor.id).state == FIRING
    # Saying what is already so is no change.
    monitors.update_monitor(db, monitor.id, {"threshold": 0.2, "name": "watch"})
    assert monitors.get_state(db, monitor.id).state == FIRING
    changed = monitors.update_monitor(db, monitor.id, {"threshold": 0.3, "name": "renamed"})
    assert changed.name == "renamed"
    assert monitors.get_state(db, monitor.id) == monitors.MonitorState(monitor.id)
    monitors.evaluate(db, changed, NOW + 1)
    assert [(a.kind, a.monitor_name) for a in alerts(db)] == [
        ("firing", "watch"),
        ("firing", "renamed"),
    ]
    # Off and on again is also a new start.
    off = monitors.update_monitor(db, monitor.id, {"enabled": False})
    assert monitors.get_state(db, monitor.id).evaluated_ns is None
    assert off.enabled is False


def test_a_result_read_before_a_change_is_discarded(db: Store) -> None:
    monitor = make(db)
    monitors.update_monitor(db, monitor.id, {"threshold": 0.5})
    assert monitors.evaluate(db, monitor, NOW) is None
    assert monitors.get_state(db, monitor.id).evaluated_ns is None


def test_a_disabled_monitor_is_not_evaluated(db: Store, traces: Traces) -> None:
    traces.calls(MODEL, failed=20, total=20)
    make(db, enabled=False)
    assert MonitorRunner(db, clock=lambda: NOW).run_once() == 0
    assert alerts(db) == []


def test_a_failing_monitor_does_not_stop_the_others(
    db: Store, traces: Traces, caplog: pytest.LogCaptureFixture
) -> None:
    traces.calls(MODEL, failed=20, total=20)
    bad = make(db, name="first")
    with db.transaction() as conn:
        conn.execute("UPDATE monitors SET metric = 'gone' WHERE id = ?", [bad.id])
    make(db, name="second")
    with caplog.at_level(logging.ERROR):
        assert MonitorRunner(db, clock=lambda: NOW).run_once() == 1
    assert bad.id in caplog.text
    assert [a.monitor_name for a in alerts(db)] == ["second"]


def test_runner_starts_and_stops(db: Store) -> None:
    runner = MonitorRunner(db, clock=lambda: NOW)
    assert not runner.running
    runner.start(interval_s=0.01)
    runner.start(interval_s=0.01)
    assert runner.running
    runner.stop(timeout_s=5)
    assert not runner.running


# --- Validation --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"metric": "nonsense"}, "metric must be"),
        ({"metric": monitors.SCORE_MEAN}, "score_name is required"),
        ({"threshold": float("nan")}, "finite"),
        ({"threshold": float("inf")}, "finite"),
        ({"threshold": 10**400}, "finite"),
        ({"threshold": True}, "finite"),
        ({"threshold": "1"}, "finite"),
        ({"window_minutes": 0}, "window_minutes"),
        ({"window_minutes": 43201}, "window_minutes"),
        ({"window_minutes": 1.5}, "window_minutes"),
        ({"min_samples": 0}, "min_samples"),
        ({"min_samples": 100_001}, "min_samples"),
        ({"comparison": "equal"}, "comparison must be"),
        ({"enabled": "yes"}, "enabled"),
        ({"name": ""}, "name is required"),
        ({"name": "x" * 201}, "longer than"),
        ({"service": 3}, "service"),
        ({"id": "chosen"}, "unknown field"),
        ({"webhook": "https://example.com"}, "unknown field"),
    ],
)
def test_validation_refusals(db: Store, fields: dict[str, Any], message: str) -> None:
    with pytest.raises(monitors.MonitorError, match=message):
        make(db, **fields)


def test_a_request_must_be_an_object(db: Store) -> None:
    with pytest.raises(monitors.MonitorError, match="JSON object"):
        monitors.create_monitor(db, [])


def test_duplicate_names_are_refused(db: Store) -> None:
    make(db, name="one")
    other = make(db, name="two")
    with pytest.raises(monitors.Conflict):
        make(db, name="one")
    with pytest.raises(monitors.Conflict):
        monitors.update_monitor(db, other.id, {"name": "one"})


def test_defaults_and_score_name_only_for_the_score_metric(db: Store) -> None:
    monitor = monitors.create_monitor(
        db,
        {
            "name": "n",
            "metric": monitors.COST_USD,
            "score_name": "ignored",
            "service": "",
            "window_minutes": 1,
            "comparison": "below",
            "threshold": 1,
        },
    )
    assert monitor.min_samples == 10
    assert monitor.enabled
    assert monitor.score_name is None
    assert monitor.service is None
    assert monitor.threshold == 1.0


# --- API ---------------------------------------------------------------------------------


@pytest.fixture
def client(db: Store) -> TestClient:
    return TestClient(server.create_app(db, ui_dir=None), base_url="http://127.0.0.1")


BODY = {
    "name": "api",
    "metric": monitors.MODEL_ERROR_RATE,
    "window_minutes": 10,
    "comparison": "above",
    "threshold": 0.2,
}


def test_api_crud(client: TestClient, db: Store, traces: Traces) -> None:
    created = client.post("/api/monitors", json=BODY)
    assert created.status_code == 201
    monitor = created.json()["monitor"]
    assert isinstance(monitor["created_ns"], str)
    assert monitor["state"] == {
        "state": "OK",
        "value": None,
        "bound": None,
        "samples": None,
        "evaluated_ns": None,
        "since_ns": None,
        "insufficient": False,
    }
    path = f"/api/monitors/{monitor['id']}"
    assert client.get(path).json()["monitor"] == monitor
    assert client.get("/api/monitors").json()["monitors"] == [monitor]
    assert client.post("/api/monitors", json=BODY).status_code == 409
    assert client.post("/api/monitors", json=BODY | {"metric": "x"}).status_code == 400
    assert client.post("/api/monitors", content=b"{", headers=JSON).status_code == 400
    assert client.post("/api/monitors", content=b"{}").status_code == 415

    traces.calls(MODEL, failed=20, total=20)
    MonitorRunner(db, clock=lambda: NOW).run_once()
    state = client.get(path).json()["monitor"]["state"]
    assert state["state"] == "FIRING"
    assert state["samples"] == 20
    assert (state["evaluated_ns"], state["since_ns"]) == (str(NOW), str(NOW))

    patched = client.patch(path, json={"threshold": 0.5})
    assert patched.status_code == 200
    assert patched.json()["monitor"]["threshold"] == 0.5
    assert patched.json()["monitor"]["state"]["state"] == "OK"
    assert client.patch(path, json={"threshold": "x"}).status_code == 400
    assert client.patch("/api/monitors/none", json={}).status_code == 404

    assert client.delete(path).status_code == 204
    assert client.get(path).status_code == 404
    assert client.delete(path).status_code == 404


JSON = {"content-type": "application/json"}


def fire_and_resolve(db: Store, traces: Traces) -> None:
    """Two alerts, the firing one first."""
    traces.calls(MODEL, failed=20, total=20)
    MonitorRunner(db, clock=lambda: NOW).run_once()
    later = NOW + 11 * MINUTE
    traces.calls(MODEL, failed=0, total=5, start=later - MINUTE)
    MonitorRunner(db, clock=lambda: later).run_once()


def test_alerts_order_and_delivery(client: TestClient, db: Store, traces: Traces) -> None:
    make(db)
    fire_and_resolve(db, traces)
    newest = client.get("/api/alerts").json()["alerts"]
    assert [a["kind"] for a in newest] == ["resolved", "firing"]
    assert isinstance(newest[0]["created_ns"], str)
    assert newest[0]["delivered_ns"] is None
    assert set(newest[0]) == {
        "id",
        "monitor_id",
        "monitor_name",
        "kind",
        "metric",
        "comparison",
        "threshold",
        "value",
        "bound",
        "samples",
        "window_minutes",
        "created_ns",
        "delivered_ns",
    }
    oldest = client.get("/api/alerts?undelivered=1").json()["alerts"]
    assert [a["kind"] for a in oldest] == ["firing", "resolved"]
    assert len(client.get("/api/alerts?limit=1").json()["alerts"]) == 1
    assert client.get("/api/alerts?limit=x").status_code == 400

    target = f"/api/alerts/{oldest[0]['id']}/delivered"
    first = client.post(target, headers=JSON)
    assert first.status_code == 200
    delivered = first.json()["alert"]["delivered_ns"]
    assert isinstance(delivered, str)
    # Delivering again keeps the first time.
    assert client.post(target, headers=JSON).json()["alert"]["delivered_ns"] == delivered
    left = client.get("/api/alerts?undelivered=1").json()["alerts"]
    assert [a["kind"] for a in left] == ["resolved"]
    assert client.post("/api/alerts/none/delivered", headers=JSON).status_code == 404
    # A write like any other: a plain cross-origin form cannot make this request.
    assert client.post(target).status_code == 415


def test_mark_delivered_sets_the_time_once(db: Store, traces: Traces) -> None:
    make(db)
    fire_and_resolve(db, traces)
    alert = alerts(db)[0]
    assert monitors.mark_delivered(db, alert.id, now_ns=5).delivered_ns == 5
    assert monitors.mark_delivered(db, alert.id, now_ns=6).delivered_ns == 5


def test_alerts_survive_monitor_deletion(db: Store, traces: Traces) -> None:
    monitor = make(db)
    fire_and_resolve(db, traces)
    monitors.delete_monitor(db, monitor.id)
    assert [a.monitor_name for a in alerts(db)] == ["watch", "watch"]
    assert db.query("SELECT * FROM monitor_states") == []
    assert monitors.list_monitors(db) == []


# --- Retention and schema ----------------------------------------------------------------


def test_prune_removes_old_alerts_only(db: Store, traces: Traces) -> None:
    monitor = make(db)
    fire_and_resolve(db, traces)
    fired, resolved = alerts(db)
    monitors.mark_delivered(db, resolved.id)
    # Between the two: the firing alert is old and undelivered, and still goes.
    db.prune(NOW + 1)
    assert [a.id for a in alerts(db)] == [resolved.id]
    assert fired.id not in [a.id for a in alerts(db)]
    # The later one, delivered, goes too once it is old.
    db.prune(NOW + DAY_NS)
    assert alerts(db) == []
    assert monitors.list_monitors(db) == [monitor]
    assert monitors.get_state(db, monitor.id).evaluated_ns is not None


def test_pruning_alerts_leaves_recent_scores(db: Store, traces: Traces) -> None:
    traces.score("q", 1.0, created=NOW)
    make(db, monitors.SCORE_MEAN, score_name="q")
    db.prune(NOW - DAY_NS)
    assert db.query_one("SELECT count(*) FROM scores")[0] == 1


def test_migration_from_schema_8(tmp_path: Path) -> None:
    path = tmp_path / "old.duckdb"
    old = Store(path)
    old.insert(
        [
            Span(
                trace_id="1" * 32,
                span_id="1" * 16,
                parent_span_id=None,
                name="s",
                start_ns=1,
                end_ns=2,
            )
        ],
        received_ns=1,
    )
    old.close()
    # Back to how version 8 left it: without the tables version 9 adds.
    conn = duckdb.connect(str(path))
    for table in ("monitors", "monitor_states", "alerts"):
        conn.execute(f"DROP TABLE {table}")
    conn.execute("UPDATE meta SET value = '8' WHERE key = 'schema'")
    conn.close()

    upgraded = Store(path)
    assert upgraded.query_one("SELECT value FROM meta WHERE key = 'schema'")[0] == str(
        store.SCHEMA_VERSION
    )
    assert store.SCHEMA_VERSION == 9
    assert upgraded.span_count() == 1
    make(upgraded)
    assert len(monitors.list_monitors(upgraded)) == 1
    assert monitors.list_alerts(upgraded) == []
    upgraded.close()
