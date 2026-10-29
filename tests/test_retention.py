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
    db.insert([span(1, old), span(2, new), span(3, kept_for_run)], received_ns=old)
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
    db.insert([span(1, NOW - 100 * DAY_NS), late], received_ns=NOW)
    assert db.prune(NOW - 90 * DAY_NS) == 0


@pytest.mark.parametrize("value", ["256MB", "1GiB", "512 MB"])
def test_memory_limits(value: str) -> None:
    Store(memory_limit=value).close()


@pytest.mark.parametrize("value", ["256", "lots", "1GB'; DROP TABLE spans; --"])
def test_bad_memory_limits_are_refused(value: str) -> None:
    with pytest.raises(ValueError, match="memory limit"):
        Store(memory_limit=value)


def test_retention_needs_a_day() -> None:
    with pytest.raises(ValueError, match="at least one day"):
        Retention(Store(), 0)
    with pytest.raises(SystemExit):
        cli.main(["serve", "--retain-days", "0"])
