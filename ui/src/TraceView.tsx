import { useEffect, useMemo, useState } from "react";
import { getTrace, STATUS_ERROR, type SpanRecord } from "./api";
import { count, duration, timestamp, usd } from "./format";
import { layout, type Row } from "./tree";

export function TraceView({ traceId }: { traceId: string }) {
  const [spans, setSpans] = useState<SpanRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setSpans(null);
    setError(null);
    getTrace(traceId, controller.signal).then(
      (result) => {
        setSpans(result);
        setSelected(null);
      },
      (reason: unknown) => {
        if (!controller.signal.aborted) setError(String(reason));
      },
    );
    return () => controller.abort();
  }, [traceId]);

  const view = useMemo(() => layout(spans ?? []), [spans]);
  const chosen = view.rows.find((row) => row.span.span_id === selected) ?? view.rows[0];

  if (error) return <p className="error">Could not load trace: {error}</p>;
  if (!spans) return <p className="muted">Loading…</p>;

  const root = view.rows[0]?.span;
  return (
    <section className="trace">
      <p>
        <a href="#/">← All traces</a>
      </p>
      <h1>{root?.name ?? traceId}</h1>
      <p className="muted small">
        {root && timestamp(root.start_ns)} · {duration(view.totalNs)} · {spans.length} spans ·{" "}
        <code>{traceId}</code>
      </p>
      <div className="split">
        <div className="waterfall" role="tree" aria-label="Span tree">
          {view.rows.map((row) => (
            <SpanRow
              key={row.span.span_id}
              row={row}
              totalNs={view.totalNs}
              selected={row.span.span_id === chosen?.span.span_id}
              onSelect={() => setSelected(row.span.span_id)}
            />
          ))}
        </div>
        {chosen && <SpanDetail row={chosen} />}
      </div>
    </section>
  );
}

function SpanRow({
  row,
  totalNs,
  selected,
  onSelect,
}: {
  row: Row;
  totalNs: number;
  selected: boolean;
  onSelect: () => void;
}) {
  const { span } = row;
  const g = span.genai;
  const left = totalNs > 0 ? (row.offsetNs / totalNs) * 100 : 0;
  const width = totalNs > 0 ? Math.max((row.durationNs / totalNs) * 100, 0.3) : 100;
  const kind = g.operation ?? "span";
  return (
    <button
      type="button"
      role="treeitem"
      aria-selected={selected}
      data-testid="span-row"
      className={`span-row${selected ? " selected" : ""}`}
      onClick={onSelect}
    >
      <span className="label" style={{ paddingLeft: `${row.depth * 14}px` }}>
        <span className={`op op-${kind.replace(/[^a-z_]/g, "")}`}>{kind}</span>
        <span className="name">{g.tool_name ?? g.agent_name ?? g.model ?? span.name}</span>
        {span.status_code === STATUS_ERROR && <span className="badge bad">error</span>}
        {row.orphan && <span className="badge warn">parent missing</span>}
      </span>
      <span className="track">
        <span
          className={`bar bar-${kind.replace(/[^a-z_]/g, "")}${span.status_code === STATUS_ERROR ? " bar-error" : ""}`}
          style={{ left: `${left}%`, width: `${Math.min(width, 100 - left)}%` }}
        />
      </span>
      <span className="dur">{duration(row.durationNs)}</span>
    </button>
  );
}

function SpanDetail({ row }: { row: Row }) {
  const { span } = row;
  const g = span.genai;
  const facts: [string, string][] = [];
  if (g.operation) facts.push(["Operation", g.operation]);
  if (g.provider) facts.push(["Provider", g.provider]);
  if (g.model) facts.push(["Model", g.model]);
  if (g.input_tokens !== null) facts.push(["Input tokens", count(g.input_tokens)]);
  if (g.cache_read_tokens !== null) facts.push(["Cache read", count(g.cache_read_tokens)]);
  if (g.cache_write_tokens !== null) facts.push(["Cache write", count(g.cache_write_tokens)]);
  if (g.output_tokens !== null) facts.push(["Output tokens", count(g.output_tokens)]);
  if (g.reasoning_tokens !== null) facts.push(["Reasoning", count(g.reasoning_tokens)]);
  if (g.cost_usd !== null) facts.push(["Cost", usd(g.cost_usd)]);
  if (g.finish_reasons.length) facts.push(["Finish", g.finish_reasons.join(", ")]);
  if (g.error_type) facts.push(["Error type", g.error_type]);
  facts.push(["Duration", duration(row.durationNs)]);
  facts.push(["Offset", duration(row.offsetNs)]);
  return (
    <aside className="detail" data-testid="span-detail">
      <h2>{span.name}</h2>
      {span.status_message && <p className="error">{span.status_message}</p>}
      <dl className="facts">
        {facts.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
      <h3>Attributes</h3>
      <Attributes values={span.attributes} />
      {span.events.length > 0 && (
        <>
          <h3>Events</h3>
          <ul className="events">
            {span.events.map((event, index) => (
              <li key={index}>
                <strong>{event.name}</strong> <span className="muted">{timestamp(event.time_ns)}</span>
                <Attributes values={event.attributes} />
              </li>
            ))}
          </ul>
        </>
      )}
      <h3>Resource</h3>
      <Attributes values={span.resource} />
      <p className="muted small">
        span <code>{span.span_id}</code>
        {span.parent_span_id && (
          <>
            {" "}
            · parent <code>{span.parent_span_id}</code>
          </>
        )}
      </p>
    </aside>
  );
}

function Attributes({ values }: { values: Record<string, unknown> }) {
  const entries = Object.entries(values).sort(([a], [b]) => a.localeCompare(b));
  if (entries.length === 0) return <p className="muted small">None</p>;
  return (
    <table className="attrs">
      <tbody>
        {entries.map(([key, value]) => (
          <tr key={key}>
            <th>{key}</th>
            <td>{typeof value === "string" ? value : JSON.stringify(value)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
