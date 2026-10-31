export interface TraceSummary {
  trace_id: string;
  root_name: string;
  service_name: string;
  /** Unix nanoseconds as a decimal string; it exceeds Number's exact range. */
  start_ns: string;
  duration_ns: number;
  span_count: number;
  error_count: number;
  model_calls: number;
  tool_calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cost_usd: number | null;
  /** Model calls that reported no cost; above zero, cost_usd is a lower bound. */
  calls_without_cost: number;
  models: string[];
  complete: boolean;
  session_id: string | null;
  user_id: string | null;
  scores: ScoreAggregate[];
}

export interface ScoreAggregate {
  name: string;
  data_type: ScoreType;
  count: number;
  mean: number | null;
  label: string | null;
}

export type ScoreType = "NUMERIC" | "BOOLEAN" | "CATEGORICAL";

export interface Score {
  id: string;
  trace_id: string;
  span_id: string | null;
  name: string;
  data_type: ScoreType;
  value: number | null;
  label: string | null;
  source: "API" | "ANNOTATION" | "EVAL";
  comment: string | null;
  created_ns: string;
}

export interface TraceDetail {
  trace_id: string;
  session_id: string | null;
  user_id: string | null;
  spans: SpanRecord[];
  scores: Score[];
}

export interface NewScore {
  trace_id: string;
  span_id?: string;
  name: string;
  value?: number | boolean;
  label?: string;
  comment?: string;
}

/** Adds a score as a human annotation and returns it as stored. */
export async function addScore(score: NewScore): Promise<Score> {
  const response = await fetch("api/scores?source=annotation", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(score),
  });
  if (!response.ok) throw new Error(`${response.status} ${(await response.text()).trim()}`);
  const body = (await response.json()) as { scores: Score[] };
  return body.scores[0]!;
}

export async function deleteScore(id: string): Promise<void> {
  const response = await fetch(`api/scores/${encodeURIComponent(id)}`, { method: "DELETE" });
  if (!response.ok && response.status !== 404) {
    throw new Error(`${response.status} ${(await response.text()).trim()}`);
  }
}

export interface TracePage {
  traces: TraceSummary[];
  /** Cursor for the next (older) page, or null when this was the last. */
  next: string | null;
}

export interface GenAIFields {
  operation: string | null;
  provider: string | null;
  model: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
  reasoning_tokens: number | null;
  cost_usd: number | null;
  finish_reasons: string[];
  tool_name: string | null;
  agent_name: string | null;
  error_type: string | null;
  conversation_id: string | null;
  user_id: string | null;
  /** "reported" by the exporter, or "calculated" from a model price. */
  cost_source: "reported" | "calculated" | null;
}

export interface SpanEvent {
  name: string;
  time_ns: string;
  attributes: Record<string, unknown>;
}

export interface SpanRecord {
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  name: string;
  kind: number;
  start_ns: string;
  end_ns: string;
  status_code: number;
  status_message: string;
  service_name: string;
  scope_name: string;
  attributes: Record<string, unknown>;
  resource: Record<string, unknown>;
  events: SpanEvent[];
  links: { trace_id: string; span_id: string; attributes: Record<string, unknown> }[];
  genai: GenAIFields;
}

export const STATUS_ERROR = 2;

export interface TraceQuery {
  limit?: number;
  before?: string;
  service?: string;
  errors?: boolean;
  session?: string;
  user?: string;
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, signal ? { signal } : {});
  if (!response.ok) {
    throw new Error(`${response.status} ${(await response.text()).trim()}`);
  }
  return (await response.json()) as T;
}

export async function listTraces(query: TraceQuery, signal?: AbortSignal): Promise<TracePage> {
  const params = new URLSearchParams();
  if (query.limit !== undefined) params.set("limit", String(query.limit));
  if (query.before) params.set("before", query.before);
  if (query.service) params.set("service", query.service);
  if (query.errors) params.set("errors", "1");
  if (query.session) params.set("session", query.session);
  if (query.user) params.set("user", query.user);
  return getJson<TracePage>(`api/traces?${params}`, signal);
}

