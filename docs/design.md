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
rebinds its own domain to 127.0.0.1 therefore cannot read or write traces. When a request
carries an `Origin`, its port must also match the `Host` header's, so a page served from another
local port (another dev server) cannot write either.

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

Re-derivation writes its scratch file next to the database file, not in the system temp
directory, which can be a small in-memory file system.

Two version numbers in `meta` keep stored data current:

- `schema`: the table layout. A new database is created at version 1 and brought up to date
  by the same `MIGRATIONS` an old one runs, each step in its own transaction, so both end up
  identical. A database from a newer agentwatch refuses to open.
- `derivation`: the extraction rules (`genai.DERIVATION_VERSION`). When it changes, stored spans
  are read again at startup so new rules (such as accepting OpenInference) apply to data received
  before them. One scan fetches only the attribute keys the rules use (`genai.attribute_keys()`,
  via `json_extract`), so full prompts stay in DuckDB; Python handles 5,000 rows at a time and
  writes the new values to a newline-delimited JSON file, which one `UPDATE … FROM read_json`
  applies to the rows whose values changed, in one transaction. On a laptop, 200,000 spans with
  4 KB of attributes each took about 11 s when every row changed and about 3 s when none did.
  Startup logs the span count first, and the server does not accept traces until it finishes.

Ingest derives the columns from the attributes in their stored JSON form (bytes as hex, NaN as
`"nan"`), so a re-derivation gives exactly the values ingest did.

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
- `GET /api/traces/<trace_id>`: every span, with the extracted `genai` fields, plus the trace's
  session, user and scores.
- `GET /api/traces?session=<id>&user=<id>`: the same list, filtered to one session or user.
- `GET /api/sessions?limit=50&before=<cursor>&service=<name>&user=<id>`: sessions with the most
  recent trace first; `next` is `<last trace start_ns>:<session_id>`.
- `GET /api/sessions/<session_id>`: the session's totals and its traces, oldest first (the most
  recent 500 if longer, with `truncated`). Session IDs may contain `/`.
- `GET /api/metrics?from=<ns>&to=<ns>&service=<name>` (or `range=all`; the default is the last 24
  hours): the dashboard's figures, below.
- `GET /api/services` (root services, the values the list filters on), `GET /healthz`.

## Datasets

A dataset (schema version 5) is a named collection of items, as in Langfuse: each item has an
`input` and optional `expected_output` (any JSON), metadata, a status (`ACTIVE` or `ARCHIVED`) and,
when it was made from a trace, the trace and span it came from. Items are archived rather than
deleted so that experiments that used them stay readable; deleting a whole dataset removes its
items.

- `GET`/`POST /api/datasets` (names are unique; 409 on a duplicate), `GET`/`DELETE
  /api/datasets/<id>`.
- `GET /api/datasets/<id>/items[?archived=1]`; `POST` one item or `{"items": [...]}` (up to
  1,000, all or none). An item with a caller-chosen `id` (URL-safe, as for scores) replaces the
  same item in place, keeping its creation time and status; an `id` owned by another dataset is a
  409. Each item is limited to 512 KiB of JSON.
