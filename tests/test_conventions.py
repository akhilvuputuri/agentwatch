from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from agentwatch import genai, store
from agentwatch.spans import Attributes, Span
from agentwatch.store import Store

T = "a" * 32


def span(attributes: Attributes, span_id: str = "1" * 16) -> Span:
    return Span(
        trace_id=T,
        span_id=span_id,
        parent_span_id=None,
        name="x",
        start_ns=0,
        end_ns=1,
        attributes=attributes,
    )


def test_openinference_llm_span() -> None:
    fields = genai.read(
        span(
            {
                "openinference.span.kind": "LLM",
                "llm.provider": "openai",
                "llm.model_name": "gpt-x",
                "llm.token_count.prompt": 120,
                "llm.token_count.completion": 30,
                "llm.token_count.prompt_details.cache_read": 100,
                "llm.token_count.prompt_details.cache_write": 7,
                "llm.token_count.completion_details.reasoning": 12,
                "llm.cost.total": 0.0066,
                "llm.finish_reason": "stop",
                "session.id": "s-1",
                "user.id": "u-1",
            }
        )
    )
    assert fields.operation == "chat"
    assert fields.is_model_call
    assert (fields.provider, fields.model) == ("openai", "gpt-x")
    assert (fields.input_tokens, fields.output_tokens) == (120, 30)
    assert (fields.cache_read_tokens, fields.cache_write_tokens, fields.reasoning_tokens) == (
        100,
        7,
        12,
    )
    assert fields.cost_usd == pytest.approx(0.0066)
    assert fields.finish_reasons == ("stop",)
    assert (fields.conversation_id, fields.user_id) == ("s-1", "u-1")


@pytest.mark.parametrize(
    ("kind", "operation"),
    [
        ("TOOL", "execute_tool"),
        ("AGENT", "invoke_agent"),
        ("CHAIN", "invoke_workflow"),
        ("EMBEDDING", "embeddings"),
        ("RETRIEVER", "retrieval"),
        ("GUARDRAIL", "guardrail"),
        ("llm", "chat"),
    ],
)
def test_openinference_span_kinds(kind: str, operation: str) -> None:
    assert genai.read(span({"openinference.span.kind": kind})).operation == operation


def test_openinference_tool_and_agent_names() -> None:
    assert (
        genai.read(span({"openinference.span.kind": "TOOL", "tool.name": "search"})).tool_name
        == "search"
    )
    assert (
        genai.read(span({"openinference.span.kind": "AGENT", "agent.name": "planner"})).agent_name
        == "planner"
    )


def test_openllmetry_spans() -> None:
    llm = genai.read(
        span(
            {
                "llm.request.type": "chat",
                "gen_ai.system": "anthropic",
                "gen_ai.request.model": "claude-x",
                "gen_ai.usage.prompt_tokens": 50,
                "gen_ai.usage.completion_tokens": 5,
            }
        )
    )
    assert (llm.operation, llm.provider, llm.input_tokens, llm.output_tokens) == (
        "chat",
        "anthropic",
        50,
        5,
    )
    assert genai.read(span({"traceloop.span.kind": "workflow"})).operation == "invoke_workflow"
    assert genai.read(span({"traceloop.span.kind": "task"})).operation is None
    assert genai.read(span({"llm.request.type": "embedding"})).operation == "embeddings"


def test_genai_names_win_and_invalid_values_fall_through() -> None:
    fields = genai.read(
        span(
            {
                "gen_ai.operation.name": "chat",
                "openinference.span.kind": "TOOL",
                "gen_ai.usage.input_tokens": "not a number",
                "llm.token_count.prompt": 9,
                "gen_ai.request.model": "",
                "llm.model_name": "fallback-model",
            }
        )
    )
    assert fields.operation == "chat"
    assert fields.input_tokens == 9
    # OpenInference's llm.model_name names the response model.
    assert (fields.request_model, fields.model) == (None, "fallback-model")


def test_openinference_spans_count_in_trace_totals() -> None:
    db = Store()
    db.insert(
        [
            span({"openinference.span.kind": "AGENT", "llm.cost.total": 9.0}, "1" * 16),
            Span(
                trace_id=T,
                span_id="2" * 16,
                parent_span_id="1" * 16,
                name="llm",
                start_ns=0,
                end_ns=1,
                attributes={
                    "openinference.span.kind": "LLM",
                    "llm.model_name": "m",
                    "llm.token_count.prompt": 10,
                    "llm.cost.total": 0.5,
                },
            ),
        ],
        received_ns=1,
    )
    [summary] = db.traces()
    assert (summary.model_calls, summary.input_tokens) == (1, 10)
    # The agent span's cost repeats its children's, so it is not added again.
    assert summary.cost_usd == pytest.approx(0.5)


