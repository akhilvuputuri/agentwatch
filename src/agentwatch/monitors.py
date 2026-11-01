"""Monitors: rules over a trailing window of metrics, which raise alerts when breached.

A background runner reads each enabled monitor's metric and moves it between two states: OK and
FIRING. Each move writes one alert, in the same transaction, so a monitor that stays firing does
not repeat itself and an alert is never lost from a move that happened.

A rate from few calls says little: one failure in three calls is a 33% error rate, and means
almost nothing. So a rate fires on a Wilson 95% bound, not on its point value: an "above" monitor
when the lower bound exceeds the threshold, a "below" monitor when the upper bound is under it.
Everything else compares its point value.

A firing monitor resolves only once its point value has stayed on the good side of the threshold
for a cooldown, with no bound to clear. Without one, a value that sits at the threshold (a trace
count that moves between 2 and 3 at a threshold of 3) would write an alert every other minute.

A drift monitor asks a different question: did the metric move, between the trailing window and
the window directly before it? See `drift` for the comparison. It tests overlapping windows over
and over, so it has guards of its own: it evaluates at most once an hour, uses a 99.5% interval,
and fires only on six breaches in a row. It reports a change, not a level: once a shift has been
in place for a whole baseline window, the two windows agree again and the monitor resolves, whatever
the level is. Simulated traffic with ordinary overdispersion still fires falsely, so trial one muted.

A muted monitor runs as usual, but its alerts are written as already delivered, so a polling
client never sees them. Toggling it changes nothing about its state.

Alerts carry only names and numbers. Delivery is a client polling for undelivered alerts, so
agentwatch stores no URL or secret and calls nothing.
"""

from __future__ import annotations

import logging
import math
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import duckdb

from agentwatch import drift
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

THRESHOLD = "threshold"
DRIFT = "drift"
MODES = (THRESHOLD, DRIFT)

ABOVE = "above"
BELOW = "below"
COMPARISONS = (ABOVE, BELOW)

OK = "OK"
FIRING = "FIRING"
# Kinds of alert.
FIRED = "firing"
RESOLVED = "resolved"
# Why a resolved alert came from an edit, not from the metric. It then has no value.
CHANGED = "changed"
REMOVED = "removed"

MINUTE_NS = 60 * 1_000_000_000
MAX_WINDOW_MINUTES = 30 * 24 * 60
MAX_MIN_SAMPLES = 100_000
MAX_NAME = 200
# Error rates are proportions. The Wilson bound already guards them at low volume, so they need
# fewer samples by default than a metric compared by its point value.
ERROR_RATES = (MODEL_ERROR_RATE, TOOL_ERROR_RATE)
RATE_MIN_SAMPLES = 3
DEFAULT_MIN_SAMPLES = 10
DEFAULT_COOLDOWN_MINUTES = 60
# Controls, format and bidi characters, and line breaks: a name is shown wherever an alert is
# delivered, and must not be able to fake a new line or reorder what is around it.
_REFUSED_IN_NAME = ("Cc", "Cf", "Cs", "Zl", "Zp")
# Guards against re-testing overlapping windows: see the module's docstring. A 99% interval and
# two breaches fired about once a month on simulated traffic with no change; docs/design.md has
# the figures. The resamples leave five draws beyond each end of the interval.
DRIFT_INTERVAL_NS = 60 * MINUTE_NS
DRIFT_CONFIDENCE = 0.995
DRIFT_BREACHES = 6
DRIFT_RESAMPLES = 2000
# A longer gap means the breaches so far were about windows unrelated to the next one.
DRIFT_MAX_GAP_NS = 3 * DRIFT_INTERVAL_NS


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
    # How long the value must stay on the good side before a firing monitor resolves.
    cooldown_minutes: int
    enabled: bool
    created_ns: int
    # THRESHOLD compares with a level; DRIFT with the window directly before.
    mode: str = THRESHOLD
    # The length of that window; None in threshold mode.
    baseline_minutes: int | None = None
    # Evaluates as usual, but its alerts are written as already delivered.
    muted: bool = False


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
    # While firing: since when the value has been on the good side. Any bad evaluation clears it.
    good_since_ns: int | None = None
    # Drift only: the baseline window's value, and how many evaluations in a row have breached.
    baseline_value: float | None = None
    breaches: int = 0


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
    # None when an edit, not the metric, resolved it.
    value: float | None
    bound: float | None
    samples: int
    window_minutes: int
    created_ns: int
    delivered_ns: int | None
    # CHANGED or REMOVED when an edit resolved it; otherwise None.
    reason: str | None = None
    mode: str = THRESHOLD
    # Drift only: the two windows' values and the baseline's length. `bound` is then a difference.
    # A resolved alert means these windows no longer differ, not that the level has come back.
    baseline_value: float | None = None
    baseline_minutes: int | None = None
    # Its monitor was muted, so it was written as already delivered.
    muted: bool = False


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
    "cooldown_minutes, enabled, created_ns, mode, baseline_minutes, muted"
)
_STATE_COLUMNS = (
    "monitor_id, state, value, bound, samples, evaluated_ns, since_ns, insufficient, "
    "good_since_ns, baseline_value, breaches"
)
_ALERT_COLUMNS = (
    "id, monitor_id, monitor_name, kind, metric, comparison, threshold, value, bound, samples, "
    "window_minutes, created_ns, delivered_ns, reason, mode, baseline_value, "
    "baseline_minutes, muted"
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
    "cooldown_minutes",
    "enabled",
    "mode",
    "baseline_minutes",
    "muted",
)
# What the state's meaning depends on: a change to any of these starts the monitor again. A name
# or a cooldown changes nothing about what was measured.
_RULE_FIELDS = (
    "metric",
    "score_name",
    "service",
    "window_minutes",
    "comparison",
    "threshold",
    "min_samples",
    "enabled",
    "mode",
    "baseline_minutes",
)


