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
        "cooldown_minutes": 0,
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


def test_five_failures_in_five_calls_fire_with_the_defaults(db: Store, traces: Traces) -> None:
    traces.calls(MODEL, failed=5, total=5)
    monitor = monitors.create_monitor(
        db,
        {
            "name": "defaults",
            "metric": monitors.MODEL_ERROR_RATE,
            "window_minutes": 10,
            "comparison": "above",
            "threshold": 0.2,
        },
    )
    assert monitor.min_samples == 3
    state = monitors.evaluate(db, monitor, NOW)
    assert state is not None
    assert state.state == FIRING


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


def firing(db: Store, traces: Traces, **fields: Any) -> monitors.Monitor:
    traces.calls(MODEL, failed=20, total=20)
    monitor = make(db, **fields)
    monitors.evaluate(db, monitor, NOW)
    assert monitors.get_state(db, monitor.id).state == FIRING
    return monitor


def test_a_no_op_or_a_rename_or_a_cooldown_keeps_the_state(db: Store, traces: Traces) -> None:
    monitor = firing(db, traces)
    before = monitors.get_state(db, monitor.id)
    monitors.update_monitor(db, monitor.id, {"threshold": 0.2, "name": "watch", "service": None})
    assert monitors.get_state(db, monitor.id) == before
    renamed = monitors.update_monitor(db, monitor.id, {"name": "renamed"})
    assert renamed.name == "renamed"
    assert monitors.get_state(db, monitor.id) == before
    cooled = monitors.update_monitor(db, monitor.id, {"cooldown_minutes": 5})
    assert cooled.cooldown_minutes == 5
    assert monitors.get_state(db, monitor.id) == before
    assert [a.kind for a in alerts(db)] == ["firing"]
    # The state carries on under the new name, with no second alert.
    state = monitors.evaluate(db, cooled, NOW + 1)
    assert state is not None
    assert state.state == FIRING
    assert len(alerts(db)) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"metric": monitors.TOOL_ERROR_RATE},
        {"metric": monitors.SCORE_MEAN, "score_name": "q"},
        {"service": "other"},
        {"window_minutes": 20},
        {"comparison": "below"},
        {"threshold": 0.3},
        {"min_samples": 2},
        {"enabled": False},
    ],
)
def test_a_rule_change_while_firing_writes_one_resolved_alert(
    db: Store, traces: Traces, change: dict[str, Any]
) -> None:
    monitor = firing(db, traces)
    monitors.update_monitor(db, monitor.id, change, now_ns=NOW + 1)
    assert monitors.get_state(db, monitor.id) == monitors.MonitorState(monitor.id)
    fired, resolved = alerts(db)
    assert fired.reason is None
    assert (resolved.kind, resolved.reason, resolved.value) == ("resolved", "changed", None)
    assert (resolved.bound, resolved.samples, resolved.created_ns) == (None, 0, NOW + 1)
    # It describes the rule that was firing.
    assert (resolved.metric, resolved.threshold) == (monitor.metric, monitor.threshold)


def test_a_rule_change_that_also_renames_uses_the_old_name(db: Store, traces: Traces) -> None:
    monitor = firing(db, traces)
    changed = monitors.update_monitor(
        db, monitor.id, {"threshold": 0.3, "name": "renamed"}, now_ns=NOW + 1
    )
    monitors.evaluate(db, changed, NOW + 2)
    assert [(a.kind, a.monitor_name) for a in alerts(db)] == [
        ("firing", "watch"),
        ("resolved", "watch"),
        ("firing", "renamed"),
    ]


def test_a_change_while_ok_writes_no_alert(db: Store) -> None:
    monitor = make(db)
    monitors.evaluate(db, monitor, NOW)
    monitors.update_monitor(db, monitor.id, {"threshold": 0.3})
    monitors.update_monitor(db, monitor.id, {"enabled": False})
    monitors.update_monitor(db, monitor.id, {"enabled": True})
    monitors.delete_monitor(db, monitor.id)
    assert alerts(db) == []


def test_deleting_a_firing_monitor_writes_one_resolved_alert(db: Store, traces: Traces) -> None:
    monitor = firing(db, traces)
    monitors.delete_monitor(db, monitor.id, now_ns=NOW + 1)
    fired, resolved = alerts(db)
    assert fired.kind == "firing"
    assert (resolved.kind, resolved.reason, resolved.value) == ("resolved", "removed", None)
    assert resolved.monitor_name == "watch"
    assert db.query("SELECT * FROM monitor_states") == []
    with pytest.raises(monitors.NotFound):
        monitors.delete_monitor(db, monitor.id)


def test_disable_then_enable_starts_again(db: Store, traces: Traces) -> None:
    monitor = firing(db, traces)
    monitors.update_monitor(db, monitor.id, {"enabled": False}, now_ns=NOW + 1)
    assert monitors.get_state(db, monitor.id).evaluated_ns is None
    on = monitors.update_monitor(db, monitor.id, {"enabled": True}, now_ns=NOW + 2)
    assert monitors.get_state(db, monitor.id).evaluated_ns is None
    assert [a.kind for a in alerts(db)] == ["firing", "resolved"]
    monitors.evaluate(db, on, NOW + 3)
    assert [a.kind for a in alerts(db)] == ["firing", "resolved", "firing"]