def version_one_database(path: Path) -> None:
    """A database as agentwatch 0.1 left it: schema 1, rows derived by the old rules."""
    db = duckdb.connect(str(path))
    db.execute(store._SCHEMA)  # pyright: ignore[reportPrivateUsage]
    db.execute("INSERT INTO meta VALUES ('schema', '1')")
    columns = (
        "trace_id, span_id, parent_span_id, name, kind, start_ns, end_ns, status_code, status_message, "
        "service_name, scope_name, scope_version, attributes, resource, events, links, operation, "
        "finish_reasons, is_model_call, counts_toward_totals, received_ns"
    )
    db.execute(
        f"INSERT INTO spans ({columns}) VALUES (?, ?, NULL, 'llm', 1, 0, 1, 0, '', 'svc', '', '', ?, '{{}}', '[]', '[]', NULL, [], false, true, 1)",
        [
            T,
            "1" * 16,
            '{"openinference.span.kind": "LLM", "llm.model_name": "m", "llm.token_count.prompt": 40, "user.id": "u"}',
        ],
    )
    db.close()


def test_upgrading_a_version_one_database(tmp_path: Path) -> None:
    path = tmp_path / "old.duckdb"
    version_one_database(path)
    db = Store(path)
    [stored] = db.trace(T)
    assert stored.fields.operation == "chat"
    assert db.query_one("SELECT value FROM meta WHERE key = 'schema'")[0] == str(
        store.SCHEMA_VERSION
    )
    row = db.query_one("SELECT operation, input_tokens, is_model_call, user_id FROM spans")
    assert tuple(row) == ("chat", 40, True, "u")
    [summary] = db.traces()
    assert summary.model_calls == 1
    db.close()
    # A second open neither migrates nor re-derives again.
    again = Store(path)
    assert again.query_one("SELECT value FROM meta WHERE key = 'derivation'")[0] == str(
        genai.DERIVATION_VERSION
    )
    again.close()


def test_a_newer_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "new.duckdb"
    Store(path).close()
    db = duckdb.connect(str(path))
    db.execute("UPDATE meta SET value = '99' WHERE key = 'schema'")
    db.close()
    with pytest.raises(RuntimeError, match="newer"):
        Store(path)


def test_new_databases_match_migrated_ones(tmp_path: Path) -> None:
    fresh = Store(tmp_path / "fresh.duckdb")
    version_one_database(tmp_path / "old.duckdb")
    upgraded = Store(tmp_path / "old.duckdb")
    describe = "SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_name IN ('spans', 'scores') ORDER BY table_name, column_name"
    assert fresh.query(describe) == upgraded.query(describe)


def test_rederivation_reads_in_chunks_and_matches_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "db.duckdb"
    db = Store(path)
    spans = [
        span(
            {
                "openinference.span.kind": "LLM",
                "llm.model_name": "m",
                "llm.token_count.prompt": i,
                # Bytes and NaN change form when stored; both paths must agree.
                "session.id": bytes([i % 256, 1]),
                "llm.cost.total": float("nan") if i % 2 else 0.25,
            },
            f"{i + 1:016x}",
        )
        for i in range(23)
    ]
    db.insert(spans, received_ns=1)
    at_ingest = db.query(
        "SELECT span_id, conversation_id, input_tokens, cost_usd FROM spans ORDER BY span_id"
    )
    db.close()
    # Force a re-read, a few rows per chunk.
    raw = duckdb.connect(str(path))
    raw.execute("UPDATE meta SET value = '0' WHERE key = 'derivation'")
    raw.execute("UPDATE spans SET conversation_id = NULL, input_tokens = NULL, cost_usd = NULL")
    raw.close()
    monkeypatch.setattr(store, "_REDERIVE_CHUNK", 5)
    reopened = Store(path)
    after = reopened.query(
        "SELECT span_id, conversation_id, input_tokens, cost_usd FROM spans ORDER BY span_id"
    )
    assert after == at_ingest
    assert at_ingest[1][1] == "0101"
    reopened.close()