- `POST /api/datasets/<id>/items/from-trace` with `trace_id` and optional `span_id` makes an item
  from a span's captured messages. Without `span_id` it uses the root span, or, when the root
  captured no input (agent frameworks usually capture messages on the model-call spans), the
  earliest span that did. The input comes from `gen_ai.input.messages`
  (the GenAI conventions' opt-in capture) or OpenInference `input.value`, expected output from
  `gen_ai.output.messages` or `output.value`, parsed when they hold JSON text. Spans without
  captured content give an item with a null input, still linked to the trace.
- `PATCH /api/dataset-items/<id>` with `{"status": "ARCHIVED" | "ACTIVE"}`; item IDs are unique
  across datasets, so the route needs no dataset.

All JSON writes (datasets, scores, prices) must be `application/json`, which forces a preflight
on any cross-origin browser request, and standard JSON: the `NaN` and `Infinity` tokens Python
would accept are refused with 400, since a stored value that cannot be returned would break every
listing that includes it. Absurdly deep nesting is a 400 as well.

## Experiment runs

A run (schema version 6) runs a dataset through a task, as Langfuse dataset runs and Braintrust
experiments do. It belongs to one dataset, has a unique name there, and optional description and
metadata (model, prompt version, commit). A run item records, for one dataset item, the task's
output (any standard JSON, up to 512 KiB) or error, its start and end, and the trace that holds it.

- `POST /api/datasets/<id>/runs` creates a run (409 on a repeated name); `GET` lists the dataset's
  runs with, per run: items, errors, mean latency, cost and tokens over the items' traces, and
  the mean and count of each score name on those traces.
- `POST /api/runs/<id>/items` records one run item or `{"items": [...]}` in one transaction, all
  or none (the run and its dataset are re-checked inside it). Each needs `item_id` (an item of the
  run's dataset), and may carry `output`, `error`, `start_ns`, `end_ns` (not more than a day in
  the future), `trace_id` (not shared with another item of the run) and `scores` (as for
  `/api/scores`, without `trace_id`; stored with source `EVAL`). Recording an item again replaces
  its earlier result; if that result had a synthetic trace, the trace and its scores are removed,
  so a re-run is not counted twice anywhere.
- When `trace_id` is given, the caller traced the task itself and its spans arrive by OTLP.
  Otherwise the server records a one-span trace for the item (service `agentwatch.experiment`,
  with the item's input and output as `input.value` and `output.value`), so every result has a
  trace for its scores and for the trace list.
- `GET /api/runs/<id>` returns the run and its items with their dataset input and expected
  output and their scores; `DELETE` removes the run and its run items, keeping their traces and
  scores as trace data.

`agentwatch.client.run_experiment` is a runner that uses only the standard library: it creates a
run, calls the task on each active item (in a thread pool), applies evaluators to successful
outputs, and posts each result. An evaluator receives `(input, output, expected_output)` and
returns a number, boolean, label, score dict or list of those; its function name names the score.
A task's exception is the item's error. An evaluator's exception, or a score the server would
refuse (non-finite, nameless, a name over 200 characters), is reported in `evaluator_errors` and
skipped; a result the server refuses (say, an output over 512 KiB) is reported in `failed_posts`;
neither stops the run, and the score means count only accepted scores. An unreachable server or a
non-JSON reply raises `AgentwatchError` with the URL.

Deleting a dataset also removes its runs and run items; their traces and scores stay as trace
data. Removing a replaced item's synthetic trace removes every score on it, including any human
annotation added in the UI. A caller that re-records an item with the same `trace_id` it traced
itself keeps that trace's earlier scores, so it should replace (same score `id`) rather than re-add
them.

## Comparing runs

`GET /api/compare?baseline=<run>&candidate=<run>[&margin=0.05][&relative_margin=0.05]` compares
two runs of the same dataset item by item. Only items both runs recorded are paired. The
statistics (`stats.py`) are dependency-free and deterministic, and every comparison ends in a
verdict rather than a bare difference. Two margins say what size of change matters: `margin`, in
pass rate (absolute, 0.05 = 5 points), for boolean scores and task success; `relative_margin`, as a
share of the baseline mean, for numeric scores and latency, never below 0.01 (1 ms for latency).

- **better / worse**: unlikely to be noise. Boolean scores use an exact McNemar test on the items
  that flipped (5% level). Numeric scores and latency use a 95% paired *t* interval of the mean
  difference, which must exclude zero, and need at least 10 pairs: the percentile bootstrap used
  first gave 9–13% false positives at 5–10 pairs. A significant change smaller than its margin is
  still better or worse, flagged `below_margin` ("real but smaller than the change that matters").
- **no detectable difference**: not significant, at least 10 pairs, and the minimum detectable
  effect (the smallest true change detected 80% of the time at the 5% level) is within the margin.
  For numbers it is (1.96 + 0.84) × the standard deviation of the paired differences / √n. For
  pass/fail it uses the upper Wilson bound of the share of items that flipped instead of the
  observed spread, so ten items that agree completely cannot claim "no difference" (they could
  not have detected a 5-point change); about 300 agreeing items can.
- **underpowered**: anything else: too few pairs to rule out a change that matters.

Each comparison also reports both sides' rates (with Wilson intervals) or means, the difference
and its interval, how many items improved or regressed, and the minimum detectable effect. Task
success (no error) and latency (lower is better) are always compared. An item whose task failed
counts as failing every boolean score the other run's item has, so a crash never hides behind a
missing score; numeric scores cannot be filled in that way, so those pairs are left out and
counted in `dropped_failures` on the card. Several numeric scores of one name on an item are
averaged; several boolean ones pass only if all pass; a name used with both types is compared as
numeric. Label-only scores are not compared.

The item list shows both runs' outputs and scores side by side, with each item's changes: a
boolean that flipped, or a numeric score that moved by more than the margin. The UI's compare page
shows a card per comparison with its verdict (icon and label) and, by default, only the items that
changed. Runs are chosen as baseline and candidate on the dataset page.

## Statistics shared with Chief

`stats.py` and `scoring.py` replace Chief's `evals/decisions/stats.py` and `scoring.py` (Wilson,
exact McNemar, percentile and cluster bootstraps with a Wilson fallback, nearest-rank percentiles,
and per-call scoring helpers). Parity was checked on 29 October 2026 by re-scoring Chief's two
stored decision reports (`continuity` and `routing`, Chief `0c3039c`) offline, with no model
calls: once with Chief's own modules, which reproduced both reports exactly, and once with
agentwatch's modules standing in for them. Every figure matched except eight Wilson interval
bounds, which move by at most 0.00017 (a `calls_saved_per_100` bound) and otherwise by less than
0.00001, because agentwatch uses the exact 97.5th normal percentile (1.959964) instead of 1.96.
No verdict, point estimate, p-value or bootstrap interval changed. Wilson bounds at 0 and n
successes are exactly 0 and 1. One more intended difference does not show in those reports:
`call_stats` ignores a latency or cost given as a boolean, where Chief's counted `True` as 1.

Chief keeps its own copies until agentwatch is published: Chief installs Python dependencies
with `--require-hashes`, which a git-pinned package cannot satisfy. At the first PyPI release
(issue #3), Chief pins the hashed package and deletes its copies.

Wilson and McNemar come from Chief's evaluation code. Seeded simulations in `tests/test_stats.py`
check that Wilson intervals cover the true rate about 95% of the time; that McNemar, and the
numeric test at 10, 20 and 40 pairs, call a difference at most about 5–7% of the time when there
is none; that a clear 25-point improvement is detected; and that a real 10-point regression on 10
or 20 items is almost never reported as "no detectable difference".

## Model prices

When a model call reports tokens but no cost, its cost is calculated from a model price, as with
Langfuse's model definitions. A price (schema version 4) has a regular expression matched against
the whole model name (response model, else request model) case-insensitively, prices in US dollars
per million tokens for input, output, cache reads and cache writes (cache prices default to the
input price), and an optional time it takes effect. Of the prices in effect at the call's start
whose pattern matches, the longest (most specific) pattern wins; among that pattern's prices, the
one that took effect latest, then the most recently added. So `gpt-4o-mini.*` keeps applying to
mini calls when a generic `gpt-4o.*` is added, dated or not. To record a price change over time,
add a dated price with the same pattern:

cost = (input − cache read − cache write) × input price + cache read × cache-read price
+ cache write × cache-write price + output × output price, per million.

Input tokens include cached ones, and output tokens include reasoning, as the GenAI conventions
and OpenInference define them. When cache reads and writes together exceed the input count, the
instrumentation reported fresh input only (some Anthropic instrumentations do), so the input count
is charged in full as fresh input. Each span records `cost_source`: `reported` when the exporter sent a
cost (never changed), `calculated` when a price applied, or none. Matching runs in DuckDB's regular
expressions once per distinct model name (50 prices over 80,000 calls in 40 models recalculate in
about 0.03 s), for new spans at ingest and for every model call whenever a price is added, replaced
or deleted, or the extraction rules change, so calculated costs always reflect the current prices.
No prices are built in; `GET`/`POST /api/prices` and `DELETE /api/prices/<id>` manage them, and the
Prices page in the UI does the same. A pattern DuckDB cannot compile is refused with 400.

## Scores

A score is a named judgement on a trace, optionally on one of its spans: numeric (any finite
number), boolean (stored as 1 or 0) or categorical (a label), with a source: `API`, `ANNOTATION`
(added in the UI) or `EVAL` (from an evaluator). Scores live in their own table (schema version 3)
and may arrive before the trace they refer to.

They come in three ways:

- `POST /api/scores` with one score object or `{"scores": [...]}` (up to 1,000, all validated before
  any is stored). Fields: `trace_id`, optional `span_id`, `name`, `value` (number or boolean) and/or
  `label`, optional `comment`, `data_type` and `id`. The body must be `application/json`. A
  caller's `id` is 1 to 200 letters, digits or `. _ : -` (not `.` or `..`), so every score can be
  deleted by URL. IDs are global: one with an existing `id` replaces it if it is on the same
  trace, and the request fails with 409 if that `id` is used on another trace. The response
  returns the stored scores. The UI posts with `?source=annotation`. `DELETE /api/scores/<id>`
  removes one; the UI asks first, and warns that an evaluator's score returns if its event is
  sent again.
- `gen_ai.evaluation.result` events, the GenAI conventions' way of reporting an evaluation, sent
  as OTLP log records to `POST /v1/logs`. The event name is read from the record's `event_name`
  field or an `event.name` attribute; `gen_ai.evaluation.name`, `.score.value`, `.score.label` and
  `.explanation` become the name, value, label and comment, and the record's trace and span IDs
  attach it. Other log records are ignored, and evaluation records without a trace ID (the
  conventions allow correlating by `gen_ai.response.id` instead, which agentwatch does not
  support) are rejected through OTLP partial success.
- The same events attached to a span as span events, read when the span is stored.

An event's score ID is a hash of its trace, span, name, time and result, so a re-sent event or
span keeps one copy. An event whose ID is already used on another trace (only possible if an API
caller chose that ID) is skipped: on `/v1/logs` it counts as rejected in the partial-success reply. A span re-sent with a changed or removed evaluation event keeps the scores
from the earlier copy. An all-zero span ID on a log record means no span. Scores record when they
were received, which is the time the dashboard groups them by.

Within a trace, scores of one name are summarised as their count, the mean of their values, and
their commonest label (ties go to the alphabetically first). A name used with mixed types mixes
their values in one mean. The trace list shows each trace's scores per name (count, mean, commonest
label); the trace view lists them with a form to add and delete annotations; the dashboard
summarises scores recorded in the window by name (count, mean, minimum, maximum, labels, sources).

## LLM judges

A judge asks a model a question about each sampled new trace and stores the answer as an `EVAL`
score named after the judge, on the trace's root span (score ID `judge.<judge id>.<trace id>`, so
a re-judged trace keeps one score). It is online evaluation in the sense other tools use: it runs
as traces arrive, not over a dataset.

