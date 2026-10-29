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
  return getJson<TracePage>(`api/traces?${params}`, signal);
}

export async function getTrace(traceId: string, signal?: AbortSignal): Promise<SpanRecord[]> {
  const body = await getJson<{ spans: SpanRecord[] }>(`api/traces/${traceId}`, signal);
  return body.spans;
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

export interface Metrics {
  window: { start_ns: string; end_ns: string; bucket_ns: number; service: string | null };
  overview: MetricsOverview;
  by_model: ModelRow[];
  by_tool: ToolRow[];
  by_agent: AgentRow[];
  series: SeriesPoint[];
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
