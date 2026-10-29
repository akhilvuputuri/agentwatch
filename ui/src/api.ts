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

/** Adds a score as a human annotation. */
export async function addScore(score: NewScore): Promise<string> {
  const response = await fetch("api/scores?source=annotation", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(score),
  });
  if (!response.ok) throw new Error(`${response.status} ${(await response.text()).trim()}`);
  const body = (await response.json()) as { ids: string[] };
  return body.ids[0]!;
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
  traces: (filter: { session?: string; user?: string }) => {
    const params = new URLSearchParams();
    if (filter.session) params.set("session", filter.session);
    if (filter.user) params.set("user", filter.user);
    const query = params.toString();
    return query ? `#/?${query}` : "#/";
  },
};