- **Definition** (`judges` table, schema version 7): score name, model, an OpenAI-compatible base
  URL, the name of the environment variable that holds its API key, a question with `{input}`
  and `{output}` placeholders, a score type (`BOOLEAN` or `NUMERIC` from 0 to 1), a sample rate,
  a limit of model calls per 24 hours, an optional root service filter, and whether it is on. The
  name and score type are fixed for life; everything else can change. A new judge is off.
  `sessions_only` (schema version 8) limits it to traces in a session, that is with a
  `gen_ai.conversation.id`, so background work that answers no one is not judged.
  Switching it off, deleting it or lowering its limit takes effect before its next model call,
  even in the middle of a pass.
- **What it sees.** The trace's captured input and output: the root span's
  `gen_ai.input.messages` / `gen_ai.output.messages` (or OpenInference `input.value` /
  `output.value`), else the earliest captured input and the latest captured output. Each is cut
  to 20,000 characters. A trace with no captured output is recorded as `NO_CONTENT` and never
  sent, since judging it would be blind.
- **Which traces.** Those whose root starts after the judge was last switched on (switching a
  judge on never judges the traces from before, including those that arrived while it was off), that have a span without a parent, and that have received no span for 30
  seconds. The background runner looks at up to 20 per judge every 30 seconds, oldest first.