# --- Cooldown ----------------------------------------------------------------------------


def alternating(db: Store, traces: Traces, **fields: Any) -> list[monitors.Alert]:
    """A trace every other minute against a count that needs 3 in 5 minutes, for 20 minutes:
    the count moves between 2 and 3, so it crosses the threshold at every evaluation."""
    for minute in range(-10, 21, 2):
        traces.add({}, start=NOW + minute * MINUTE)
    monitor = make(
        db, monitors.TRACE_COUNT, window_minutes=5, comparison="below", threshold=3, **fields
    )
    for minute in range(21):
        monitors.evaluate(db, monitor, NOW + minute * MINUTE)
    return alerts(db)


def test_a_value_at_its_threshold_makes_one_alert_within_the_cooldown(
    db: Store, traces: Traces
) -> None:
    assert [a.kind for a in alternating(db, traces, cooldown_minutes=60)] == ["firing"]


def test_a_cooldown_of_zero_resolves_at_once(db: Store, traces: Traces) -> None:
    kinds = [a.kind for a in alternating(db, traces, cooldown_minutes=0)]
    assert kinds[:4] == ["firing", "resolved", "firing", "resolved"]


def count_monitor(db: Store) -> monitors.Monitor:
    """Fires above 0 traces in 5 minutes; a window without traces is on the good side."""
    return make(
        db, monitors.TRACE_COUNT, window_minutes=5, threshold=0, cooldown_minutes=30, name="count"
    )


def test_resolves_after_the_cooldown(db: Store, traces: Traces) -> None:
    traces.add({}, start=NOW - MINUTE)
    monitor = count_monitor(db)
    assert_state(db, monitor, NOW, FIRING, good_since=None)
    good = NOW + 10 * MINUTE
    assert_state(db, monitor, good, FIRING, good_since=good)
    assert_state(db, monitor, good + 30 * MINUTE - 1, FIRING, good_since=good)
    assert_state(db, monitor, good + 30 * MINUTE, OK, good_since=None)
    assert [a.kind for a in alerts(db)] == ["firing", "resolved"]
    assert alerts(db)[1].reason is None
    assert alerts(db)[1].value == 0.0


def test_a_bad_evaluation_starts_the_cooldown_again(db: Store, traces: Traces) -> None:
    traces.add({}, start=NOW - MINUTE)
    monitor = count_monitor(db)
    assert_state(db, monitor, NOW, FIRING, good_since=None)
    good = NOW + 10 * MINUTE
    assert_state(db, monitor, good, FIRING, good_since=good)
    bad = good + 29 * MINUTE
    traces.add({}, start=bad - MINUTE)
    assert_state(db, monitor, bad, FIRING, good_since=None)
    again = bad + 10 * MINUTE
    assert_state(db, monitor, again, FIRING, good_since=again)
    assert_state(db, monitor, again + 30 * MINUTE, OK, good_since=None)
    assert [a.kind for a in alerts(db)] == ["firing", "resolved"]


def assert_state(
    db: Store, monitor: monitors.Monitor, now: int, state: str, *, good_since: int | None
) -> None:
    result = monitors.evaluate(db, monitor, now)
    assert result is not None
    assert (result.state, result.good_since_ns) == (state, good_since)


def test_a_rate_recovers_by_the_point_value_after_the_cooldown(db: Store, traces: Traces) -> None:
    monitor = firing(db, traces, cooldown_minutes=30)
    # One failure in two: the lower bound is under the threshold but the point value is not.
    bad = NOW + 11 * MINUTE
    traces.calls(MODEL, failed=1, total=2, start=bad - MINUTE)
    assert_state(db, monitor, bad, FIRING, good_since=None)
    # Back on the good side, by the point value alone.
    good = bad + 11 * MINUTE
    traces.calls(MODEL, failed=1, total=5, start=good - MINUTE)
    assert_state(db, monitor, good, FIRING, good_since=good)
    last = good + 30 * MINUTE
    traces.calls(MODEL, failed=0, total=5, start=last - MINUTE)
    assert_state(db, monitor, last, OK, good_since=None)


