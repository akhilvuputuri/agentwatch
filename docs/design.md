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
- `GET /api/auth/session` (`{"auth": bool, "user": name or null}`), `POST /api/auth/login`
  (the sign-in form), `POST /api/auth/logout` (JSON): see [signing in](#signing-in).

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

## Monitors and alerts

A monitor (`monitors` table, schema version 9) compares one metric over a trailing window with a
threshold: a unique name, the metric, the score name (for `score_mean` only), an optional root
service, a window of 1 to 43,200 minutes, `above` or `below`, a finite threshold (0 to 1 for the
error rates, where 0.05 means 5%), `min_samples` (1 to 100,000; by default 3 for the error rates,
since the Wilson bound already guards them at low volume, and 10 for the rest),
`cooldown_minutes` (0 to 43,200, default 60) and whether it is enabled (by default it is). A name
may not contain control, format or bidi characters (Unicode Cc and Cf, such as a newline or U+202E)
or line separators, since it is shown wherever an alert is delivered. The background runner
evaluates every enabled monitor each minute; a monitor that fails is logged, and the others go on.

This section describes `threshold` mode; [drift mode](#drift-detection) compares with an earlier
window instead.

**Metrics.** Windows are half-open and end now. Populations follow the dashboard's: spans that
started in the window, limited to traces whose root service matches.

| Metric | Value | Samples | Compared by |
| --- | --- | --- | --- |
| `model_error_rate` | failed model calls / model calls | model calls | Wilson bound |
| `tool_error_rate` | failed tool calls / tool calls | tool calls | Wilson bound |
| `cost_usd` | sum of cost (spans that count toward totals) | model calls; `min_samples` ignored | point value |
| `model_p95_ms` | 95th percentile of model call latency | model calls | point value |
| `trace_count` | traces whose root started in the window | always 1; `min_samples` ignored | point value |
| `score_mean` | mean of the named BOOLEAN or NUMERIC score recorded in the window, any source | scores | Wilson bound if all BOOLEAN, otherwise point value |

An error-rate window must be longer than the longest model call. A call counts from when it
started but arrives when it ends, so a call longer than the window has already left the window by
the time it arrives, and a failure that takes that long is missed.

`cost_usd` ignores `min_samples` because a cost spike must alert even from few calls, and
`trace_count` because a `below` monitor on it is how silence is caught. With fewer samples than
`min_samples`, a monitor's state stays as it is and the evaluation is recorded as `insufficient`,
including a firing monitor whose traffic stopped: nothing has shown that it recovered.

**The Wilson rule.** One failure in three calls is a 33% error rate, and almost no evidence of
anything: `min_samples` alone cannot tell it from 30 failures in 90. So a rate fires on the 95%
Wilson bound (the one in `stats`) on the threshold's side: `above` when the lower bound exceeds the
threshold, `below` when the upper bound is under it. At a threshold of 20%, 1 failure in 3 (lower
bound 6%) does not fire, and 30 in 50 (lower bound 46%) does; 5 in 5 (lower bound 57%) fires with
the default `min_samples`. Recovery compares the point value, so a monitor does not have to wait
for a bound to clear, which at low volume could take very long. A value exactly on the threshold
is on the good side. Non-rates compare the point value both ways.

**Cooldown.** A value that sits at its threshold would otherwise fire and resolve over and over:
a `trace_count` below 3 over 5 minutes, with one trace every other minute, wrote 17 alerts in 20
minutes. So a firing monitor, rate or not, stays firing until its point value has been on the good
side continuously for `cooldown_minutes` (`good_since_ns` in its state). Any evaluation on the bad
side clears that. An evaluation with too few samples neither clears nor advances it: the good time
built up is kept, and the time passed adds nothing (`good_since_ns` moves forward by it), so a
firing monitor does not resolve from silence on a rate. A cooldown of 0 resolves at once.

**State machine.** One `monitor_states` row per monitor: `OK` or `FIRING`, the last value, bound,
samples, evaluation time, when the state began, whether the evaluation was insufficient, and
since when a firing monitor has been good.
`OK` to `FIRING` writes a `firing` alert, `FIRING` to `OK` a `resolved` one, each in one
transaction with the state, so there is no alert while a monitor stays firing. The query runs
before that transaction, under the store's lock only for itself; a monitor deleted or changed
during its query discards the result.

A change to the metric, score name, service, window, comparison, threshold or minimum samples, or
switching a monitor off or on, deletes its state, so it starts from `OK` and re-evaluates. A
rename or a new cooldown keeps the state and writes no alert. If the monitor was firing, the reset
first writes one `resolved` alert in the same transaction, so what the owner last heard is not
"still firing": `reason` is `changed`, `value` is null, samples is 0, and the alert describes the
old rule under the old name. Deleting a firing monitor does the same with `reason` `removed`.
Deleting a monitor deletes its state and keeps its alerts.

**What an alert contains.** Names and numbers only: monitor ID and name (copied, so it outlives
the monitor), kind, metric, comparison, threshold, value, bound, samples, window, creation time,
delivery time and `reason` (null unless an edit resolved it, as above). No trace content. `Store.prune` deletes alerts older than its cutoff by creation
time, delivered or not; monitors and states are not affected.

**Delivery is polling.** There are no webhooks, so no URL or secret is ever stored and agentwatch
sends nothing. A client polls `GET /api/alerts?undelivered=1`, which lists the oldest first, acts
on each and confirms with `POST /api/alerts/<id>/delivered`. Confirming is idempotent and keeps the
first time, so a client that retries after a lost reply changes nothing; a client that crashes
before confirming sees the alert again, so delivery is at least once.

API: `GET/POST /api/monitors`, `GET/PATCH/DELETE /api/monitors/<id>` (each with its current state),
`GET /api/alerts?limit=&undelivered=1` (newest first unless `undelivered`) and
`POST /api/alerts/<id>/delivered` (404 if unknown; the body is ignored, but the request must say
`application/json` as every write does). The UI's Alerts page adds monitors, lists them with their
state, and lists recent alerts. Whether these thresholds and windows suit real traffic has not
been measured.

## Drift detection

Did a metric move between two time windows? (`drift.py`.) It is a different question from a
threshold's, and from a comparison of runs.

**Why unpaired and clustered.** Experiment runs pair up on dataset items; two windows hold
different traffic, so nothing pairs and `stats`' paired tests do not apply. Calls in one trace are
not independent either: a retry loop can fail fifty times for one reason. So a comparison reduces
each window to per-trace records and takes a seeded percentile bootstrap of (current - baseline),
resampling whole traces, independently in each window (1,000 resamples). In a test, 100 failures
in one trace among 20 are not a change, though the same failures as independent calls are. The same
seed and data give the same result.

**Metrics.** Scope and service filter follow the dashboard's. `model_error_rate` and
`tool_error_rate` pool failures / calls, for calls that started in the window; `model_mean_ms` and
`model_p95_ms` use the same calls (the percentile interpolated as DuckDB's `quantile_cont`, so it
matches the dashboard); `cost_per_trace` covers traces whose root started in the window and have a
known cost; `score_mean` the named BOOLEAN or NUMERIC scores recorded in the window. `model` narrows
every metric but `tool_error_rate` and `score_mean`, where it would mean nothing and is refused.
Each window keeps its most recent 20,000 traces (for a percentile, 5,000 traces, 20,000 calls and
200 calls a trace), and says `truncated` when more were left out. `trace_count` is not a drift
metric: a count has no traces to resample.

**What the verdict says.** `underpowered` when either window has fewer than 10 traces: values are
given, no interval and no claim. `changed` when the interval excludes zero. `no detectable
difference` otherwise, with `detectable`, a change this size would usually show, so it is not read
as "no change". A true change equal to the interval's half-width clears it under half the time, so
`detectable` is the half-width scaled by (z + 0.84) / z at the interval's level, which aims at 80%
power. Measured, a true change of that size showed 57 to 77% of the time (200 to 300 runs at rates of
0.02, 0.1 and 0.4 with 15 and 30 traces a side, about 3 points of noise), so read it as about 60 to 80%.
It is null, shown as "can't say", when a window has no spread at all (every trace alike), since the
bootstrap then has nothing to measure; a proportion at 0 or 1 still has its Wilson width. `direction`
is `higher` or `lower` only for `changed`. It never says better or worse, since only the caller knows which way is bad, and it does
not say why. When a proportion's window is exactly 0 or 1, every resample of it is the same, and the
bootstrap would see no uncertainty on that side; the interval then also covers a Newcombe difference
of Wilson intervals, counting each trace as one observation.

**Calibration, simulated.** Two alike windows of n traces each, 4,000 repeats, at a nominal 5%:

| False "changed" | n=10 | n=20 | n=30 | n=100 |
| --- | --- | --- | --- | --- |
| Error rate, independent failures | 6.6% | 6.5% | 6.6% | 5.2% |
| Error rate, failures bunched in traces | 4.5% | 7.6% | 7.2% | 5.5% |
| Mean latency, lognormal | 8.8% | 7.1% | 7.8% | 6.1% |
| p95 latency, lognormal | 4.3% | 4.3% | 4.2% | 3.6% |

So intervals run hot at small n, as percentile bootstraps do. `MIN_TRACES` is a floor below which
nothing is claimed, not a calibrated claim above it. These windows are alike by construction: real
windows differ in ways this does not model (below), which makes the true rate higher.

**Comparing around a change.** `GET /api/drift?metric=&score_name=&service=&model=&baseline_from=&baseline_to=&current_from=&current_to=`
compares one metric at 95%; the times are required, as Unix nanoseconds, and the windows may not
overlap. `GET /api/drift/summary?service=&days=7` compares the call metrics and up to 50 score names
for the last N days (1 to 365) with the N before, or over two windows given as above. It tests k
comparisons at once, so its intervals are Bonferroni-corrected to 1 - 0.05/k (with up to 2,000
resamples, which keeps a few draws in the tails up to about 10 comparisons; at the limit of 55 there
is about one, so those ends are rough); the response gives that `confidence` and the page says so.
That targets 5% across a page but does not reach it, because the intervals run hot: simulated pages
of 5 metrics with no true difference showed a false "changed" 12.8% of the time at 10 traces a side,
11.6% at 20 and 8.5% at 30, against roughly 30% uncorrected.

Each of these two endpoints runs one comparison at a time across both, and answers 429 with
`Retry-After` when busy, while the same normalised request is answered from a 30-second cache. The
page waits and retries. `GET /api/changes?service=&days=` lists when each model
and `service.version` resource attribute first appeared, newest first (up to 200); first means
across all stored data, by the exporter's span times, and `days` only limits which are listed. The
Drift page shows the summary and, for a change it lists, the same summary, with scores, for the same
length either side, shortened if the change is recent. It uses the change's own service when it has
one, and all that service's traffic, because a model's own traffic has no "before".

**Drift monitors.** A monitor with `mode` `drift` (schema version 10; existing monitors are
`threshold`) compares its trailing window with the `baseline_minutes` (default the window's length)
directly before. `comparison` names the bad direction, `threshold` is the smallest change that
matters (absolute, at least 0, and at most 1 for error rates), `min_samples` is unused, and
`cooldown_minutes` defaults to the window, since at 60 minutes one shift could fire twice. An
evaluation breaches when the verdict is `changed`, in the bad direction, and the difference is at
least the threshold. Underpowered evaluations neither fire nor recover and are recorded as
`insufficient`, and their time does not count toward the cooldown. A run of breaches ends at an
underpowered evaluation or a gap of over 3 hours since the last evaluation, so that breaches on
unrelated windows cannot combine.

Alerts add `mode`, `baseline_value` and `baseline_minutes` (null for threshold monitors); `value` is
the current value and `bound` the interval's edge on the threshold's side, so a difference, which
can be negative. A resolved drift alert means the monitor has seen no clear change of at least its
threshold in the bad direction, for its cooldown. It does not mean the level recovered, or even that
the windows agree: `value` and `baseline_value` are both there to read. They are alike if a shift
simply filled the two windows, and the baseline holds the incident if a brief one has moved into it.
Threshold monitors otherwise behave as before.

**Muted monitors.** A monitor of either mode with `muted` evaluates and records alerts as usual, but
they are written as already delivered and flagged `muted`, so a polling client never sees them.
Toggling it changes no state and no earlier alert. A resolved alert goes the way its firing alert
went, whatever the monitor is now: muting mid-incident still sends the resolve of what the client was
told, and unmuting mid-incident sends nothing until the next firing. The monitor's state records how
the firing alert went, so this holds if retention has removed that alert. This is how a drift monitor
should be trialled on real traffic, given the results below, before it can reach a phone.

**Load, measured in process on a laptop.** The bootstrap holds no store lock, but it is pure Python.
Timings on 200,000 spans: a summary about 9 to 21 s, `GET /api/changes` about 0.05 s, and a percentile
at a monitor's settings about 10 s at the row limit. At the default 5 ms interpreter switch interval
each step of an ingest request waited that long, so `serve` sets 0.5 ms. Ingest p50 with 8 clients
asking for the summary, through the ASGI app rather than a socket, was 21 ms with no clients, 21 ms
(p95 88 ms) when the cache answered them, and 65 ms (p95 143 ms) with the cache off, so that a
comparison was always running.

**Repeated-testing guards.** A monitor re-tests overlapping windows every hour, so a 95% interval
would be wrong far more than 5% of the time. It evaluates at most once an hour, uses a 99.5%
interval (2,000 resamples) and fires only on 6 breaches in a row, then recovers by the cooldown.
The first rule tried, a 99% interval and 2 breaches, fired 0.33 to 1.33 times a month falsely on the
two easiest traffic models, so it was tightened after a sweep of both settings. Consecutive hourly
windows share nearly all their traces, so even 6 in a row are not independent evidence, and longer
runs missed real shifts at short windows, which pass through in a few days.

**Simulated results, not measurements of real traffic.** From `tests/drift_simulation.py`: the
monitor's own rule over made-up traces, 5 to 20 a day, 1 to 8 calls each, base error rate 10%,
threshold 0.05, cooldown one window, evaluated hourly. No lasting change in the false-firing columns,
over 60 simulated 30-day months for 3-day windows and 40 for 7-day; a shift from 10% to 40% in the
detection columns, in 20 runs for 3-day windows and 10 for 7-day (20 for independent), watched for
two window lengths.

| Traffic | False firings a month, 7-day / 3-day | Shift detected, 7-day / 3-day | Median delay, 7-day / 3-day |
| --- | --- | --- | --- |
| Independent failures | 0.07 / 0.17 | 20 of 20 / 20 of 20 | 51 h / 30 h |
| Bunched in traces (1 in 5 at four times the rate, the rest at a quarter) | 0.20 / 0.15 | 10 of 10 / 9 of 20 | 80 h / 71 h |
| Shared within sessions of 1 to 7 traces | 0.75 / 1.83 | 10 of 10 / 16 of 20 | 64 h / 41 h |
| Daily rate lognormal, sigma 0.5 | 0.50 / 1.12 | 10 of 10 / 20 of 20 | 46 h / 34 h |
| Weekdays 8%, weekends 15% | 0.07 / 0.97 | 10 of 10 / 20 of 20 | 54 h / 22 h |
| A 6-hour incident at 60% about once a fortnight | 0.23 / 0.28 | 10 of 10 / 20 of 20 | 50 h / 33 h |

The easy rows are not what to expect. Real traffic has overdispersion: failures shared across
sessions and days, which a bootstrap at the level of traces only partly captures, because it treats
traces as independent. On such traffic this rule fires falsely about once a month or more, and more
with several monitors. Trial a drift monitor muted first, and read what it would have sent. A weekly
pattern hurts a 3-day window far more than a 7-day one, which always holds one of each day. A short
window can also miss a real shift among bunched failures. The rule was not tightened further for
these models: detection is already slow, and no setting tried removes the cause. Reproduce one row
with `uv run python -m tests.drift_simulation 7 sessions 40 10`, and the calibration with
`uv run python -m tests.drift_simulation calibration 4000`.

**Limits.** A difference between windows is not a cause. Weekly seasonality is the obvious false
cause: a window shorter than a week compares different days, so a weekday against a weekend reads as
a change. Prefer whole weeks where traffic has a weekly shape. A monitor reports a change, not a
level: once a shift has filled both windows, they agree again and it resolves while the metric stays
bad, which is what a threshold monitor is for. A slow drift can stay under the interval for ever. A
trace with calls on both sides of the split counts in both windows, which the bootstrap treats as
independent. Truncation favours the most recent traces of a window.

## Retention and the server image

`agentwatch serve --retain-days N` (1 to 36,500) deletes, at start and every 6 hours, every
trace whose last span arrived more than N days ago, with its scores and judgements, and every
alert created more than N days ago, delivered or not (`Store.prune`). Memory records are kept
([memory](#memory)). Age is the server's arrival time, not the exporter's span times, so a client
with a wrong clock cannot get new traces deleted. A trace with any recent span is kept whole.
Traces and scores of experiment runs are kept, since their runs still show them. Scores and
judgements older than the cutoff whose trace no longer exists, or never arrived, are deleted
too; a recent score whose trace has not arrived yet is kept. DuckDB reuses
the freed space rather than shrinking the file. Without the flag, nothing is deleted.

`--memory-limit` sets DuckDB's `memory_limit` (it spills to disk beyond it), for a shared host.
The value is checked against a size pattern, and must be at least 64MB, before it is used.

On shutdown the server waits at most 3 seconds each for an in-flight judge call, monitor query or
prune, so it stops within a container's default 10-second grace period; the database is closed only
if all have finished, and DuckDB replays its log at the next start otherwise.

The image (`Dockerfile`) builds the UI in a Node stage and installs the locked dependencies with
uv into a Python 3.13 slim image; all three base images are pinned by digest. It runs as UID
10001, keeps the database in the `/data` volume, has a health check on `/healthz`, and starts
`agentwatch serve --host 0.0.0.0`. The image workflow builds it on every pull request and smoke-
tests it: health, OTLP ingest, the UI, the non-root user, and that data survives a restart. On
`main`, a separate job, the only one with permission to write packages, loads that same tested
image and publishes it to GHCR as `sha-<commit>` and `main`, recording the digest.

## Signing in

Off unless `serve --auth-user NAME` is given (`src/agentwatch/auth.py`). Mainstream trace tools
build sign-in in; this follows their shape at one account's size, on the standard library's
primitives, with choices taken from OWASP's Password Storage, Session Management and
Authentication cheat sheets and the Auth Book (auth.pilcrowonpaper.com), not from any one tool.
Phoenix, read for comparison, hashes with PBKDF2 at 10,000 iterations and compares with `==`;
OWASP's floor for PBKDF2-SHA256 is 600,000, so it was not copied.

- **One account, no stored password.** The operator runs `agentwatch hash-password` and puts its
  output in an environment variable (`--password-hash-env`, default `AGENTWATCH_PASSWORD_HASH`).
  The hash is scrypt with N=2^15, r=8, p=3, one of OWASP's equivalent minimums, at 32 MiB a check
  (N=2^17 would take 128 MiB of a container capped at 512 MB). The format,
  `scrypt:N:r:p:salt:key` in unpadded base64url, has no `$`, so Compose and `.env` files keep it
  whole. A hash needing more than 64 MiB (128 x r x N), N below 2^14 or not a power of two, r
  outside 1 to 32 or p outside 1 to 16 is refused, and so is a password outside 12 to 1,024
  characters. An unknown user name is checked against a random decoy with the same parameters,
  so the time taken does not tell names apart, and the answer is the same "Wrong user name or
  password." either way.
- **Checks in turn.** One check runs at a time, on a thread of its own limiter, not the thread
  pool the API and ingest share, so a burst of guesses waits without holding the threads reads
  and ingest need. With 8 checks running or waiting, a sign-in gets 503 with `Retry-After: 5`,
  and is not counted against the limits.
- **Sessions.** Signing in makes a 256-bit token (`secrets.token_urlsafe(32)`) and keeps only its
  SHA-256, in memory, so signing out ends the session on the server and a restart ends all of
  them. A session lasts 7 days unused and 30 days in all; at most 64 are kept, the least recently
  used going first. Signing in again ends the session the browser came with. OWASP suggests idle
  limits of minutes for valuable data; this is a personal viewer opened from a phone, so it
  follows Langfuse's 30-day default instead, and the limits are constants to shorten if that
  changes.
- **The cookie.** `__Host-agentwatch`: `Secure`, `HttpOnly`, `Path=/`, no `Domain`,
  `SameSite=Lax`. Lax rather than Strict, so a link from a chat message to a trace opens signed
  in. `--insecure-cookies` drops `Secure` for plain HTTP, and with it the `__Host-` prefix
  browsers would refuse (`agentwatch_session`).
- **Cross-site requests.** Unchanged from before: the host guard refuses another site's `Origin`,
  and writes must be JSON, which a cross-site form cannot send. The sign-in form is the one
  form-encoded write; the guard covers it, and signing out is a JSON write, so another site can
  neither sign the owner in to its account nor sign them out. The sign-in page sends
  `Referrer-Policy: same-origin`: under `no-referrer`, browsers send `Origin: null` with a form
  or a same-origin `fetch` write, which the guard refuses, so a proxy must not set `no-referrer`
  on these pages either. The page also sends a CSP allowing only its own inline script (by hash)
  and style, and refuses framing.
- **Throttling.** Per client address: 5 failures free, then a wait of 1 minute doubling up to an
  hour, cleared by a success. For the account: at most 30 failures an hour from all addresses,
  since OWASP ties lockout to the account so that many addresses cannot each take their free
  tries. Each attempt counts as a failure from the moment it is let through until the password
  turns out right, in one locked step with the check of the limits, so attempts sent together
  cannot all slip in before the first failure is recorded. A throttled request gets 429 with
  `Retry-After` and is not checked. Failures are logged with the address only, never the name or
  password tried.
- **What the throttle costs the owner.** One address cannot lock the owner out. Many addresses
  can: 30 failures an hour keeps new sign-ins refused for as long as it goes on, from anywhere.
  Sessions already open keep working, and a restart clears the throttle (and every session).
  That is the price of a cap on distributed guessing; with a long random password the cap is
  rarely what stops a guess, so it is there for a weak one.
- **The client address.** The TCP peer, or with `--forwarded-allow-ips` the client a trusted
  proxy names in `X-Forwarded-For` (uvicorn's proxy headers, otherwise off). Behind a proxy the
  flag is needed: without it every visitor shares the proxy's address, and five wrong guesses
  from anyone make the owner wait too. Name the proxy's exact address or network. With Caddy on
  the host and agentwatch in a container, the peer is the Docker network's gateway (such as
  `172.18.0.1`), not `127.0.0.1`. `'*'` believes whatever any peer writes, so a client that can
  reach the port directly could claim a new address for each guess.
- **Programs.** `--api-key-env VAR` (repeatable) names variables holding keys of at least 32
  characters, accepted as `Authorization: Bearer <key>` on every route, OTLP ingest included, as
  Phoenix's system keys are. The presented key's SHA-256 is compared with every key's in
  constant time. A key is not a user: `/api/auth/session` reports no user for it. Keys have no
  scopes: an exporter's key can also read, change and delete, so give each program its own key
  and keep them out of logs.
- **What stays open.** `/login`, the sign-in post, signing out (so a stale cookie can still be
  cleared), `/api/auth/session` (whether signing in is on, and who is signed in) and `/healthz`, which then reports only `{"ok": true}`, not the span
  count. Without a session, a request for the UI page gets a 303 to `/login`, which browsers
  follow keeping the URL's `#route`; the page copies the route into the form, so signing in
  returns to it. Any other request gets 401, and the UI sends itself to the sign-in page on a 401.
  A refused attempt (wrong password, busy, too many attempts once the form has been read) shows
  the form again as the answer to the post, at an address with no `#route`. So the server writes
  the posted route back into the form when it is a UI route, and the page's script replaces it
  only from an address that has one; a mistyped password no longer lands on the trace list
  instead of the linked trace (found on a phone, 13 November 2026). The user name typed is
  written back too, escaped, and the password field takes the focus; the password is never
  written back.
- **Failing closed.** `--auth-user` without a usable hash, an empty name (as from an unset
  Compose variable) or an empty key variable stops the server at start rather than serving
  without the sign-in asked for. So does a password hash in the environment without
  `--auth-user`, or `--api-key-env` or `--insecure-cookies` without it: a forgotten flag, not a
  choice to serve openly.

Not built: more than one account, roles, scoped API keys, password change from the UI (make a
new hash and restart), two-factor sign-in, and sessions that survive a restart.

## Memory

What an agent remembers, how each item changed, and what each model call had in context
(`memory.py`, schema version 11). It reads the GenAI memory spans (semantic-conventions-genai,
development status): a span whose `gen_ai.operation.name` is `create_memory`, `update_memory`,
`upsert_memory` or `delete_memory` changed the records it names, and one whose operation is
`search_memory` read them. Each record is one row of `memory_records`, from
`gen_ai.memory.records` (a list of `{id, content, metadata, score}`, structured or as a JSON
string, as the conventions allow on spans), else from `gen_ai.memory.record.id`.
`gen_ai.memory.store.id` names the store, and an item is a store and record ID.

- **What is not recorded.** A span that failed records nothing, since its change or read may not
  have happened. A `delete_memory` without a record ID, which in the conventions deletes the whole
  store, and a write record without an ID are skipped, since no item can be named. A search record
  without an ID is kept for the call's context, but belongs to no item. At most 500 records a
  span; IDs over 500 characters are dropped with their record, content over 64 KB of JSON is cut
  and says so, and metadata over 8 KB is dropped.
- **Content.** The conventions make `gen_ai.memory.records` opt-in because it carries content. A
  record whose `content` is `null` has its ID, metadata and size without the text, which is how an
  exporter can report what was in context with content capture off.
- **Re-sending** a span replaces its records. The rules have a version in `meta`
  (`memory_derivation`, which also follows `genai.DERIVATION_VERSION`, since that decides a span's
  operation), so stored spans are read again, 200 at a time, when either changes, and once after
  the upgrade to schema 11.
- **Retention.** Records are not pruned with their traces: an item's history outlasts the traces
  it links to, which then show as no longer kept. A pruned read keeps its count but loses its copy
  of the content, which the changes already hold. `DELETE /api/memory/item?store=&id=` deletes an
  item's history, for example after the agent was told to forget it: its records, and its records
  inside the stored memory spans, which are rewritten without them. The deletion is remembered
  (`memory_deleted`), so re-reading or re-sending those spans cannot bring it back. A change is
  recorded again when its span ends after the deletion by agentwatch's clock, so one made just
  before the deletion but sent after it is dropped, as is one from an exporter whose clock is
  behind. The text can still be in captured prompts and tool
  results until their traces are pruned. It does not change the agent's own memory.

**In context.** A model call has in context the records of the `search_memory` spans in the same
agent that ended before it started: the spans whose nearest `invoke_agent` ancestor is the call's
own, or that have none when the call has none. So reads by a tool inside the agent count for the
calls after them, and a nested agent sees only its own reads; an exporter should put the read
that fills a prompt inside the agent whose calls use it. An item read more than once is listed
once, as its latest read, and embeddings calls have no context. This is what was in the prompt,
as the instrumentation reports it. Whether the model used an item is not known, so the UI never
says it was used. The Memory pages count reads, whether or not a call followed them, and only a
model call's span says what was in context. A record's size is its metadata's `characters` when
given, else the length of its captured content; tokens are an estimate at four characters a
token.

**API.** `GET /api/memory?store=&service=&q=&limit=&before=` lists items, most recently active
first, with `stores` and a `next` cursor; `q` matches the record ID or content. The cursor follows
latest activity, so an item that becomes active while you page can be skipped. An item's content
and metadata are its latest captured ones, `writes` and `reads` count its changes and reads, and
`deleted` is whether its last change was a delete. `GET /api/memory/item?store=&id=` gives one
item with all its changes (up to 500) and its 50 most recent reads, fetched apart so the reads of
an agent that loads every item each turn cannot bury the changes. Each event carries the links of
its span, such as a learner's links to the turns it learned from. IDs are arbitrary text, so both
go in the query string. `GET /api/traces/<id>` adds `in_context`, from model call span ID to its
records.

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

Hash-routed (`#/`, `#/trace/<id>`, `#/trace/<id>/<span>` with that span selected, `#/memory`), so the static server needs no fallback routes. The waterfall
orders spans depth-first by start time, puts the true root first and shows spans whose parent
never arrived as extra roots marked "parent missing", and keeps spans caught in a parent cycle instead of dropping them.
Offsets are computed with `BigInt` to keep nanosecond precision.

### Reading a trace

Added 13 November 2026, after an audit of the screens on a phone: opening an alert's trace and
finding what went wrong took one tap and four long scrolls, and what the user had asked appeared
only as escaped JSON in an attribute table.

- **Turn summary.** The trace page opens with what was asked, what was replied and the first
  error.
  - *Asked* is the last user message of the root span's captured input (`gen_ai.input.messages`,
    then `input.value`), else of the earliest span whose input is a list of messages. Plain text
    counts as the request only on the root: on another span it may be a search query.
  - *Replied* is read from the root's captured output, else the latest span's. Agents often end
    a turn by calling a tool, so when the output ends in a tool call with an argument named
    `reply`, `answer`, `response` or `message`, that argument is the reply and the heading names
    the tool; text beside it is the model's preamble. The list of names is short on purpose:
    `content`, `text` and `output` are as often a file's body or a command's input. An output
    that ends by calling some other tool is not a reply, whatever text came before the call: the
    summary says none was captured and names the tool, which is where the turn stopped.
    Otherwise the reply is the output's text.
  - *First error* is the earliest failed span with no failed span anywhere below it. A failed
    span above another only reports it, however many spans that succeeded lie between. (Failed
    spans that only explain each other, in a parent cycle, give the earliest of them.) It shows
    the span's status message and exception, then what it returned, because a failed tool's
    result usually holds the reason. When the root did not fail, the heading adds "the turn went
    on": a retry that then succeeded, for example.
- **What is selected.** The span in the address when the trace has it; else the first error;
  else the root.
- **Steps and Timeline.** Timeline is the waterfall. Steps lists the same spans in the same order
  as things that happened: each model call's text (thinking folded away) and the names of the
  tools it called, each tool's input and output, everything else as one line. A model call's input
  is not repeated in Steps, since it is mostly the history above it; it is in the span's detail.
  Steps is the default at 900 px and narrower, Timeline above; the choice is kept in
  `localStorage`, a per-browser convenience that nothing else depends on.
- **Captured content** is shown as messages (role, text, tool calls and results) when it is a
  list of them in the GenAI parts shape or the chat-completions shape, and otherwise as text with
  JSON indented. JSON text holding a number of 16 digits or more is left as written, since
  JavaScript would change it on parsing; a number that long inside a message list that is itself
  one JSON text is already rounded when read, which only a lossless parser would avoid. Line
  breaks are kept. Long text is cut to a few lines with "Show all", and a long
  history shows its last four messages first; nothing is dropped, only folded. Each Input, Output
  and Error has a Copy button. The raw attribute table is still there, folded when the span has
  content sections.
- **On a narrow screen a span opens over the page** as a full-screen panel, exactly when the
  address names one of the trace's spans. The address and the view never disagree: picking a
  span changes the address through the History API and the router is told at once, so a resize,
  a reload or Back finds them matching. Tapping a span adds a history entry marked as added by
  a tap, so the browser's Back closes it. Previous and next replace the entry and keep the mark.
  Closing goes back over a marked entry (also after a reload, since history state survives one)
  and otherwise replaces the address with the trace's, so the trace has one entry either way.
  Escape closes it, and the focus returns to the span's row. Focus is not trapped inside the
  panel. On a wide screen, picking a span replaces the address without adding history: Back
  leaves the trace.
- **Operation words become dots** at 600 px and narrower, so a row has room for the tool's name;
  the word stays in the markup for screen readers.
- A trace that cannot be loaded says why in words (not here yet or past retention, or agentwatch
  unreachable) and offers another try.
- Scores show as badges under the summary. The form to add one and "Add to dataset" are in a
  section below the spans, open on a wide screen and folded on a narrow one.

The demo data now carries synthetic content on model calls and tool calls, generated without
drawing random numbers so every other demo value is unchanged.

### Filters live in the address

Added 13 November 2026. The trace list's service and "Errors only" (`#/?service=…&errors=1`,
beside `session` and `user`) and the dashboard's range and service
(`#/dashboard?range=7d&service=…`; the 24-hour range is the default and is left out) are read
from the address and changed by changing it. A link or bookmark reopens the same view, a reload
keeps it, and Back undoes a filter. Such a change goes through `nav.ts`, as picking a span on
the trace page does: the address changes through the History API and the router is told at
once. The browser reports a change of `location.hash` only a moment later, and a checkbox
driven by the address would otherwise show its old state in between.