- **Sampling.** A stable draw per judge and trace (a SHA-256 of both IDs), so a restart samples
  the same traces. Unsampled traces are recorded as `NOT_SAMPLED`.
- **Limit.** At most `daily_limit` model calls in any 24 hours, counting failures. Each call is
  recorded as `PENDING` before it is made, so a call whose result is lost (a crash, a reply that
  breaks the HTTP client) still counts and its trace is not called again. Sampled traces beyond
  the limit are recorded as `OVER_LIMIT` and not judged later, so a busy day never turns into a
  backlog of paid calls. Trial calls (below) are made on request and do not count.
- **The call.** System message: grade the work from the transcript and reply with only
  `{"score": …, "explanation": "…"}`. User message: the question with the placeholders filled in.
  Temperature 0, at most 400 output tokens, a 60-second timeout. The first `{` to the last `}` of
  the reply is parsed, so a fenced or chatty reply still works. A score of the wrong type, or
  outside 0 to 1, is a failure. The reply's `usage.prompt_tokens`, `completion_tokens` and
  `cost` are recorded per judgement; a judge whose endpoint is `openrouter.ai` asks for
  usage accounting (`"usage": {"include": true}`), without which OpenRouter reports no cost. Redirects are not followed (one is
  a failure), so the key cannot be sent to another host or over plain HTTP.