# --- Definitions -------------------------------------------------------------------------


def create_monitor(store: Store, body: Any, *, now_ns: int | None = None) -> Monitor:
    data = _known(body)
    defaults: dict[str, Any] = {
        "min_samples": (
            RATE_MIN_SAMPLES if data.get("metric") in ERROR_RATES else DEFAULT_MIN_SAMPLES
        ),
        # Long enough that a drift monitor cannot resolve and re-fire on one shift.
        "cooldown_minutes": (
            data.get("window_minutes") if data.get("mode") == DRIFT else DEFAULT_COOLDOWN_MINUTES
        ),
        "enabled": True,
    }
    created = time.time_ns() if now_ns is None else now_ns
    monitor = _validated(defaults | data, monitor_id=uuid.uuid4().hex, created_ns=created)
    with store.transaction() as db:
        _check_name_free(db, monitor.name)
        db.execute(
            f"INSERT INTO monitors ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                monitor.cooldown_minutes,
                monitor.enabled,
                monitor.created_ns,
                monitor.mode,
                monitor.baseline_minutes,
                monitor.muted,
            ],
        )
    return monitor


def update_monitor(
    store: Store, monitor_id: str, body: Any, *, now_ns: int | None = None
) -> Monitor:
    """Applies the fields given. A change to the rule, or switching the monitor on or off, sends
    it back to OK to re-evaluate, since its state described the old rule or a time it was off. If
    it was firing, that first writes a resolved alert, so its last word is not "still firing"."""
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
            "comparison = ?, threshold = ?, min_samples = ?, cooldown_minutes = ?, enabled = ?, "
            "mode = ?, baseline_minutes = ?, muted = ? WHERE id = ?",
            [
                monitor.metric,
                monitor.score_name,
                monitor.service,
                monitor.window_minutes,
                monitor.comparison,
                monitor.threshold,
                monitor.min_samples,
                monitor.cooldown_minutes,
                monitor.enabled,
                monitor.mode,
                monitor.baseline_minutes,
                monitor.muted,
                monitor.id,
            ],
        )
        if any(getattr(monitor, f) != getattr(current, f) for f in _RULE_FIELDS):
            # The alert describes the rule that was firing, under the name it was firing as.
            _end_state(db, current, CHANGED, time.time_ns() if now_ns is None else now_ns)
    return monitor


