# Design

## Shape

One Python process (Starlette on uvicorn) serves three things on one port:

- `POST /v1/traces`: OTLP/HTTP trace ingest.
- `GET /api/...`: a small JSON read API for the UI.
- `/`: the built React UI, from `src/agentwatch/static`.

Storage is one DuckDB file. A single connection is shared behind a lock. Decoding and database
work run in the threadpool, so the event loop stays free while a batch is stored. Inserts use one
multi-row statement per 200 spans, about eight times faster than `executemany` (5,000 spans in
about 0.9 s instead of 7.7 s on an M-series laptop).

A host guard refuses requests whose `Host` (or, when present, `Origin`) names a host other than
the loopback names, the bound address and any `--allow-host` names, with 403. A web page that
rebinds its own domain to 127.0.0.1 therefore cannot read or write traces.

## Ingest

`otlp.decode` turns a request body into `Span` records:

1. Decompress `gzip` or `deflate`, refusing more than 32 MiB decompressed and truncated streams.
2. Protobuf bodies parse directly. JSON bodies follow OTLP/JSON, which writes trace and span IDs
   as hex; they are rewritten to base64 and then parsed by protobuf's JSON mapping, so both
   encodings share one code path. Unknown JSON fields are ignored, as OTLP requires.
3. Spans with malformed or all-zero IDs, an undefined kind or status code, or timestamps above
   2^63-1 ns, are skipped and
   reported through OTLP partial success (`rejected_spans`, `error_message`); the rest are stored.
   An all-zero parent ID means no parent.

Concatenated gzip members are all decompressed. An unsupported content type gets 415, an
undecodable body 400, and requests over 16 MiB 413 before decoding. Error bodies are plain text,
not `google.rpc.Status`. Counts above 2^63-1 and non-finite or negative costs are ignored rather
than stored; non-finite attribute values are kept as the strings `nan`, `inf` and `-inf`. A span sent twice (same trace
and span ID) replaces the earlier copy, so exporter retries are harmless.

## Storage

`spans` keeps every span field, its attributes, resource, events and links as JSON, and the fields
extracted by `genai.read` as typed columns (operation, provider, models, token counts, cost, finish
reasons, tool and agent names, session and user, error type).

`genai.read` takes each field from the GenAI conventions, then OpenInference, then OpenLLMetry, and
moves to the next name when a value is present but unusable (for example a token count that is not
a number). The README lists the names.

Two version numbers in `meta` keep stored data current:

- `schema`: the table layout. A new database is created at version 1 and brought up to date
  by the same `MIGRATIONS` an old one runs, each step in its own transaction, so both end up
  identical. A database from a newer agentwatch refuses to open.
- `derivation`: the extraction rules (`genai.DERIVATION_VERSION`). When it changes, every stored
  span's attributes are read again and the extracted columns rewritten, in one transaction, so
  new rules (such as accepting OpenInference) apply to data received before them.

Trace summaries are computed at query time:

- start and duration from the earliest start and latest end;
- the root is chosen among spans whose parent is absent from the trace: a span with no parent at
  all wins over an orphan, then the earliest; a trace is `complete` when that root has no parent;
- tokens sum over model calls; cost sums over spans whose operation is not `invoke_agent`,
  `create_agent` or `invoke_workflow`, and is null when no span reported one;
  `calls_without_cost` counts model calls with no cost, in which case the cost is a lower bound;
- model calls count `chat`, `text_completion`, `generate_content`, `embeddings` and `completion`,
  plus spans with no operation that name a model and report tokens (older instrumentations).

## API

Absolute timestamps (`start_ns`, `end_ns`, event `time_ns`) are Unix nanoseconds sent as decimal
strings, because they exceed JavaScript's exact integer range. Durations are numbers.

- `GET /api/traces?limit=50&before=<cursor>&service=<root service>&errors=1`: most recent first,
  ties ordered by trace ID. The response's `next` is the cursor (`<start_ns>:<trace_id>`) for the
  following page, or null.
- `GET /api/traces/<trace_id>`: every span, with the extracted `genai` fields.
- `GET /api/metrics?from=<ns>&to=<ns>&service=<name>` (or `range=all`; the default is the last 24
  hours): the dashboard's figures, below.
- `GET /api/services` (root services, the values the list filters on), `GET /healthz`.

## Dashboard metrics

`metrics.py` computes the dashboard's figures for a window and, optionally, a service. The service
filter selects traces by their root's service, the same rule as the trace list, so a trace's calls
made by other services are included. Within that, there are two populations:

- **Calls** (model and tool calls, tokens, cost, latency): spans that started in the window.
- **Traces** (count, duration and cost per trace): traces whose root started in the window, with
  all of their spans, including any that ran past its end. A trace's root is chosen as in the
  trace list, so a trace whose root never arrived counts where its earliest orphan started.

The figures are descriptive: counts, sums and interpolated percentiles (`quantile_cont`), with no
intervals or comparisons; those belong to the statistics engine. Durations are clamped at zero,
because an exporter can send an end before the start.

- **Overview:** traces; model and tool calls and failures; token sums (model calls only); cost (as
  for trace totals) and model calls without cost; model-call latency p50/p95; per-trace duration
  and cost p50/p95 and mean.
- **By model** (`response_model`, else `request_model`): calls, failures, tokens, cost, latency
  p50/p95/max, error types and finish reasons. Finish reasons come from a separate query, so a
  call reporting two reasons is counted once in `calls` and once under each reason. When one model
  name was served by several providers, the provider shown is any one of them.
- **By tool:** calls, failures, latency p50/p95/max, error types.
- **By agent:** `invoke_agent` runs, failures and duration p50/p95, plus the model calls, tool calls
  and other priced spans each agent made itself. A span belongs to its nearest `invoke_agent`
  ancestor (a recursive walk up the parents, at most 64 levels), so a parent's figures exclude its
  child agents'. Spans with no agent ancestor form one row flagged `unowned`, kept apart from any
  agent of the same name.
- **Series:** the smallest of 1, 5 or 15 minutes, 1, 3, 6 or 12 hours, a day or a week that gives at
  most 72 buckets; for windows longer than about 72 weeks, a whole number of weeks. Buckets start
  on whole multiples of their width since the Unix epoch, so day buckets are UTC days (the UI labels
  them in UTC) and week buckets start on Thursdays. Empty buckets are returned with zero counts and
  null cost and latency. Calls count in the bucket where they started, traces in the bucket where
  their root started.

`range=all` covers the earliest start to the latest start or end of the matching spans (so a span
with an end before its start is still included), capped at the largest timestamp, or the last day
when nothing is stored.

The UI shows these as stat tiles, a cost and a model-call bar chart and a p50/p95 latency line
chart (each with a data table), and the three breakdown tables. The page itself is served with
`Cache-Control: no-cache` and files under the URL path `assets/` (content-hashed names) as
immutable, so an upgraded install never keeps loading an old bundle. Hover readouts are hidden from
screen readers; each chart's data table carries the same values.

## UI

Hash-routed (`#/`, `#/trace/<id>`), so the static server needs no fallback routes. The waterfall
orders spans depth-first by start time, puts the true root first and shows spans whose parent
never arrived as extra roots marked "parent missing", and keeps spans caught in a parent cycle instead of dropping them.
Offsets are computed with `BigInt` to keep nanosecond precision.
