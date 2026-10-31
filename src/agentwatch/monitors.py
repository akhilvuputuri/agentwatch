"""Monitors: rules over a trailing window of metrics, which raise alerts when breached.

A background runner reads each enabled monitor's metric and moves it between two states: OK and
FIRING. Each move writes one alert, in the same transaction, so a monitor that stays firing does
not repeat itself and an alert is never lost from a move that happened.

A rate from few calls says little: one failure in three calls is a 33% error rate, and means
almost nothing. So a rate fires on a Wilson 95% bound, not on its point value: an "above" monitor
when the lower bound exceeds the threshold, a "below" monitor when the upper bound is under it.
It recovers once the point value is back on the good side of the threshold, so a monitor does
not flap around the bound or wait for a bound to clear. Everything else compares its point value.

Alerts carry only names and numbers. Delivery is a client polling for undelivered alerts, so
agentwatch stores no URL or secret and calls nothing.
"""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import duckdb

from agentwatch.metrics import Window, monitor_figures, score_figures
from agentwatch.stats import wilson
from agentwatch.store import Store

log = logging.getLogger(__name__)

MODEL_ERROR_RATE = "model_error_rate"
TOOL_ERROR_RATE = "tool_error_rate"
COST_USD = "cost_usd"
MODEL_P95_MS = "model_p95_ms"
TRACE_COUNT = "trace_count"
SCORE_MEAN = "score_mean"
METRICS = (MODEL_ERROR_RATE, TOOL_ERROR_RATE, COST_USD, MODEL_P95_MS, TRACE_COUNT, SCORE_MEAN)

ABOVE = "above"
BELOW = "below"
COMPARISONS = (ABOVE, BELOW)

OK = "OK"
FIRING = "FIRING"
# Kinds of alert.
FIRED = "firing"
RESOLVED = "resolved"

MINUTE_NS = 60 * 1_000_000_000
MAX_WINDOW_MINUTES = 30 * 24 * 60
MAX_MIN_SAMPLES = 100_000
MAX_NAME = 200


class MonitorError(ValueError):
    """A monitor definition that cannot be stored, with the reason."""


class NotFound(MonitorError):
    pass


class Conflict(MonitorError):
    pass


@dataclass(frozen=True, slots=True)
class Monitor:
    id: str
    name: str
    metric: str
    # The score read by score_mean; None for every other metric.
    score_name: str | None
    # Limits the metric to traces whose root span has this service.
    service: str | None
    window_minutes: int
    comparison: str
    threshold: float
    min_samples: int
    enabled: bool
    created_ns: int


@dataclass(frozen=True, slots=True)
class MonitorState:
    """A monitor's last evaluation. The fields are empty until its first one."""

    monitor_id: str
    state: str = OK
    value: float | None = None
    bound: float | None = None
    samples: int | None = None
    evaluated_ns: int | None = None
    # When it entered this state.
    since_ns: int | None = None
    # The last evaluation had too few samples to compare, so the state stayed as it was.
    insufficient: bool = False


@dataclass(frozen=True, slots=True)
class Alert:
    id: str
    monitor_id: str
    # Copied, so an alert still reads correctly after its monitor is renamed or deleted.
    monitor_name: str
    kind: str
    metric: str
    comparison: str
    threshold: float
    value: float
    bound: float | None
    samples: int
    window_minutes: int
    created_ns: int
    delivered_ns: int | None


@dataclass(frozen=True, slots=True)
class Reading:
    """A metric over one window."""

    value: float | None
    samples: int
    # A rate's Wilson bound on the threshold's side; None for anything that is not a rate.
    bound: float | None = None
    # False for totals, whose value means the same however few samples went into it.
    needs_samples: bool = True


_COLUMNS = (
    "id, name, metric, score_name, service, window_minutes, comparison, threshold, min_samples, "
    "enabled, created_ns"
)
_STATE_COLUMNS = "monitor_id, state, value, bound, samples, evaluated_ns, since_ns, insufficient"
_ALERT_COLUMNS = (
    "id, monitor_id, monitor_name, kind, metric, comparison, threshold, value, bound, samples, "
    "window_minutes, created_ns, delivered_ns"
)
_FIELDS = (
    "name",
    "metric",
    "score_name",
    "service",
    "window_minutes",
    "comparison",
    "threshold",
    "min_samples",
    "enabled",
)


# --- Definitions -------------------------------------------------------------------------


