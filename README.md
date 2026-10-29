# agentwatch

Local tracing for LLM agents. agentwatch receives OpenTelemetry traces over OTLP/HTTP, reads the
[GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions-genai) off each
span, stores everything in a local DuckDB file and shows it in a web UI: a trace list with tokens,
cache share and cost per trace, a span waterfall for each trace, and a dashboard of cost, tokens,
cache share and latency over time and by model, tool and agent.

It runs on one machine for one person. There is no authentication, so it binds to `127.0.0.1` by
default and refuses requests addressed to any other host name, which blocks DNS-rebinding and
cross-origin browser requests. To reach it from another machine, bind with `--host` and name the
address clients will use with `--allow-host`.

> Status: early development (phases 1–2 of the plan in
> [chief-agent#323](https://github.com/akhilvuputuri/chief-agent/issues/323): tracing in, cost and
> latency dashboards). The statistics engine and eval comparison come next.

## Run it

```bash
uvx agentwatch serve --demo        # synthetic demo data, in memory
uvx agentwatch serve               # real data in ~/.agentwatch/agentwatch.duckdb
```

Open <http://127.0.0.1:4318>. The same port accepts OTLP/HTTP exports at `/v1/traces`, so any
OpenTelemetry SDK works with its defaults:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
```

Both `application/x-protobuf` and `application/json` bodies are accepted, plain or gzip/deflate
compressed.

## What it reads from a span

Each field is read from the OpenTelemetry GenAI conventions first, then from
[OpenInference](https://github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md)
(Phoenix and its instrumentations), then from OpenLLMetry (Traceloop), so spans from any of the
three show up the same way.

| Field | GenAI | OpenInference | OpenLLMetry and older names |
| --- | --- | --- | --- |
| Operation | `gen_ai.operation.name` | `openinference.span.kind` (`LLM` → chat, `TOOL` → execute_tool, `AGENT` → invoke_agent, `CHAIN` → invoke_workflow, `EMBEDDING`, `RETRIEVER`; others lowercased) | `llm.request.type`, `traceloop.span.kind` |
| Provider | `gen_ai.provider.name` | `llm.provider`, `llm.system` | `gen_ai.system` |
| Model | `gen_ai.response.model`, `gen_ai.request.model` | `llm.response.model_name`, `llm.request.model_name`, `llm.model_name` | |
| Input / output tokens | `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | `llm.token_count.prompt`, `llm.token_count.completion` | `gen_ai.usage.prompt_tokens`, `gen_ai.usage.completion_tokens`, `llm.usage.*` |
| Cache read / write | `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_write.input_tokens` | `llm.token_count.prompt_details.cache_read`, `…cache_write` | `gen_ai.usage.cache_creation.input_tokens` |
| Reasoning tokens | `gen_ai.usage.reasoning.output_tokens` | `llm.token_count.completion_details.reasoning` | |
| Cost (USD) | none in the conventions; `agentwatch.usage.cost_usd` | `llm.cost.total` | |
| Finish reasons | `gen_ai.response.finish_reasons` | `llm.finish_reason` | |
| Tool, agent | `gen_ai.tool.name`, `gen_ai.agent.name` | `tool.name`, `agent.name` | |
| Session, user | `gen_ai.conversation.id`, `user.id` | `session.id`, `user.id` | |
| Error | `error.type` | | |

`chat`, `completion`, `text_completion`, `generate_content` and `embeddings` count as model calls,
as do spans with no operation that name a model and report tokens.

Trace token totals add up model calls only. Cost adds up every span except `invoke_agent`,
`create_agent` and `invoke_workflow`, so an agent span that repeats its children's cost is not
counted twice; when a model call reports no cost, the trace's cost is shown as a lower bound. See
[docs/design.md](docs/design.md) for the storage model and API.

## Develop

Python 3.13 with [uv](https://docs.astral.sh/uv/), Node 22.

```bash
uv sync
uv run pytest && uv run ruff check . && uv run pyright
cd ui && npm ci && npm run build && npm test && npm run e2e
```

`npm run dev` in `ui/` starts Vite with `/api` and `/v1` proxied to a server on port 4318.
`npm run build` writes the UI into `src/agentwatch/static`, which the wheel ships.

## Data and privacy

The demo dataset is generated (`src/agentwatch/demo.py`) and contains no real conversations. Real
traces stay in the local DuckDB file. agentwatch only reads what exporters send; whether prompts
or tool arguments are included is the exporter's choice.

## Licence

MIT
