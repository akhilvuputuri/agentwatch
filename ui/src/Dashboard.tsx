import { useEffect, useState } from "react";
import { getMetrics, listServices, type Metrics, type MetricsRange } from "./api";
import { BarChart, ChartTable, LineChart } from "./charts";
import { count, duration, msTick, nsToMs, period, share, usd, usdTick } from "./format";

const RANGES: { label: string; range: MetricsRange }[] = [
  { label: "24 hours", range: { kind: "last", hours: 24 } },
  { label: "7 days", range: { kind: "last", hours: 24 * 7 } },
  { label: "30 days", range: { kind: "last", hours: 24 * 30 } },
  { label: "All time", range: { kind: "all" } },
];

const DAY_NS = 24 * 3600 * 1e9;

export function Dashboard() {
  const [rangeIndex, setRangeIndex] = useState(0);
  const [service, setService] = useState("");
  const [services, setServices] = useState<string[]>([]);
  const [data, setData] = useState<Metrics | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    listServices(controller.signal).then(setServices, () => undefined);
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    // The previous figures stay on screen until the new ones arrive.
    getMetrics(RANGES[rangeIndex]!.range, service, controller.signal).then(
      (result) => {
        setData(result);
        setError(null);
      },
      (reason: unknown) => {
        if (!controller.signal.aborted) setError(String(reason));
      },
    );
    return () => controller.abort();
  }, [rangeIndex, service]);

  return (
    <section className="dashboard">
      <div className="toolbar">
        <div className="segmented" role="group" aria-label="Time range">
          {RANGES.map((option, index) => (
            <button
              key={option.label}
              type="button"
              aria-pressed={index === rangeIndex}
              onClick={() => setRangeIndex(index)}
            >
              {option.label}
            </button>
          ))}
        </div>
        <label>
          Service{" "}
          <select value={service} onChange={(event) => setService(event.target.value)}>
            <option value="">All</option>
            {services.map((name) => (
              <option key={name} value={name}>
                {name || "(unnamed)"}
              </option>
            ))}
          </select>
        </label>
      </div>
      {error && <p className="error">Could not load metrics: {error}</p>}
      {data && <Figures data={data} />}
    </section>
  );
}