def create_monitor(store: Store, body: Any, *, now_ns: int | None = None) -> Monitor:
    data = _known(body)
    defaults: dict[str, Any] = {"min_samples": 10, "enabled": True}
    created = time.time_ns() if now_ns is None else now_ns
    monitor = _validated(defaults | data, monitor_id=uuid.uuid4().hex, created_ns=created)
    with store.transaction() as db:
        _check_name_free(db, monitor.name)
        db.execute(
            f"INSERT INTO monitors ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                monitor.id,
                monitor.name,
                monitor.metric,
                monitor.score_name,
                monitor.service,
                monitor.window_minutes,
                monitor.comparison,
                monitor.threshold,
                monitor.min_samples,
                monitor.enabled,
                monitor.created_ns,
            ],
        )
    return monitor


def update_monitor(store: Store, monitor_id: str, body: Any) -> Monitor:
    """Applies the fields given. Any real change sends the monitor back to OK, to re-evaluate:
    its state described the old rule, or a time when it was switched off."""
    data = _known(body)
    with store.transaction() as db:
        current = _get(db, monitor_id)
        monitor = _validated(
            asdict(current) | data, monitor_id=current.id, created_ns=current.created_ns
        )
        if monitor == current:
            return current
        if monitor.name != current.name:
            _check_name_free(db, monitor.name)
            db.execute("UPDATE monitors SET name = ? WHERE id = ?", [monitor.name, monitor.id])
        db.execute(
            "UPDATE monitors SET metric = ?, score_name = ?, service = ?, window_minutes = ?, "
            "comparison = ?, threshold = ?, min_samples = ?, enabled = ? WHERE id = ?",
            [
                monitor.metric,
                monitor.score_name,
                monitor.service,
                monitor.window_minutes,
                monitor.comparison,
                monitor.threshold,
                monitor.min_samples,
                monitor.enabled,
                monitor.id,
            ],
        )
        db.execute("DELETE FROM monitor_states WHERE monitor_id = ?", [monitor.id])
    return monitor


def delete_monitor(store: Store, monitor_id: str) -> None:
    """Deletes the monitor and its state; the alerts it raised stay."""
    with store.transaction() as db:
        if not db.execute(
            "DELETE FROM monitors WHERE id = ? RETURNING id", [monitor_id]
        ).fetchone():
            raise NotFound("monitor not found")
        db.execute("DELETE FROM monitor_states WHERE monitor_id = ?", [monitor_id])


def list_monitors(store: Store) -> list[Monitor]:
    rows = store.query(f"SELECT {_COLUMNS} FROM monitors ORDER BY created_ns, id")
    return [Monitor(*row) for row in rows]


def get_monitor(store: Store, monitor_id: str) -> Monitor:
    row = store.query_one(f"SELECT {_COLUMNS} FROM monitors WHERE id = ?", [monitor_id])
    if row is None:
        raise NotFound("monitor not found")
    return Monitor(*row)


def get_state(store: Store, monitor_id: str) -> MonitorState:
    row = store.query_one(
        f"SELECT {_STATE_COLUMNS} FROM monitor_states WHERE monitor_id = ?", [monitor_id]
    )
    return MonitorState(monitor_id) if row is None else MonitorState(*row)


def list_alerts(store: Store, *, limit: int = 50, undelivered: bool = False) -> list[Alert]:
    """Newest first; or, for a delivering client, the undelivered ones oldest first."""
    where, order = ("WHERE delivered_ns IS NULL", "") if undelivered else ("", "DESC")
    rows = store.query(
        f"SELECT {_ALERT_COLUMNS} FROM alerts {where} ORDER BY created_ns {order}, id LIMIT ?",
        [limit],
    )
    return [Alert(*row) for row in rows]


def mark_delivered(store: Store, alert_id: str, *, now_ns: int | None = None) -> Alert:
    """Records delivery once: a client retrying after a lost reply keeps the first time."""
    delivered = time.time_ns() if now_ns is None else now_ns
    with store.transaction() as db:
        db.execute(
            "UPDATE alerts SET delivered_ns = ? WHERE id = ? AND delivered_ns IS NULL",
            [delivered, alert_id],
        )
        row = db.execute(f"SELECT {_ALERT_COLUMNS} FROM alerts WHERE id = ?", [alert_id]).fetchone()
    if row is None:
        raise NotFound("alert not found")
    return Alert(*row)