export async function getTrace(traceId: string, signal?: AbortSignal): Promise<TraceDetail> {
  return getJson<TraceDetail>(`api/traces/${traceId}`, signal);
}

export async function listServices(signal?: AbortSignal): Promise<string[]> {
  const body = await getJson<{ services: string[] }>("api/services", signal);
  return body.services;
}

export interface MetricsOverview {
  traces: number;
  model_calls: number;
  model_errors: number;
  tool_calls: number;
  tool_errors: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  reasoning_tokens: number;
  cost_usd: number | null;
  calls_without_cost: number;
  model_p50_ms: number | null;
  model_p95_ms: number | null;
  trace_p50_ms: number | null;
  trace_p95_ms: number | null;
  trace_cost_p50: number | null;
  trace_cost_p95: number | null;
  trace_cost_mean: number | null;
}

export interface ModelRow {
  model: string;
  provider: string | null;
  calls: number;
  errors: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  reasoning_tokens: number;
  cost_usd: number | null;
  calls_without_cost: number;
  p50_ms: number | null;
  p95_ms: number | null;
  max_ms: number | null;
  finish_reasons: Record<string, number>;
  error_types: Record<string, number>;
}

export interface ToolRow {
  tool: string;
  calls: number;
  errors: number;
  p50_ms: number | null;
  p95_ms: number | null;
  max_ms: number | null;
  error_types: Record<string, number> | null;
}

export interface AgentRow {
  agent: string;
  /** Calls with no invoke_agent ancestor, grouped together. */
  unowned: boolean;
  runs: number;
  errors: number;
  p50_ms: number | null;
  p95_ms: number | null;
  model_calls: number;
  tool_calls: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number | null;
}

export interface SeriesPoint {
  start_ns: string;
  model_calls: number;
  model_errors: number;
  tool_calls: number;
  traces: number;
  input_tokens: number;
  cache_read_tokens: number;
  cost_usd: number | null;
  model_p50_ms: number | null;
  model_p95_ms: number | null;
}

export interface ScoreRow {
  name: string;
  data_type: ScoreType;
  count: number;
  mean: number | null;
  min: number | null;
  max: number | null;
  labels: Record<string, number>;
  sources: Record<string, number>;
}

export interface Metrics {
  window: { start_ns: string; end_ns: string; bucket_ns: number; service: string | null };
  overview: MetricsOverview;
  by_model: ModelRow[];
  by_tool: ToolRow[];
  by_agent: AgentRow[];
  series: SeriesPoint[];
  scores: ScoreRow[];
}

export type MetricsRange = { kind: "last"; hours: number } | { kind: "all" };

export async function getMetrics(
  range: MetricsRange,
  service: string,
  signal?: AbortSignal,
): Promise<Metrics> {
  const params = new URLSearchParams();
  if (range.kind === "all") {
    params.set("range", "all");
  } else {
    const to = BigInt(Date.now()) * 1_000_000n;
    params.set("to", String(to));
    params.set("from", String(to - BigInt(range.hours) * 3_600_000_000_000n));
  }
  if (service) params.set("service", service);
  return getJson<Metrics>(`api/metrics?${params}`, signal);
}

export interface SessionSummary {
  session_id: string;
  traces: number;
  first_start_ns: string;
  last_start_ns: string;
  last_end_ns: string;
  error_count: number;
  model_calls: number;
  tool_calls: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number | null;
  calls_without_cost: number;
  users: string[];
  services: string[];
}

export async function listSessions(
  query: { before?: string; user?: string; limit?: number },
  signal?: AbortSignal,
): Promise<{ sessions: SessionSummary[]; next: string | null }> {
  const params = new URLSearchParams();
  if (query.limit !== undefined) params.set("limit", String(query.limit));
  if (query.before) params.set("before", query.before);
  if (query.user) params.set("user", query.user);
  return getJson(`api/sessions?${params}`, signal);
}

export async function getSession(
  sessionId: string,
  signal?: AbortSignal,
): Promise<{ session: SessionSummary; traces: TraceSummary[]; truncated: boolean }> {
  return getJson(`api/sessions/${encodeURIComponent(sessionId)}`, signal);
}

