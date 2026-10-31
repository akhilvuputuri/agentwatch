# agentwatch

Local tracing for LLM agents. agentwatch receives OpenTelemetry traces over OTLP/HTTP, reads the
[GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions-genai) off each
span, stores everything in a local DuckDB file and shows it in a web UI: a trace list with tokens,
cache share and cost per trace, a span waterfall for each trace, sessions (traces grouped by
conversation) and users, scores (from an API, OTel `gen_ai.evaluation.result` events, or added by
hand), datasets (items written by hand, through the API, or made from any trace), experiment runs
over datasets, LLM judges that score a sample of new traces, a dashboard of cost, tokens, cache
share and latency over time and by model, tool and agent, and monitors that raise alerts when a
recent metric crosses a threshold.

It runs on one machine for one person. There is no authentication, so it binds to `127.0.0.1` by
default and refuses requests addressed to any other host name, which blocks DNS-rebinding and
cross-origin browser requests. To reach it from another machine, bind with `--host` and name the
address clients will use with `--allow-host`.

> Status: early development, following the plan in
> [chief-agent#323](https://github.com/akhilvuputuri/chief-agent/issues/323). Tracing, dashboards,
> scores, datasets, experiments with paired statistics, LLM judges, and monitors with alerts work.
> Not yet published to PyPI.

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
compressed. Evaluation results sent as OTLP logs (`/v1/logs`) become scores; scores can also be
posted to `/api/scores`.

## What it reads from a span

Each field is read from the OpenTelemetry GenAI conventions first, then from
[OpenInference](https://github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md)
(Phoenix and its instrumentations), then from OpenLLMetry (Traceloop), so spans from any of the
three show up the same way.

Within a row, names are tried left to right: current GenAI names, then deprecated GenAI names
(listed in the GenAI column after a semicolon), then OpenInference, then OpenLLMetry.

| Field | GenAI | OpenInference | OpenLLMetry |
| --- | --- | --- | --- |
| Operation | `gen_ai.operation.name` | `openinference.span.kind` (`LLM` → chat, `TOOL` → execute_tool, `AGENT` → invoke_agent, `CHAIN` → invoke_workflow, `EMBEDDING`, `RETRIEVER`; others lowercased) | `llm.request.type`, `traceloop.span.kind` |
| Provider | `gen_ai.provider.name`; `gen_ai.system` | `llm.provider`, `llm.system` | |
| Model | `gen_ai.response.model`, `gen_ai.request.model` | `llm.response.model_name`, `llm.model_name` (response), `llm.request.model_name` | |
| Input / output tokens | `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`; `gen_ai.usage.prompt_tokens`, `gen_ai.usage.completion_tokens` | `llm.token_count.prompt`, `llm.token_count.completion` | `llm.usage.prompt_tokens`, `llm.usage.completion_tokens` |
| Cache read / write | `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_write.input_tokens`; `gen_ai.usage.cache_creation.input_tokens` | `llm.token_count.prompt_details.cache_read`, `…cache_write` | |
| Reasoning tokens | `gen_ai.usage.reasoning.output_tokens` | `llm.token_count.completion_details.reasoning` | |
| Cost (USD) | none in the conventions; `agentwatch.usage.cost_usd` | `llm.cost.total` | |

When a model call carries tokens but no cost, agentwatch calculates one from the model prices you
add on the Prices page (regular-expression model match, US dollars per million tokens, optional
effective date). No prices are built in.
| Finish reasons | `gen_ai.response.finish_reasons` | `llm.finish_reason` | |
| Tool, agent | `gen_ai.tool.name`, `gen_ai.agent.name` | `tool.name`, `agent.name` | |
| Session, user | `gen_ai.conversation.id`, `user.id` | `session.id`, `user.id` | |
| Error | `error.type` | | |

`chat`, `completion`, `text_completion`, `generate_content` and `embeddings` count as model calls,
as do spans with no operation that name a model and report tokens. OpenInference `CHAIN` spans,
like `invoke_workflow` and `invoke_agent`, wrap other spans, so their cost is not added to trace
totals; a trace whose only priced span is a chain shows no cost.

Trace token totals add up model calls only. Cost adds up every span except `invoke_agent`,
`create_agent` and `invoke_workflow`, so an agent span that repeats its children's cost is not
counted twice; when a model call reports no cost, the trace's cost is shown as a lower bound. See
[docs/design.md](docs/design.md) for the storage model and API.

### In a container

Each commit to `main` publishes an image to `ghcr.io/akhilvuputuri/agentwatch` (tags `main` and
`sha-<commit>`; pin the digest the image workflow prints). It runs as a non-root user and keeps
its database in `/data`:

```bash
docker run -d --name agentwatch -p 127.0.0.1:4318:4318 -v agentwatch-data:/data \
  ghcr.io/akhilvuputuri/agentwatch:main serve --host 0.0.0.0 --retain-days 90 --memory-limit 256MB
```

Other containers that send to it by name must be allowed with `--allow-host <name>`.
`--retain-days` deletes traces that ended more than that many days ago (checked at start and
every 6 hours); without it, nothing is deleted. `--memory-limit` caps DuckDB's memory.

## Experiments

Run a dataset through your agent and score the results:

```python
from agentwatch.client import run_experiment


def answer(question):  # the task under test
    return my_agent(question)


def exact(question, output, expected):  # a score named "exact"
    return output == expected


result = run_experiment(dataset="Demo requests", name="prompt v2", task=answer, evaluators=[exact])
print(result.scores)  # {"exact": 0.83}
```

Each run appears on its dataset's page with per-item outputs, scores and traces. Choose two runs
to compare them: each score gets a verdict (better, worse, no detectable difference, or
underpowered) from paired statistics, with the items that changed listed side by side.

## Judges

A judge asks a model about each sampled new trace and stores the answer as a score. Add one on
the Judges page or with `POST /api/judges`: a score name, a model, any OpenAI-compatible endpoint
(OpenRouter, OpenAI, or a local server such as Ollama), the environment variable that holds the
key, a question about `{input}` and `{output}`, a sample rate and a limit of calls a day. Judges
start switched off, see only traces that arrive after they were switched on, and skip traces whose
messages were not captured. Judging sends those messages to the endpoint you chose; nothing
else leaves the machine. On a shared machine, pin what judges may do with `serve --judge-endpoint`,
`--judge-key-env` and `--judge-max-daily`. See [the design notes](docs/design.md#llm-judges).

## Monitors

A monitor watches a metric over a trailing window: model or tool error rate, cost, p95 model
latency, trace count, or the mean of a score. Add one on the Alerts page or with
`POST /api/monitors`. Each one is evaluated every minute, and an alert is recorded when it starts
firing and when it resolves. Error rates fire only when a 95% statistical bound clears the
threshold, so a few unlucky calls cannot set one off, and a firing monitor resolves only after its
value has stayed good for a cooldown (60 minutes by default). agentwatch sends nothing itself: a client
polls `GET /api/alerts?undelivered=1` and confirms each alert with
`POST /api/alerts/<id>/delivered`. See [the design notes](docs/design.md#monitors-and-alerts).

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
traces stay in the local DuckDB file, except the captured messages of traces a judge you
switched on sends to its model endpoint. agentwatch only reads what exporters send; whether prompts
or tool arguments are included is the exporter's choice.

## Licence

MIT