def delete_monitor(store: Store, monitor_id: str, *, now_ns: int | None = None) -> None:
    """Deletes the monitor and its state, after a resolved alert if it was firing. Its alerts
    stay."""
    with store.transaction() as db:
        current = _get(db, monitor_id)
        db.execute("DELETE FROM monitors WHERE id = ?", [monitor_id])
        _end_state(db, current, REMOVED, time.time_ns() if now_ns is None else now_ns)


def _end_state(db: duckdb.DuckDBPyConnection, monitor: Monitor, reason: str, now_ns: int) -> None:
    firing = db.execute(
        "SELECT 1 FROM monitor_states WHERE monitor_id = ? AND state = ?", [monitor.id, FIRING]
    ).fetchone()
    if firing:
        _write_alert(db, monitor, RESOLVED, now_ns, reason=reason)
    db.execute("DELETE FROM monitor_states WHERE monitor_id = ?", [monitor.id])


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
    name = _name(fields.get("name"))
    mode = fields.get("mode", THRESHOLD)
    if mode not in MODES:
        raise MonitorError(f"mode must be one of {', '.join(MODES)}")
    metric = fields.get("metric")
    allowed = drift.METRICS if mode == DRIFT else METRICS
    if not isinstance(metric, str) or metric not in allowed:
        raise MonitorError(f"metric must be one of {', '.join(allowed)}")
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
    window = _whole(fields.get("window_minutes"), MAX_WINDOW_MINUTES, "window_minutes")
    baseline = None
    if mode == DRIFT:
        given = fields.get("baseline_minutes")
        baseline = _whole(
            window if given is None else given, MAX_WINDOW_MINUTES, "baseline_minutes"
        )
    threshold = _threshold(fields.get("threshold"), metric)
    if mode == DRIFT and threshold < 0:
        raise MonitorError("a drift monitor's threshold is the smallest change, so at least 0")
    muted = fields.get("muted", False)
    if not isinstance(muted, bool):
        raise MonitorError("muted must be true or false")
    return Monitor(
        id=monitor_id,
        name=name,
        metric=metric,
        score_name=score_name,
        service=None if service in (None, "") else _text(service, "service"),
        window_minutes=window,
        comparison=comparison,
        threshold=threshold,
        min_samples=_whole(fields.get("min_samples"), MAX_MIN_SAMPLES, "min_samples"),
        cooldown_minutes=_whole(
            fields.get("cooldown_minutes"), MAX_WINDOW_MINUTES, "cooldown_minutes", minimum=0
        ),
        enabled=enabled,
        created_ns=created_ns,
        mode=mode,
        baseline_minutes=baseline,
        muted=muted,
    )


