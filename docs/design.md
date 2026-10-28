# Design

## Shape

One Python process (Starlette on uvicorn) serves three things on one port:

- `POST /v1/traces`: OTLP/HTTP trace ingest.
- `GET /api/...`: a small JSON read API for the UI.
- `/`: the built React UI, from `src/agentwatch/static`.

Storage is one DuckDB file. A single connection is shared behind a lock; the expected load is one
agent sending a few spans per second, so there is no write queue.

## Ingest

`otlp.decode` turns a request body into `Span` records:

1. Decompress `gzip` or `deflate`, refusing more than 32 MiB decompressed and truncated streams.
2. Protobuf bodies parse directly. JSON bodies follow OTLP/JSON, which writes trace and span IDs
   as hex; they are rewritten to base64 and then parsed by protobuf's JSON mapping, so both
   encodings share one code path. Unknown JSON fields are ignored, as OTLP requires.
3. Spans with malformed or all-zero IDs fail the whole request with 400. An all-zero parent ID
   means no parent.

The server rejects requests over 16 MiB with 413 before decoding. A span sent twice (same trace
and span ID) replaces the earlier copy, so exporter retries are harmless.

## Storage

`spans` keeps every span field, its attributes, resource, events and links as JSON, and the GenAI
fields extracted by `genai.read` as typed columns (operation, provider, models, token counts, cost,
finish reasons, tool and agent names, error type). `meta.schema` records the schema version; an
unknown version refuses to open rather than guessing.

Trace summaries are computed at query time:

- start and duration from the earliest start and latest end;
- the root is the earliest span whose parent is absent from the trace; a trace is `complete` when
  that root has no parent at all;
- tokens and cost sum over spans whose operation is not `invoke_agent`, `create_agent` or
  `invoke_workflow`; cost is null when no span reported one;
- model calls count `chat`, `text_completion`, `generate_content`, `embeddings` and `completion`.

## API

Absolute timestamps (`start_ns`, `end_ns`, event `time_ns`) are Unix nanoseconds sent as decimal
strings, because they exceed JavaScript's exact integer range. Durations are numbers.

- `GET /api/traces?limit=50&before=<start_ns>&service=<name>&errors=1`: most recent first; page
  with the last trace's `start_ns` as `before`.
- `GET /api/traces/<trace_id>`: every span, with the extracted `genai` fields.
- `GET /api/services`, `GET /healthz`.

## UI

Hash-routed (`#/`, `#/trace/<id>`), so the static server needs no fallback routes. The waterfall
orders spans depth-first by start time, shows spans whose parent never arrived as extra roots
marked "parent missing", and keeps spans caught in a parent cycle instead of dropping them.
Offsets are computed with `BigInt` to keep nanosecond precision.