/** Hash links to the views, with IDs encoded so any session or user string is safe. */
export const links = {
  session: (id: string) => `#/session/${encodeURIComponent(id)}`,
  dataset: (id: string) => `#/dataset/${encodeURIComponent(id)}`,
  run: (id: string) => `#/run/${encodeURIComponent(id)}`,
  compare: (baseline: string, candidate: string) =>
    `#/compare/${encodeURIComponent(baseline)}/${encodeURIComponent(candidate)}`,
  traces: (filter: { session?: string; user?: string }) => {
    const params = new URLSearchParams();
    if (filter.session) params.set("session", filter.session);
    if (filter.user) params.set("user", filter.user);
    const query = params.toString();
    return query ? `#/?${query}` : "#/";
  },
};

export interface Price {
  id: string;
  match_pattern: string;
  input_per_million: number;
  output_per_million: number;
  cache_read_per_million: number | null;
  cache_write_per_million: number | null;
  start_ns: string | null;
  created_ns: string;
}

export type NewPrice = Omit<Price, "id" | "created_ns" | "start_ns"> & { start_ns?: string };

export async function listPrices(signal?: AbortSignal): Promise<Price[]> {
  const body = await getJson<{ prices: Price[] }>("api/prices", signal);
  return body.prices;
}

export async function addPrice(price: NewPrice): Promise<Price> {
  const response = await fetch("api/prices", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(price),
  });
  if (!response.ok) throw new Error(`${response.status} ${(await response.text()).trim()}`);
  return ((await response.json()) as { price: Price }).price;
}

export async function deletePrice(id: string): Promise<void> {
  const response = await fetch(`api/prices/${encodeURIComponent(id)}`, { method: "DELETE" });
  if (!response.ok && response.status !== 404) {
    throw new Error(`${response.status} ${(await response.text()).trim()}`);
  }
}

export interface Dataset {
  id: string;
  name: string;
  description: string | null;
  metadata: Record<string, unknown>;
  created_ns: string;
  items: number;
  archived_items: number;
}

export interface DatasetItem {
  id: string;
  dataset_id: string;
  input: unknown;
  expected_output: unknown;
  metadata: Record<string, unknown>;
  source_trace_id: string | null;
  source_span_id: string | null;
  status: "ACTIVE" | "ARCHIVED";
  created_ns: string;
}

async function send<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    ...(body === undefined ? {} : { headers: { "content-type": "application/json" }, body: JSON.stringify(body) }),
  });
  if (!response.ok) throw new Error(`${response.status} ${(await response.text()).trim()}`);
  return (response.status === 204 ? undefined : await response.json()) as T;
}

export async function listDatasets(signal?: AbortSignal): Promise<Dataset[]> {
  return (await getJson<{ datasets: Dataset[] }>("api/datasets", signal)).datasets;
}

export async function getDataset(id: string, signal?: AbortSignal): Promise<Dataset> {
  return (await getJson<{ dataset: Dataset }>(`api/datasets/${encodeURIComponent(id)}`, signal)).dataset;
}

export async function createDataset(name: string, description: string): Promise<Dataset> {
  return (await send<{ dataset: Dataset }>("POST", "api/datasets", { name, description: description || null }))
    .dataset;
}

export async function deleteDataset(id: string): Promise<void> {
  await send("DELETE", `api/datasets/${encodeURIComponent(id)}`);
}

export async function listItems(datasetId: string, archived: boolean, signal?: AbortSignal): Promise<DatasetItem[]> {
  const query = archived ? "?archived=1" : "";
  return (await getJson<{ items: DatasetItem[] }>(`api/datasets/${encodeURIComponent(datasetId)}/items${query}`, signal))
    .items;
}

export async function addItem(
  datasetId: string,
  item: { input: unknown; expected_output?: unknown },
): Promise<DatasetItem> {
  const body = await send<{ items: DatasetItem[] }>("POST", `api/datasets/${encodeURIComponent(datasetId)}/items`, item);
  return body.items[0]!;
}