def _threshold(value: Any, metric: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MonitorError("threshold must be a finite number")
    try:
        number = float(value)
    except OverflowError as error:  # JSON allows an integer too large for a float
        raise MonitorError("threshold must be a finite number") from error
    if not math.isfinite(number):
        raise MonitorError("threshold must be a finite number")
    if metric in ERROR_RATES and not 0 <= number <= 1:
        raise MonitorError("an error rate's threshold must be from 0 to 1: 0.05 means 5%")
    return number


def _whole(value: Any, maximum: int, field: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise MonitorError(f"{field} must be a whole number from {minimum} to {maximum}")
    return value


def _name(value: Any) -> str:
    # Checked before _text strips the ends, so a trailing newline is refused, not tidied away.
    if isinstance(value, str) and any(
        unicodedata.category(character) in _REFUSED_IN_NAME for character in value
    ):
        raise MonitorError("name must not contain control or formatting characters")
    return _text(value, "name")


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


def next_state(
    monitor: Monitor, reading: Reading, state: str, good_since: int | None, now_ns: int
) -> tuple[str, int | None]:
    """The state after a comparable reading, and since when a firing monitor has been good.

    Firing needs the bound (for a rate) past the threshold. Recovery looks only at the point
    value, which must stay on the good side for the cooldown; one bad reading starts it again.
    """
    assert reading.value is not None
    if state == OK:
        compared = reading.value if reading.bound is None else reading.bound
        return (FIRING if _past(monitor, compared) else OK), None
    if _past(monitor, reading.value):
        return FIRING, None
    good_since = now_ns if good_since is None else good_since
    if now_ns - good_since >= monitor.cooldown_minutes * MINUTE_NS:
        return OK, None
    return FIRING, good_since


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
    if monitor.mode == DRIFT:
        return _evaluate_drift(store, monitor, now_ns)
    reading = read(store, monitor, now_ns)
    with store.transaction() as db:
        row = db.execute(f"SELECT {_COLUMNS} FROM monitors WHERE id = ?", [monitor.id]).fetchone()
        if row is None or Monitor(*row) != monitor:
            return None  # deleted or changed during the read, which no longer applies
        stored = db.execute(
            "SELECT state, since_ns, good_since_ns, evaluated_ns FROM monitor_states "
            "WHERE monitor_id = ?",
            [monitor.id],
        ).fetchone()
        state: str = OK if stored is None else stored[0]
        since: int = now_ns if stored is None else stored[1]
        good_since: int | None = None if stored is None else stored[2]
        insufficient = reading.value is None or (
            reading.needs_samples and reading.samples < monitor.min_samples
        )
        if insufficient:
            # No evidence either way: moved forward by the time passed, so the good time built
            # up is kept but silence adds none, and a rate cannot resolve because calls stopped.
            following = state
            if stored is not None and good_since is not None:
                good_since += max(0, now_ns - stored[3])
        else:
            following, good_since = next_state(monitor, reading, state, good_since, now_ns)
        if following != state:
            since = now_ns
            _write_alert(
                db,
                monitor,
                FIRED if following == FIRING else RESOLVED,
                now_ns,
                value=reading.value,
                bound=reading.bound,
                samples=reading.samples,
            )
        result = MonitorState(
            monitor_id=monitor.id,
            state=following,
            value=reading.value,
            bound=reading.bound,
            samples=reading.samples,
            evaluated_ns=now_ns,
            since_ns=since,
            insufficient=insufficient,
            good_since_ns=good_since,
        )
        _write_state(db, result)
    return result


def _write_state(db: duckdb.DuckDBPyConnection, state: MonitorState) -> None:
    db.execute(
        f"INSERT OR REPLACE INTO monitor_states ({_STATE_COLUMNS}) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            state.monitor_id,
            state.state,
            state.value,
            state.bound,
            state.samples,
            state.evaluated_ns,
            state.since_ns,
            state.insufficient,
            state.good_since_ns,
            state.baseline_value,
            state.breaches,
        ],
    )


def drift_breach(monitor: Monitor, result: drift.Comparison) -> bool:
    """A change, in the bad direction, of at least the smallest change that matters."""
    bad = drift.HIGHER if monitor.comparison == ABOVE else drift.LOWER
    return (
        result.verdict == drift.CHANGED
        and result.direction == bad
        and result.difference is not None
        and abs(result.difference) >= monitor.threshold
    )


def next_drift_state(
    monitor: Monitor,
    result: drift.Comparison,
    state: str,
    breaches: int,
    good_since: int | None,
    last_evaluated: int | None,
    now_ns: int,
) -> tuple[str, int, int | None]:
    """The state, breaches in a row and good-since time after a drift evaluation.

    An underpowered evaluation is no evidence either way, so it neither fires nor recovers: it
    moves good-since forward, so that its time does not count toward the cooldown, and ends a run
    of breaches, as does a gap of over DRIFT_MAX_GAP_NS, since "in a row" needs related windows.
    """
    if last_evaluated is not None and now_ns - last_evaluated > DRIFT_MAX_GAP_NS and state == OK:
        breaches = 0
    if result.verdict == drift.UNDERPOWERED:
        if good_since is not None and last_evaluated is not None:
            good_since += max(0, now_ns - last_evaluated)
        return state, 0 if state == OK else breaches, good_since
    breaching = drift_breach(monitor, result)
    if state == OK:
        breaches = breaches + 1 if breaching else 0
        return (FIRING if breaches >= DRIFT_BREACHES else OK), breaches, None
    if breaching:
        return FIRING, breaches, None
    good_since = now_ns if good_since is None else good_since
    if now_ns - good_since >= monitor.cooldown_minutes * MINUTE_NS:
        return OK, 0, None
    return FIRING, breaches, good_since


def _near_edge(monitor: Monitor, result: drift.Comparison) -> float | None:
    """The end of the difference's interval nearest no change, on the bad side."""
    if result.interval is None:
        return None
    return result.interval[0] if monitor.comparison == ABOVE else result.interval[1]


def _evaluate_drift(store: Store, monitor: Monitor, now_ns: int) -> MonitorState | None:
    """As `evaluate`, and None too when the last evaluation is under an hour old. The queries
    each hold the store's lock for themselves, and the bootstrap holds it not at all."""
    last = get_state(store, monitor.id).evaluated_ns
    if last is not None and now_ns - last < DRIFT_INTERVAL_NS:
        return None
    split = now_ns - monitor.window_minutes * MINUTE_NS
    length = (
        monitor.window_minutes if monitor.baseline_minutes is None else monitor.baseline_minutes
    )
    result = drift.compare(
        store,
        monitor.metric,
        baseline=(split - length * MINUTE_NS, split),
        current=(split, now_ns),
        service=monitor.service,
        score_name=monitor.score_name,
        confidence=DRIFT_CONFIDENCE,
        resamples=DRIFT_RESAMPLES,
    )
    with store.transaction() as db:
        row = db.execute(f"SELECT {_COLUMNS} FROM monitors WHERE id = ?", [monitor.id]).fetchone()
        if row is None or Monitor(*row) != monitor:
            return None
        stored = db.execute(
            "SELECT state, since_ns, good_since_ns, evaluated_ns, breaches FROM monitor_states "
            "WHERE monitor_id = ?",
            [monitor.id],
        ).fetchone()
        state: str = OK if stored is None else stored[0]
        since: int = now_ns if stored is None else stored[1]
        following, breaches, good_since = next_drift_state(
            monitor,
            result,
            state,
            0 if stored is None else stored[4],
            None if stored is None else stored[2],
            None if stored is None else stored[3],
            now_ns,
        )
        bound = _near_edge(monitor, result)
        if following != state:
            since = now_ns
            _write_alert(
                db,
                monitor,
                FIRED if following == FIRING else RESOLVED,
                now_ns,
                value=result.current_value,
                bound=bound,
                samples=result.current_samples,
                baseline_value=result.baseline_value,
            )
        written = MonitorState(
            monitor_id=monitor.id,
            state=following,
            value=result.current_value,
            bound=bound,
            samples=result.current_samples,
            evaluated_ns=now_ns,
            since_ns=since,
            insufficient=result.verdict == drift.UNDERPOWERED,
            good_since_ns=good_since,
            baseline_value=result.baseline_value,
            breaches=breaches,
        )
        _write_state(db, written)
    return written


def _write_alert(
    db: duckdb.DuckDBPyConnection,
    monitor: Monitor,
    kind: str,
    now_ns: int,
    *,
    value: float | None = None,
    bound: float | None = None,
    samples: int = 0,
    reason: str | None = None,
    baseline_value: float | None = None,
) -> None:
    db.execute(
        f"INSERT INTO alerts ({_ALERT_COLUMNS}) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            uuid.uuid4().hex,
            monitor.id,
            monitor.name,
            kind,
            monitor.metric,
            monitor.comparison,
            monitor.threshold,
            value,
            bound,
            samples,
            monitor.window_minutes,
            now_ns,
            now_ns if monitor.muted else None,
            reason,
            monitor.mode,
            baseline_value,
            monitor.baseline_minutes,
            monitor.muted,
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
        """One pass over every enabled monitor; returns how many were evaluated. A drift monitor
        not yet due is not, nor is one changed during its evaluation."""
        evaluated = 0
        for monitor in list_monitors(self.store):
            if not monitor.enabled or self._stop.is_set():
                continue
            try:
                state = evaluate(self.store, monitor, self.clock())
            except Exception:  # one bad monitor must not stop the others
                log.exception("monitor %s failed", monitor.id)
            else:
                evaluated += 0 if state is None else 1
        return evaluated