function Figures({ data }: { data: Metrics }) {
  const o = data.overview;
  const bucketNs = data.window.bucket_ns;
  const longBuckets = bucketNs >= DAY_NS;
  const when = (index: number) => {
    const point = data.series[index];
    if (!point) return "";
    const date = new Date(nsToMs(BigInt(point.start_ns)));
    // Day and week buckets are aligned to UTC, so they are labelled in UTC.
    return longBuckets
      ? `${date.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" })} UTC`
      : date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  };
  const bucketLabel = (index: number) => `${when(index)} · per ${period(bucketNs)}`;
  const costLowerBound = o.calls_without_cost > 0 && o.cost_usd !== null ? "≥ " : "";

  if (o.traces === 0 && o.model_calls === 0 && o.tool_calls === 0)
    return <p className="empty">No spans in this range. Pick a longer range, or send traces to /v1/traces.</p>;

  return (
    <>
      <div className="tiles">
        <Tile label="Cost" value={`${costLowerBound}${usd(o.cost_usd)}`} note={
          o.calls_without_cost > 0 ? `${o.calls_without_cost} model calls reported no cost` : `${o.traces} traces`
        } hero />
        <Tile label="Cost per trace" value={usd(o.trace_cost_p50)} note={`median · p95 ${usd(o.trace_cost_p95)}`} />
        <Tile label="Model calls" value={count(o.model_calls)} note={`${share(o.model_errors, o.model_calls)} failed`} />
        <Tile label="Cache read share" value={share(o.cache_read_tokens, o.input_tokens)} note={`${count(o.cache_read_tokens)} of ${count(o.input_tokens)} input tokens`} />
        <Tile label="Model latency" value={o.model_p50_ms === null ? "–" : duration(o.model_p50_ms * 1e6)} note={`median · p95 ${o.model_p95_ms === null ? "–" : duration(o.model_p95_ms * 1e6)}`} />
        <Tile label="Trace duration" value={o.trace_p50_ms === null ? "–" : duration(o.trace_p50_ms * 1e6)} note={`median · p95 ${o.trace_p95_ms === null ? "–" : duration(o.trace_p95_ms * 1e6)}`} />
        <Tile label="Tool calls" value={count(o.tool_calls)} note={`${share(o.tool_errors, o.tool_calls)} failed`} />
        <Tile label="Output tokens" value={count(o.output_tokens)} note={`${count(o.reasoning_tokens)} reasoning`} />
      </div>

      <div className="charts">
        <figure className="panel">
          <figcaption>Cost per {period(bucketNs)}</figcaption>
          <BarChart
            ariaLabel="Cost per time bucket"
            data={data.series.map((point, index) => ({
              label: when(index),
              value: point.cost_usd,
              tooltip: { value: usd(point.cost_usd), label: `${bucketLabel(index)} · ${point.model_calls} model calls` },
            }))}
            format={usdTick}
            axisLabel={when}
          />
          <ChartTable
            columns={["Bucket start", "Cost", "Model calls", "Traces"]}
            rows={data.series.map((p, i) => [when(i), usd(p.cost_usd), p.model_calls, p.traces])}
          />
        </figure>
        <figure className="panel">
          <figcaption>Model calls per {period(bucketNs)}</figcaption>
          <BarChart
            ariaLabel="Model calls per time bucket"
            data={data.series.map((point, index) => ({
              label: when(index),
              value: point.model_calls,
              tooltip: {
                value: `${point.model_calls} calls`,
                label: `${bucketLabel(index)} · ${point.model_errors} failed`,
              },
            }))}
            format={(v) => count(v)}
            axisLabel={when}
          />
          <ChartTable
            columns={["Bucket start", "Model calls", "Failed", "Tool calls"]}
            rows={data.series.map((p, i) => [when(i), p.model_calls, p.model_errors, p.tool_calls])}
          />
        </figure>
        <figure className="panel wide">
          <figcaption>Model call latency</figcaption>
          <LineChart
            ariaLabel="Model call latency, median and 95th percentile, per time bucket"
            series={[
              { name: "p95", color: "var(--series-2)", values: data.series.map((p) => p.model_p95_ms) },
              { name: "p50", color: "var(--series-1)", values: data.series.map((p) => p.model_p50_ms) },
            ]}
            format={msTick}
            tooltipFormat={(v) => duration(v * 1e6)}
            axisLabel={when}
            bucketLabel={bucketLabel}
          />
          <ChartTable
            columns={["Bucket start", "p50", "p95"]}
            rows={data.series.map((p, i) => [
              when(i),
              p.model_p50_ms === null ? "–" : duration(p.model_p50_ms * 1e6),
              p.model_p95_ms === null ? "–" : duration(p.model_p95_ms * 1e6),
            ])}
          />
        </figure>
      </div>

      <h2 className="section">By model</h2>
      <Table
        columns={["Model", "Calls", "Failed", "Input", "Cached", "Output", "Cost", "p50", "p95", "Finish reasons"]}
        rows={data.by_model.map((m) => [
          <span title={m.provider ?? undefined}>{m.model}</span>,
          m.calls,
          share(m.errors, m.calls),
          count(m.input_tokens),
          share(m.cache_read_tokens, m.input_tokens),
          count(m.output_tokens),
          `${m.calls_without_cost > 0 && m.cost_usd !== null ? "≥ " : ""}${usd(m.cost_usd)}`,
          ms(m.p50_ms),
          ms(m.p95_ms),
          mix(m.finish_reasons),
        ])}
      />
      <h2 className="section">By tool</h2>
      <Table
        columns={["Tool", "Calls", "Failed", "p50", "p95", "Max", "Errors"]}
        rows={data.by_tool.map((t) => [t.tool, t.calls, share(t.errors, t.calls), ms(t.p50_ms), ms(t.p95_ms), ms(t.max_ms), mix(t.error_types ?? {})])}
      />
      <h2 className="section">By agent</h2>
      <p className="muted small">An agent's calls and cost are its own, not its child agents'.</p>
      <Table
        columns={["Agent", "Runs", "Failed", "p50", "p95", "Model calls", "Tool calls", "Input", "Cost"]}
        rows={data.by_agent.map((a) => [
          a.unowned ? <span className="muted">No agent span</span> : a.agent,
          a.runs,
          share(a.errors, a.runs),
          ms(a.p50_ms),
          ms(a.p95_ms),
          a.model_calls,
          a.tool_calls,
          count(a.input_tokens),
          usd(a.cost_usd),
        ])}
      />
    </>
  );
}

function Tile({ label, value, note, hero }: { label: string; value: string; note: string; hero?: boolean }) {
  return (
    <div className={`tile${hero ? " hero" : ""}`} data-testid="tile">
      <div className="tile-label">{label}</div>
      <div className="tile-value">{value}</div>
      <div className="tile-note">{note}</div>
    </div>
  );
}

function Table({ columns, rows }: { columns: string[]; rows: React.ReactNode[][] }) {
  if (rows.length === 0) return <p className="muted small">None in this range.</p>;
  return (
    <div className="table-wrap">
      <table className="traces">
        <thead>
          <tr>
            {columns.map((column, index) => (
              <th key={column} className={isText(columns, index) ? undefined : "num"}>
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              {row.map((cell, cellIndex) => (
                <td key={cellIndex} className={isText(columns, cellIndex) ? undefined : "num"}>
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const TEXT_COLUMNS = new Set(["Finish reasons", "Errors"]);
const isText = (columns: string[], index: number) =>
  index === 0 || TEXT_COLUMNS.has(columns[index] ?? "");

const ms = (value: number | null) => (value === null ? "–" : duration(value * 1e6));

function mix(counts: Record<string, number>) {
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) return <span className="muted">–</span>;
  return <span className="small">{entries.map(([key, n]) => `${key} ${n}`).join(" · ")}</span>;
}