export async function addItemFromTrace(datasetId: string, traceId: string, spanId: string | null): Promise<DatasetItem> {
  const body = await send<{ item: DatasetItem }>("POST", `api/datasets/${encodeURIComponent(datasetId)}/items/from-trace`, {
    trace_id: traceId,
    ...(spanId ? { span_id: spanId } : {}),
  });
  return body.item;
}

export async function setItemStatus(itemId: string, status: "ACTIVE" | "ARCHIVED"): Promise<void> {
  await send("PATCH", `api/dataset-items/${encodeURIComponent(itemId)}`, { status });
}

export interface Run {
  id: string;
  dataset_id: string;
  name: string;
  description: string | null;
  metadata: Record<string, unknown>;
  created_ns: string;
}

export interface RunSummary {
  run: Run;
  items: number;
  errors: number;
  mean_latency_ms: number | null;
  cost_usd: number | null;
  input_tokens: number;
  output_tokens: number;
  scores: { name: string; count: number; mean: number | null }[];
}

export interface RunItemResult {
  item_id: string;
  trace_id: string;
  output: unknown;
  error: string | null;
  latency_ms: number;
  input: unknown;
  expected_output: unknown;
  scores: { id: string; name: string; data_type: ScoreType; value: number | null; label: string | null; comment: string | null }[];
}

export async function listRuns(datasetId: string, signal?: AbortSignal): Promise<RunSummary[]> {
  return (await getJson<{ runs: RunSummary[] }>(`api/datasets/${encodeURIComponent(datasetId)}/runs`, signal)).runs;
}

export async function getRun(runId: string, signal?: AbortSignal): Promise<{ run: Run; items: RunItemResult[] }> {
  return getJson(`api/runs/${encodeURIComponent(runId)}`, signal);
}

export async function deleteRun(runId: string): Promise<void> {
  await send("DELETE", `api/runs/${encodeURIComponent(runId)}`);
}

export type Verdict = "better" | "worse" | "no detectable difference" | "underpowered";

export interface BinaryComparison {
  name: string;
  kind: "binary";
  pairs: number;
  both: number;
  only_baseline: number;
  only_candidate: number;
  neither: number;
  baseline_rate: number | null;
  candidate_rate: number | null;
  baseline_interval: [number, number] | null;
  candidate_interval: [number, number] | null;
  difference: number | null;
  difference_interval: [number, number] | null;
  p_value: number;
  minimum_detectable: number | null;
  margin: number;
  below_margin: boolean;
  dropped_failures: number;
  verdict: Verdict;
}

export interface NumericComparison {
  name: string;
  kind: "numeric";
  pairs: number;
  baseline_mean: number | null;
  candidate_mean: number | null;
  difference: number | null;
  difference_interval: [number, number] | null;
  improved: number;
  regressed: number;
  unchanged: number;
  minimum_detectable: number | null;
  margin: number;
  below_margin: boolean;
  dropped_failures: number;
  verdict: Verdict;
  unit?: string;
  lower_is_better?: boolean;
}

export interface ComparedSide {
  output: unknown;
  error: string | null;
  trace_id: string;
  latency_ms: number;
  scores: RunItemResult["scores"];
}

export interface Comparison {
  baseline: Run;
  candidate: Run;
  pairs: number;
  only_baseline: number;
  only_candidate: number;
  margin: number;
  relative_margin: number;
  comparisons: (BinaryComparison | NumericComparison)[];
  latency: NumericComparison;
  items: {
    item_id: string;
    input: unknown;
    expected_output: unknown;
    baseline: ComparedSide;
    candidate: ComparedSide;
    flips: Record<string, "improved" | "regressed">;
  }[];
}

export async function compareRuns(baseline: string, candidate: string, signal?: AbortSignal): Promise<Comparison> {
  const params = new URLSearchParams({ baseline, candidate });
  return getJson<Comparison>(`api/compare?${params}`, signal);
}

export interface JudgeActivity {
  counts: Record<"SCORED" | "FAILED" | "PENDING" | "NOT_SAMPLED" | "NO_CONTENT" | "OVER_LIMIT", number>;
  calls: number;
  cost_usd: number;
  calls_without_cost: number;
}

