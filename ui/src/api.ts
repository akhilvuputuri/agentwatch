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
  models: string[];
  complete: boolean;
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

export async function listTraces(query: TraceQuery, signal?: AbortSignal): Promise<TraceSummary[]> {
  const params = new URLSearchParams();
  if (query.limit !== undefined) params.set("limit", String(query.limit));
  if (query.before) params.set("before", query.before);
  if (query.service) params.set("service", query.service);
  if (query.errors) params.set("errors", "1");
  const body = await getJson<{ traces: TraceSummary[] }>(`api/traces?${params}`, signal);
  return body.traces;
}

export async function getTrace(traceId: string, signal?: AbortSignal): Promise<SpanRecord[]> {
  const body = await getJson<{ spans: SpanRecord[] }>(`api/traces/${traceId}`, signal);
  return body.spans;
}

export async function listServices(signal?: AbortSignal): Promise<string[]> {
  const body = await getJson<{ services: string[] }>("api/services", signal);
  return body.services;
}
