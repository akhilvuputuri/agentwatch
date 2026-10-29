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

`spans` keeps every span field, its attributes, resource, events and links as JSON, and the GenAI
fields extracted by `genai.read` as typed columns (operation, provider, models, token counts, cost,
finish reasons, tool and agent names, error type). `meta.schema` records the schema version; an
unknown version refuses to open rather than guessing.

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

`metrics.py` computes everything for the spans that *started* in the window (optionally of one
service). The figures are descriptive: counts, sums and percentiles (`quantile_cont`, so
interpolated), with no intervals or comparisons; those belong to the statistics engine.

- **Overview:** traces, model and tool calls and failures, token sums (model calls only), cost (as
  for trace totals) and model calls without cost, model-call latency p50/p95, and per-trace
  duration and cost p50/p95 and mean.
- **By model** (`response_model`, else `request_model`): calls, failures, tokens, cost, latency
  p50/p95/max, error types and finish reasons. Finish reasons come from a separate query, so a
  call reporting two reasons is counted once in `calls` and once under each reason.
- **By tool:** calls, failures, latency p50/p95/max, error types.
- **By agent:** `invoke_agent` runs, failures and duration p50/p95, plus the model and tool calls
  each agent made itself. A call belongs to its nearest `invoke_agent` ancestor (a recursive walk
  up the parents, at most 64 levels), so a parent's figures exclude its child agents'. Calls with
  no agent ancestor are grouped as `(no agent)`.
- **Series:** buckets of 1, 5 or 15 minutes, 1, 3, 6 or 12 hours, a day or a week; the smallest
  width giving at most 72 buckets. Buckets start on whole multiples of their width (UTC), and
  empty buckets are returned with zero counts and null cost and latency. A trace counts in the
  bucket where its root span started.

The UI shows these as stat tiles, a cost and a model-call bar chart and a p50/p95 latency line
chart (each with a data table), and the three breakdown tables. The page itself is served with
`Cache-Control: no-cache` and the hashed assets as immutable, so an upgraded install never keeps
loading an old bundle.

## UI

Hash-routed (`#/`, `#/trace/<id>`), so the static server needs no fallback routes. The waterfall
orders spans depth-first by start time, puts the true root first and shows spans whose parent
never arrived as extra roots marked "parent missing", and keeps spans caught in a parent cycle instead of dropping them.
Offsets are computed with `BigInt` to keep nanosecond precision.