def test_insufficient_samples_neither_clear_nor_advance_the_cooldown(
    db: Store, traces: Traces
) -> None:
    monitor = firing(db, traces, cooldown_minutes=30, min_samples=3)
    start = NOW + 11 * MINUTE
    traces.calls(MODEL, failed=0, total=5, start=start - MINUTE)
    assert_state(db, monitor, start, FIRING, good_since=start)
    # Eight good minutes build up, with the same calls in the window.
    built = start + 8 * MINUTE
    assert_state(db, monitor, built, FIRING, good_since=start)
    # Two hours of silence: not resolved, and no closer to it. The eight minutes are still there.
    for hours in (1, 2):
        at = built + hours * 60 * MINUTE
        result = monitors.evaluate(db, monitor, at)
        assert result is not None
        assert (result.state, result.insufficient) == (FIRING, True)
        assert result.good_since_ns == at - 8 * MINUTE
    silent_end = built + 120 * MINUTE
    # Calls return, and time counts again from the last silent evaluation: 8 + 21 is still short
    # of the cooldown, and 8 + 22 reaches it.
    back = silent_end + 21 * MINUTE
    traces.calls(MODEL, failed=0, total=5, start=back - MINUTE)
    assert_state(db, monitor, back, FIRING, good_since=silent_end - 8 * MINUTE)
    done = silent_end + 22 * MINUTE
    assert_state(db, monitor, done, OK, good_since=None)
    assert [a.kind for a in alerts(db)] == ["firing", "resolved"]


def test_a_rate_does_not_resolve_from_silence(db: Store, traces: Traces) -> None:
    monitor = firing(db, traces, cooldown_minutes=0)
    state = monitors.evaluate(db, monitor, NOW + 1000 * MINUTE)
    assert state is not None
    assert (state.state, state.insufficient) == (FIRING, True)
    assert [a.kind for a in alerts(db)] == ["firing"]


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
        ({"cooldown_minutes": -1}, "cooldown_minutes"),
        ({"cooldown_minutes": 43201}, "cooldown_minutes"),
        ({"cooldown_minutes": 1.5}, "cooldown_minutes"),
        ({"threshold": 5}, "0.05 means 5%"),
        ({"threshold": -0.1}, "0.05 means 5%"),
        ({"metric": monitors.TOOL_ERROR_RATE, "threshold": 1.01}, "0.05 means 5%"),
        ({"comparison": "equal"}, "comparison must be"),
        ({"enabled": "yes"}, "enabled"),
        ({"name": ""}, "name is required"),
        ({"name": "x" * 201}, "longer than"),
        ({"name": "two\nlines"}, "control or formatting"),
        ({"name": "trailing\n"}, "control or formatting"),
        ({"name": "bell\x07"}, "control or formatting"),
        ({"name": "evil\u202egnp.exe"}, "control or formatting"),
        ({"name": "zero\u200bwidth"}, "control or formatting"),
        ({"name": "line\u2028separator"}, "control or formatting"),
        ({"service": 3}, "service"),
        ({"id": "chosen"}, "unknown field"),
        ({"webhook": "https://example.com"}, "unknown field"),
    ],
)
def test_validation_refusals(db: Store, fields: dict[str, Any], message: str) -> None:
    with pytest.raises(monitors.MonitorError, match=message):
        make(db, **fields)


def test_a_threshold_outside_0_to_1_is_fine_except_on_error_rates(db: Store) -> None:
    assert make(db, monitors.COST_USD, threshold=50, name="cost").threshold == 50.0
    assert make(db, threshold=1, name="edge").threshold == 1.0
    assert make(db, threshold=0, name="zero").threshold == 0.0
    cost = make(db, monitors.COST_USD, threshold=50, name="switch")
    with pytest.raises(monitors.MonitorError, match=r"0\.05 means 5%"):
        monitors.update_monitor(db, cost.id, {"metric": monitors.MODEL_ERROR_RATE})


def test_a_bad_name_is_refused_on_update(db: Store) -> None:
    monitor = make(db)
    with pytest.raises(monitors.MonitorError, match="control or formatting"):
        monitors.update_monitor(db, monitor.id, {"name": "a\u202eb"})
    assert monitors.get_monitor(db, monitor.id).name == "watch"


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
    assert monitor.cooldown_minutes == 60
    assert monitor.enabled
    assert monitor.score_name is None
    assert monitor.service is None
    assert monitor.threshold == 1.0


@pytest.mark.parametrize(
    ("metric", "minimum"),
    [
        (monitors.MODEL_ERROR_RATE, 3),
        (monitors.TOOL_ERROR_RATE, 3),
        (monitors.COST_USD, 10),
        (monitors.MODEL_P95_MS, 10),
        (monitors.TRACE_COUNT, 10),
        (monitors.SCORE_MEAN, 10),
    ],
)
def test_default_min_samples_per_metric(db: Store, metric: str, minimum: int) -> None:
    body = {
        "name": "n",
        "metric": metric,
        "score_name": "q",
        "window_minutes": 1,
        "comparison": "above",
        "threshold": 0.5,
    }
    assert monitors.create_monitor(db, body).min_samples == minimum
    given = monitors.create_monitor(db, body | {"name": "given", "min_samples": 7})
    assert given.min_samples == 7


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
        "good_since_ns": None,
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

    # Back to firing: deleting says so through the API too.
    MonitorRunner(db, clock=lambda: NOW).run_once()
    assert client.get(path).json()["monitor"]["state"]["state"] == "FIRING"
    assert client.delete(path).status_code == 204
    latest = client.get("/api/alerts").json()["alerts"][0]
    assert (latest["kind"], latest["reason"], latest["value"]) == ("resolved", "removed", None)
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
        "reason",
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