def _known(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise MonitorError("the request must be a JSON object")
    data: dict[str, Any] = body  # pyright: ignore[reportUnknownVariableType]
    unknown = [key for key in data if key not in _FIELDS]
    if unknown:
        raise MonitorError(f"unknown field(s): {', '.join(sorted(unknown))}")
    return data


def _validated(fields: dict[str, Any], *, monitor_id: str, created_ns: int) -> Monitor:
    """A monitor from untrusted field values, or MonitorError naming the first bad one."""
    name = _text(fields.get("name"), "name")
    metric = fields.get("metric")
    if metric not in METRICS:
        raise MonitorError(f"metric must be one of {', '.join(METRICS)}")
    score_name = None
    if metric == SCORE_MEAN:
        if fields.get("score_name") in (None, ""):
            raise MonitorError("score_name is required for score_mean")
        score_name = _text(fields.get("score_name"), "score_name")
    comparison = fields.get("comparison")
    if comparison not in COMPARISONS:
        raise MonitorError(f"comparison must be one of {', '.join(COMPARISONS)}")
    enabled = fields.get("enabled")
    if not isinstance(enabled, bool):
        raise MonitorError("enabled must be true or false")
    service = fields.get("service")
    return Monitor(
        id=monitor_id,
        name=name,
        metric=metric,
        score_name=score_name,
        service=None if service in (None, "") else _text(service, "service"),
        window_minutes=_whole(fields.get("window_minutes"), MAX_WINDOW_MINUTES, "window_minutes"),
        comparison=comparison,
        threshold=_threshold(fields.get("threshold")),
        min_samples=_whole(fields.get("min_samples"), MAX_MIN_SAMPLES, "min_samples"),
        enabled=enabled,
        created_ns=created_ns,
    )


def _threshold(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MonitorError("threshold must be a finite number")
    try:
        number = float(value)
    except OverflowError as error:  # JSON allows an integer too large for a float
        raise MonitorError("threshold must be a finite number") from error
    if not math.isfinite(number):
        raise MonitorError("threshold must be a finite number")
    return number


def _whole(value: Any, maximum: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise MonitorError(f"{field} must be a whole number from 1 to {maximum}")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MonitorError(f"{field} is required")
    if len(value.strip()) > MAX_NAME:
        raise MonitorError(f"{field} is longer than {MAX_NAME} characters")
    return value.strip()


def _check_name_free(db: duckdb.DuckDBPyConnection, name: str) -> None:
    if db.execute("SELECT 1 FROM monitors WHERE name = ?", [name]).fetchone():
        raise Conflict(f"a monitor named {name!r} already exists")


def _get(db: duckdb.DuckDBPyConnection, monitor_id: str) -> Monitor:
    row = db.execute(f"SELECT {_COLUMNS} FROM monitors WHERE id = ?", [monitor_id]).fetchone()
    if row is None:
        raise NotFound("monitor not found")
    return Monitor(*row)


# --- Evaluation --------------------------------------------------------------------------


def read(store: Store, monitor: Monitor, now_ns: int) -> Reading:
    """The monitor's metric over the window ending now, by the dashboard's population rules.

    - model_error_rate, tool_error_rate: failed calls / calls, a rate over the calls.
    - cost_usd: the sum of cost, with model calls as its samples. A total: min_samples does not
      apply, since a cost spike must alert even from few calls.
    - model_p95_ms: the 95th percentile of model call latency, over the model calls.
    - trace_count: traces whose root started in the window. Always one sample, so min_samples
      does not apply and a "below" monitor can catch silence.
    - score_mean: the mean of the named score recorded in the window, BOOLEAN or NUMERIC, from
      any source. A rate only when every one of those scores is BOOLEAN, since the Wilson bound
      is for proportions; otherwise compared by its point value.
    """
    window = Window(now_ns - monitor.window_minutes * MINUTE_NS, now_ns, monitor.service)
    if monitor.metric == SCORE_MEAN:
        scores = score_figures(store, window, monitor.score_name or "")
        if scores["samples"] and scores["booleans"] == scores["samples"]:
            return _rate(monitor, round(scores["successes"]), scores["samples"])
        return Reading(scores["mean"], scores["samples"])
    figures = monitor_figures(store, window)
    if monitor.metric == MODEL_ERROR_RATE:
        return _rate(monitor, figures["model_errors"], figures["model_calls"])
    if monitor.metric == TOOL_ERROR_RATE:
        return _rate(monitor, figures["tool_errors"], figures["tool_calls"])
    if monitor.metric == COST_USD:
        return Reading(figures["cost_usd"] or 0.0, figures["model_calls"], needs_samples=False)
    if monitor.metric == MODEL_P95_MS:
        return Reading(figures["model_p95_ms"], figures["model_calls"])
    if monitor.metric == TRACE_COUNT:
        return Reading(float(figures["traces"]), 1, needs_samples=False)
    raise MonitorError(f"unknown metric {monitor.metric!r}")


def _rate(monitor: Monitor, successes: int, total: int) -> Reading:
    interval = wilson(successes, total)
    if interval is None:
        return Reading(None, 0)
    bound = interval[0] if monitor.comparison == ABOVE else interval[1]
    return Reading(successes / total, total, bound)


def next_state(monitor: Monitor, reading: Reading, state: str) -> str:
    """Firing needs the bound (for a rate) past the threshold; recovery only the point value."""
    assert reading.value is not None
    if state == FIRING or reading.bound is None:
        return FIRING if _past(monitor, reading.value) else OK
    return FIRING if _past(monitor, reading.bound) else OK


def _past(monitor: Monitor, value: float) -> bool:
    if monitor.comparison == ABOVE:
        return value > monitor.threshold
    return value < monitor.threshold


def evaluate(store: Store, monitor: Monitor, now_ns: int) -> MonitorState | None:
    """Reads the metric, then records the result; None when the monitor changed meanwhile.

    The query takes the store's lock only for itself, and the transaction covers only the
    state and its alert, so a monitor over a long window does not hold up ingest any longer
    than its query does.
    """
    reading = read(store, monitor, now_ns)
    with store.transaction() as db:
        row = db.execute(f"SELECT {_COLUMNS} FROM monitors WHERE id = ?", [monitor.id]).fetchone()
        if row is None or Monitor(*row) != monitor:
            return None  # deleted or changed during the read, which no longer applies
        stored = db.execute(
            "SELECT state, since_ns FROM monitor_states WHERE monitor_id = ?", [monitor.id]
        ).fetchone()
        state: str = OK if stored is None else stored[0]
        since: int = now_ns if stored is None else stored[1]
        insufficient = reading.value is None or (
            reading.needs_samples and reading.samples < monitor.min_samples
        )
        following = state if insufficient else next_state(monitor, reading, state)
        if following != state:
            since = now_ns
            _write_alert(db, monitor, reading, following, now_ns)
        result = MonitorState(
            monitor_id=monitor.id,
            state=following,
            value=reading.value,
            bound=reading.bound,
            samples=reading.samples,
            evaluated_ns=now_ns,
            since_ns=since,
            insufficient=insufficient,
        )
        db.execute(
            f"INSERT OR REPLACE INTO monitor_states ({_STATE_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                result.monitor_id,
                result.state,
                result.value,
                result.bound,
                result.samples,
                result.evaluated_ns,
                result.since_ns,
                result.insufficient,
            ],
        )
    return result


def _write_alert(
    db: duckdb.DuckDBPyConnection, monitor: Monitor, reading: Reading, state: str, now_ns: int
) -> None:
    db.execute(
        f"INSERT INTO alerts ({_ALERT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        [
            uuid.uuid4().hex,
            monitor.id,
            monitor.name,
            FIRED if state == FIRING else RESOLVED,
            monitor.metric,
            monitor.comparison,
            monitor.threshold,
            reading.value,
            reading.bound,
            reading.samples,
            monitor.window_minutes,
            now_ns,
        ],
    )


class MonitorRunner:
    """Evaluates enabled monitors in the background, one pass every `interval_s` seconds."""

    def __init__(self, store: Store, *, clock: Callable[[], int] = time.time_ns) -> None:
        self.store = store
        self.clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, interval_s: float = 60.0) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.wait(interval_s):
                try:
                    self.run_once()
                except Exception:  # a bad pass must not end the loop
                    log.exception("monitor pass failed")

        self._thread = threading.Thread(target=loop, name="agentwatch-monitors", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 5) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
            if not self._thread.is_alive():
                self._thread = None

    @property
    def running(self) -> bool:
        """Whether the background thread is still working (after stop(): mid-query)."""
        return self._thread is not None and self._thread.is_alive()

    def run_once(self) -> int:
        """One pass over every enabled monitor; returns how many were evaluated."""
        evaluated = 0
        for monitor in list_monitors(self.store):
            if not monitor.enabled or self._stop.is_set():
                continue
            try:
                evaluate(self.store, monitor, self.clock())
            except Exception:  # one bad monitor must not stop the others
                log.exception("monitor %s failed", monitor.id)
            else:
                evaluated += 1
        return evaluated