export interface Judge {
  id: string;
  name: string;
  model: string;
  base_url: string;
  api_key_env: string | null;
  prompt: string;
  score_type: "BOOLEAN" | "NUMERIC";
  sample_rate: number;
  daily_limit: number;
  service: string | null;
  sessions_only: boolean;
  enabled: boolean;
  created_ns: string;
  enabled_ns: string | null;
  activity: JudgeActivity;
  problem: string | null;
}

export type NewJudge = Pick<Judge, "name" | "model" | "base_url" | "api_key_env" | "prompt" | "score_type" | "sample_rate" | "daily_limit" | "service" | "sessions_only">;

export interface Judgement {
  trace_id: string;
  outcome: keyof JudgeActivity["counts"];
  value: number | null;
  detail: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
  created_ns: string;
}

export async function listJudges(signal?: AbortSignal): Promise<Judge[]> {
  return (await getJson<{ judges: Judge[] }>("api/judges", signal)).judges;
}

export async function createJudge(judge: NewJudge): Promise<Judge> {
  return (await send<{ judge: Judge }>("POST", "api/judges", judge)).judge;
}

export async function updateJudge(id: string, fields: Partial<NewJudge & { enabled: boolean }>): Promise<Judge> {
  return (await send<{ judge: Judge }>("PATCH", `api/judges/${encodeURIComponent(id)}`, fields)).judge;
}

export async function deleteJudge(id: string): Promise<void> {
  await send<void>("DELETE", `api/judges/${encodeURIComponent(id)}`);
}

export async function listJudgements(id: string, signal?: AbortSignal): Promise<Judgement[]> {
  return (await getJson<{ judgements: Judgement[] }>(`api/judges/${encodeURIComponent(id)}/judgements`, signal)).judgements;
}

export type MonitorMetric = "model_error_rate" | "tool_error_rate" | "cost_usd" | "model_p95_ms" | "trace_count" | "score_mean";

export interface MonitorState {
  state: "OK" | "FIRING";
  value: number | null;
  /** A rate's Wilson bound on the threshold's side; null for anything that is not a rate. */
  bound: number | null;
  samples: number | null;
  evaluated_ns: string | null;
  since_ns: string | null;
  /** The last evaluation had too few samples to compare, so the state stayed as it was. */
  insufficient: boolean;
}

export interface Monitor {
  id: string;
  name: string;
  metric: MonitorMetric;
  score_name: string | null;
  service: string | null;
  window_minutes: number;
  comparison: "above" | "below";
  threshold: number;
  min_samples: number;
  enabled: boolean;
  created_ns: string;
  state: MonitorState;
}

export type NewMonitor = Pick<Monitor, "name" | "metric" | "score_name" | "service" | "window_minutes" | "comparison" | "threshold" | "min_samples">;

export interface Alert {
  id: string;
  monitor_id: string;
  monitor_name: string;
  kind: "firing" | "resolved";
  metric: MonitorMetric;
  comparison: "above" | "below";
  threshold: number;
  value: number;
  bound: number | null;
  samples: number;
  window_minutes: number;
  created_ns: string;
  delivered_ns: string | null;
}

export async function listMonitors(signal?: AbortSignal): Promise<Monitor[]> {
  return (await getJson<{ monitors: Monitor[] }>("api/monitors", signal)).monitors;
}

export async function createMonitor(monitor: NewMonitor): Promise<Monitor> {
  return (await send<{ monitor: Monitor }>("POST", "api/monitors", monitor)).monitor;
}

export async function updateMonitor(id: string, fields: Partial<NewMonitor & { enabled: boolean }>): Promise<Monitor> {
  return (await send<{ monitor: Monitor }>("PATCH", `api/monitors/${encodeURIComponent(id)}`, fields)).monitor;
}

export async function deleteMonitor(id: string): Promise<void> {
  await send<void>("DELETE", `api/monitors/${encodeURIComponent(id)}`);
}

export async function listAlerts(signal?: AbortSignal): Promise<Alert[]> {
  return (await getJson<{ alerts: Alert[] }>("api/alerts", signal)).alerts;
}