- **Record** (`judgements` table, one row per judge and trace): outcome (`SCORED`, `FAILED`,
  `PENDING`, `NOT_SAMPLED`, `NO_CONTENT`, `OVER_LIMIT`), value, explanation or failure reason,
  tokens, cost. A `SCORED` judgement and its score are written in one transaction; if the score's
  ID is already used on another trace, the judgement fails instead. Deleting a judge deletes its
  judgements and keeps its scores.
- **Keys.** The key is read from the named variable when a call is made, and is never stored or
  returned. Only variables named `…_API_KEY` are accepted, so a judge cannot be pointed at an
  unrelated secret in the server's environment. When the variable is unset, the judge pauses
  (its problem is shown) and its traces wait. The base URL must be `https`, or `http` on this
  machine (for a local model server), and must not contain credentials.

**Server policy.** The API has no login, so whoever can reach it could otherwise point a judge
at their own HTTPS server and receive the key it sends, or raise its daily limit to spend it.
`agentwatch serve --judge-endpoint URL --judge-key-env NAME --judge-max-daily N` (the first
two repeatable) restricts every judge to those endpoints, those key variables and
at most that limit. The policy is checked when a judge is created or changed, before each
pass (a judge outside it is skipped with its reason as its problem, and never calls), and in
`ask` itself, so trial calls obey it too. Without the flags, any HTTPS endpoint, any
`…_API_KEY` variable and up to 10,000 calls a day are allowed. `--judge-key-env` is refused
without `--judge-endpoint`, since a pinned key could otherwise still be sent anywhere, and
endpoints are checked and normalised at start-up as judges' base URLs are.

API: `GET/POST /api/judges`, `GET/PATCH/DELETE /api/judges/<id>` (each judge with its last 24
hours of outcome counts, calls, cost and any problem), `GET /api/judges/<id>/judgements?limit=`,
and `POST /api/judges/<id>/try` with `{"trace_id"}`, which judges one trace now and returns the
verdict without storing it (one model call, not counted toward the limit, allowed while the judge
is off; 502 when the model endpoint fails). The UI's Judges
page creates judges switched off, asks before switching one on, and lists recent judgements.

Judging is the one feature that sends trace content off the machine, to the endpoint the owner
configured. Trace content is untrusted and can try to argue the judge into a verdict, so judge
scores are evidence to read alongside others, not ground truth. Whether a judge agrees with
people has not been measured; a judge's question should be checked on a few traces with the
trial endpoint before it is switched on.

## Retention and the server image

`agentwatch serve --retain-days N` (1 to 36,500) deletes, at start and every 6 hours, every
trace whose last span arrived more than N days ago, with its scores and judgements
(`Store.prune`). Age is the server's arrival time, not the exporter's span times, so a client
with a wrong clock cannot get new traces deleted. A trace with any recent span is kept whole.
Traces and scores of experiment runs are kept, since their runs still show them. Scores and
judgements older than the cutoff whose trace no longer exists, or never arrived, are deleted
too; a recent score whose trace has not arrived yet is kept. DuckDB reuses
the freed space rather than shrinking the file. Without the flag, nothing is deleted.

`--memory-limit` sets DuckDB's `memory_limit` (it spills to disk beyond it), for a shared host.
The value is checked against a size pattern, and must be at least 64MB, before it is used.

On shutdown the server waits at most 3 seconds each for an in-flight judge call or prune, so it
stops within a container's default 10-second grace period; the database is closed only if both
have finished, and DuckDB replays its log at the next start otherwise.

The image (`Dockerfile`) builds the UI in a Node stage and installs the locked dependencies with
uv into a Python 3.13 slim image; all three base images are pinned by digest. It runs as UID
10001, keeps the database in the `/data` volume, has a health check on `/healthz`, and starts
`agentwatch serve --host 0.0.0.0`. The image workflow builds it on every pull request and smoke-
tests it: health, OTLP ingest, the UI, the non-root user, and that data survives a restart. On
`main`, a separate job, the only one with permission to write packages, loads that same tested
image and publishes it to GHCR as `sha-<commit>` and `main`, recording the digest.

## Sessions and users

A trace's session is the `gen_ai.conversation.id` (or OpenInference `session.id`) of its root span,
or of its earliest span that has one when the root does not; its user is found the same way from
`user.id`. Sessions are computed at query time from the per-trace summaries, so a session's totals
are sums over its traces and its time range runs from its first trace's start to its last trace's
end. Traces with no session do not appear in the session list. With a `service` or `user` filter,
the list sums only the matching traces of each session, while the session page always shows all
of them.

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
