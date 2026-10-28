# agentwatch

Local tracing for LLM agents. agentwatch receives OpenTelemetry traces over OTLP/HTTP, reads the
[GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions-genai) off each
span, stores everything in a local DuckDB file and shows it in a web UI: a trace list with tokens,
cache share and cost per trace, and a span waterfall for each trace.

It runs on one machine for one person. There is no authentication, so it binds to `127.0.0.1` by
default.

> Status: early development (phase 1 of the plan in
> [chief-agent#323](https://github.com/akhilvuputuri/chief-agent/issues/323): tracing in). Cost and
> latency dashboards, the statistics engine and eval comparison come next.

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

| Attribute | Used for |
| --- | --- |
| `gen_ai.operation.name` | `chat`, `text_completion`, `generate_content` and `embeddings` count as model calls; `execute_tool` as tool calls; `invoke_agent` draws an agent span |
| `gen_ai.provider.name` (or legacy `gen_ai.system`) | provider |
| `gen_ai.request.model`, `gen_ai.response.model` | model |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | tokens (legacy `prompt_tokens`/`completion_tokens` accepted) |
| `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_write.input_tokens` | cache share (legacy `cache_creation` accepted) |
| `gen_ai.usage.reasoning.output_tokens` | reasoning tokens |
| `gen_ai.response.finish_reasons`, `error.type` | finish and error mix |
| `gen_ai.tool.name`, `gen_ai.agent.name` | labels |
| `agentwatch.usage.cost_usd` | cost in US dollars (the conventions define no cost attribute) |

Trace totals add up tokens and cost from every span except `invoke_agent`, `create_agent` and
`invoke_workflow`, so an agent span that repeats its children's usage is not counted twice. See
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
