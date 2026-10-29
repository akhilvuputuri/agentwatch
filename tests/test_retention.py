from __future__ import annotations

import pytest

from agentwatch import cli
from agentwatch.retention import DAY_NS, Retention
from agentwatch.scores import API, NUMERIC, Score
from agentwatch.spans import Span
from agentwatch.store import Store

NOW = 1_000 * DAY_NS


def span(n: int, end_ns: int) -> Span:
    return Span(
        trace_id=f"{n:032x}",
        span_id="1" * 16,
        parent_span_id=None,
        name="agent",
        start_ns=end_ns - 1,
        end_ns=end_ns,
    )


def score(trace: int, created_ns: int, n: int = 0) -> Score:
    return Score(
        id=f"s{trace}-{n}",
        trace_id=f"{trace:032x}",
        span_id=None,
        name="q",
        data_type=NUMERIC,
        value=1.0,
        label=None,
        source=API,
        comment=None,
        created_ns=created_ns,
    )


def test_prune_deletes_old_traces_with_their_scores_and_judgements() -> None:
    db = Store()
    old, new, kept_for_run = NOW - 91 * DAY_NS, NOW - DAY_NS, NOW - 200 * DAY_NS
    for n, at in ((1, old), (2, new), (3, kept_for_run)):
        db.insert([span(n, at)], received_ns=at)
    db.upsert_scores([score(1, old), score(2, new), score(9, old), score(8, new)])
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO run_items VALUES ('r', 'i', ?, NULL, NULL, 0, 0, 0)", [f"{3:032x}"]
        )
        for trace in (1, 2):
            conn.execute(
                "INSERT INTO judgements (judge_id, trace_id, outcome, created_ns) "
                "VALUES ('j', ?, 'SCORED', ?)",
                [f"{trace:032x}", old if trace == 1 else new],
            )
    assert Retention(db, 90, clock=lambda: NOW).run_once() == 1
    traces = {row[0] for row in db.query("SELECT DISTINCT trace_id FROM spans")}
    assert traces == {f"{2:032x}", f"{3:032x}"}
    # The old trace's score and an old orphan go; a recent orphan may still meet its trace.
    assert {row[0] for row in db.query("SELECT id FROM scores")} == {"s2-0", "s8-0"}
    assert [row[0] for row in db.query("SELECT trace_id FROM judgements")] == [f"{2:032x}"]
    # Nothing more to do.
    assert Retention(db, 90, clock=lambda: NOW).run_once() == 0


def test_a_trace_is_kept_while_any_of_its_spans_is_recent() -> None:
    db = Store()
    late = Span(
        trace_id=f"{1:032x}",
        span_id="2" * 16,
        parent_span_id="1" * 16,
        name="late",
        start_ns=NOW - 1,
        end_ns=NOW,
    )
    db.insert([span(1, NOW - 100 * DAY_NS)], received_ns=NOW - 100 * DAY_NS)
    db.insert([late], received_ns=NOW)
    assert db.prune(NOW - 90 * DAY_NS) == 0


@pytest.mark.parametrize("value", ["256MB", "1GiB", "512 MB"])
def test_memory_limits(value: str) -> None:
    Store(memory_limit=value).close()


@pytest.mark.parametrize("value", ["256", "lots", "1GB'; DROP TABLE spans; --", "0MB", "10MB"])
def test_bad_memory_limits_are_refused(value: str) -> None:
    with pytest.raises(ValueError, match="memory limit must"):
        Store(memory_limit=value)


def test_retention_needs_a_day() -> None:
    with pytest.raises(ValueError, match="one day to 100 years"):
        Retention(Store(), 0)
    with pytest.raises(SystemExit):
        cli.main(["serve", "--retain-days", "200000"])
    with pytest.raises(SystemExit):
        cli.main(["serve", "--retain-days", "0"])


def test_age_is_the_arrival_time_not_the_exporters_clock() -> None:
    db = Store()
    # Microseconds sent as nanoseconds: the span claims 1970, but it arrived just now.
    db.insert([span(1, 1_700_000_000_000_000)], received_ns=NOW)
    assert db.prune(NOW - 90 * DAY_NS) == 0


def test_experiment_run_scores_are_kept_even_without_spans() -> None:
    db = Store()
    old = NOW - 200 * DAY_NS
    db.upsert_scores([score(5, old)])
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO run_items VALUES ('r', 'i', ?, NULL, NULL, 0, 0, 0)", [f"{5:032x}"]
        )
    db.prune(NOW - 90 * DAY_NS)
    assert [row[0] for row in db.query("SELECT id FROM scores")] == ["s5-0"]
