from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from starlette.testclient import TestClient

from agentwatch import genai, prices, server
from agentwatch.spans import Attributes, Span
from agentwatch.store import CALCULATED, REPORTED, Store

T = "e" * 32


def call(
    span_id: str, model: str, start: int = 1_000, *, cost: float | None = None, **tokens: int
) -> Span:
    attributes: Attributes = {genai.OPERATION: "chat", genai.RESPONSE_MODEL: model}
    names = {
        "input": genai.INPUT_TOKENS,
        "output": genai.OUTPUT_TOKENS,
        "cache_read": genai.CACHE_READ_TOKENS,
        "cache_write": genai.CACHE_WRITE_TOKENS,
    }
    for key, value in tokens.items():
        attributes[names[key]] = value
    if cost is not None:
        attributes[genai.COST_USD] = cost
    return Span(
        trace_id=T,
        span_id=span_id * 16,
        parent_span_id=None,
        name="chat",
        start_ns=start,
        end_ns=start + 1,
        attributes=attributes,
    )


def price(pattern: str, input_: float, output: float, **extra: float | int | str) -> prices.Price:
    return prices.from_api(
        {
            "match_pattern": pattern,
            "input_per_million": input_,
            "output_per_million": output,
            **extra,
        },
        now_ns=1,
    )


def costs(db: Store) -> dict[str, tuple[float | None, str | None]]:
    return {s.span.span_id[0]: (s.cost_usd, s.cost_source) for s in db.trace(T)}


def test_calculated_cost_splits_cache_reads_and_writes() -> None:
    db = Store()
    db.insert(
        [
            call(
                "1",
                "Vendor/Model-X",
                input=1_000_000,
                cache_read=600_000,
                cache_write=100_000,
                output=10_000,
            )
        ],
        1,
    )
    assert costs(db)["1"] == (None, None)
    db.upsert_price(
        price("vendor/model-x", 3.0, 15.0, cache_read_per_million=0.3, cache_write_per_million=3.75)
    )
    # 300k fresh x $3 + 600k cached x $0.30 + 100k written x $3.75 + 10k output x $15, per million.
    cost, source = costs(db)["1"]
    assert source == CALCULATED
    assert cost == pytest.approx(0.9 + 0.18 + 0.375 + 0.15)
    [summary] = db.traces()
    assert summary.cost_usd == pytest.approx(cost)
    assert summary.calls_without_cost == 0


def test_cache_prices_default_to_the_input_price() -> None:
    db = Store()
    db.insert([call("1", "m", input=1000, cache_read=400, output=0)], 1)
    db.upsert_price(price("m", 2.0, 0.0))
    assert costs(db)["1"][0] == pytest.approx(1000 * 2.0 / 1e6)


def test_reported_costs_win_and_unmatched_calls_stay_unpriced() -> None:
    db = Store()
    db.upsert_price(price("m.*", 1.0, 1.0))
    db.insert(
        [
            call("1", "m1", input=10, cost=0.5),
            call("2", "other", input=10),
            call("3", "m2", input=10),
        ],
        1,
    )
    found = costs(db)
    assert found["1"] == (0.5, REPORTED)
    assert found["2"] == (None, None)
    assert found["3"][1] == CALCULATED  # priced at ingest, as the price already existed


def test_the_latest_price_in_effect_applies() -> None:
    db = Store()
    db.insert(
        [call("1", "m", start=100, input=1_000_000), call("2", "m", start=300, input=1_000_000)], 1
    )
    db.upsert_price(price("m", 1.0, 0.0, id="old"))
    db.upsert_price(price("m", 2.0, 0.0, id="new", start_ns=200))
    found = costs(db)
    assert found["1"][0] == pytest.approx(1.0)
    assert found["2"][0] == pytest.approx(2.0)
    assert db.delete_price("new")
    assert costs(db)["2"][0] == pytest.approx(1.0)
    assert db.delete_price("old")
    assert costs(db)["2"] == (None, None)
    assert not db.delete_price("old")


def test_patterns_match_the_whole_name_case_insensitively() -> None:
    db = Store()
    db.insert([call("1", "gpt-x-mini", input=1_000_000), call("2", "GPT-X", input=1_000_000)], 1)
    db.upsert_price(price("gpt-x", 1.0, 0.0))
    found = costs(db)
    assert found["1"] == (None, None)
    assert found["2"][0] == pytest.approx(1.0)


def test_rederivation_keeps_calculated_costs(tmp_path: Path) -> None:
    path = tmp_path / "db.duckdb"
    db = Store(path)
    db.insert([call("1", "m", input=1_000_000)], 1)
    db.upsert_price(price("m", 1.0, 0.0))
    db.close()
    raw = duckdb.connect(str(path))
    raw.execute("UPDATE meta SET value = '0' WHERE key = 'derivation'")
    raw.close()
    again = Store(path)
    assert costs(again)["1"] == (pytest.approx(1.0), CALCULATED)
    again.close()


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"input_per_million": 1, "output_per_million": 1}, "match_pattern"),
        ({"match_pattern": "m", "output_per_million": 1}, "input_per_million is required"),
        ({"match_pattern": "m", "input_per_million": -1, "output_per_million": 1}, "between"),
        ({"match_pattern": "m", "input_per_million": True, "output_per_million": 1}, "number"),
        (
            {"match_pattern": "m", "input_per_million": 1, "output_per_million": 10**400},
            "too large",
        ),
        (
            {
                "match_pattern": "m",
                "input_per_million": 1,
                "output_per_million": 1,
                "start_ns": "soon",
            },
            "start_ns",
        ),
        (
            {"match_pattern": "m", "input_per_million": 1, "output_per_million": 1, "id": "a/b"},
            "id must be",
        ),
    ],
)
def test_invalid_prices(body: dict[str, object], message: str) -> None:
    with pytest.raises(prices.PriceError, match=message):
        prices.from_api(body)


def test_price_api() -> None:
    db = Store()
    db.insert([call("1", "m", input=2_000_000)], 1)
    http = TestClient(server.create_app(db, allowed_hosts=("testserver",)))
    created = http.post(
        "/api/prices",
        json={"id": "p1", "match_pattern": "m", "input_per_million": 0.5, "output_per_million": 1},
    )
    assert created.status_code == 201
    assert http.get("/api/prices").json()["prices"][0]["id"] == "p1"
    span = http.get(f"/api/traces/{T}").json()["spans"][0]
    assert (span["genai"]["cost_usd"], span["genai"]["cost_source"]) == (
        pytest.approx(1.0),
        CALCULATED,
    )
    bad = http.post(
        "/api/prices", json={"match_pattern": "(", "input_per_million": 1, "output_per_million": 1}
    )
    assert bad.status_code == 400
    assert "regular expression" in bad.text
    assert (
        http.post("/api/prices", content=b"{}", headers={"content-type": "text/plain"}).status_code
        == 415
    )
    assert http.delete("/api/prices/p1").status_code == 204
    assert http.delete("/api/prices/p1").status_code == 404
    assert http.get(f"/api/traces/{T}").json()["spans"][0]["genai"]["cost_usd"] is None


def test_a_more_specific_pattern_wins_over_a_newer_generic_one() -> None:
    db = Store()
    db.insert(
        [call("1", "gpt-x-mini", input=1_000_000), call("2", "gpt-x-large", input=1_000_000)], 1
    )
    db.upsert_price(price("gpt-x-mini.*", 0.1, 0.0, id="mini"))
    db.upsert_price(price("gpt-x.*", 1.0, 0.0, id="generic"))
    found = costs(db)
    assert found["1"][0] == pytest.approx(0.1)
    assert found["2"][0] == pytest.approx(1.0)


def test_input_that_excludes_cached_tokens_is_charged_as_fresh() -> None:
    db = Store()
    # Input 100 with 10,000 cache reads: input must be fresh-only.
    db.insert([call("1", "m", input=100, cache_read=10_000, output=0)], 1)
    db.upsert_price(price("m", 3.0, 0.0, cache_read_per_million=0.3))
    assert costs(db)["1"][0] == pytest.approx((100 * 3.0 + 10_000 * 0.3) / 1e6)
