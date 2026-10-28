import { useCallback, useEffect, useRef, useState } from "react";
import { listServices, listTraces, type TraceSummary } from "./api";
import { count, duration, share, timestamp, usd } from "./format";

const PAGE = 50;

export function TraceList() {
  const [service, setService] = useState("");
  const [errors, setErrors] = useState(false);
  const [services, setServices] = useState<string[]>([]);
  const [traces, setTraces] = useState<TraceSummary[]>([]);
  const [next, setNext] = useState<string | null>(null);
  // Aborts a "load older" request when the filters change under it.
  const older = useRef<AbortController | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    listServices(controller.signal).then(setServices, () => undefined);
    return () => controller.abort();
  }, [reload]);

  useEffect(() => {
    const controller = new AbortController();
    older.current?.abort();
    setNext(null);
    setLoading(true);
    listTraces({ limit: PAGE, service, errors }, controller.signal).then(
      (page) => {
        setTraces(page.traces);
        setNext(page.next);
        setError(null);
        setLoading(false);
      },
      (reason: unknown) => {
        if (controller.signal.aborted) return;
        setError(String(reason));
        setLoading(false);
      },
    );
    return () => controller.abort();
  }, [service, errors, reload]);

  const loadMore = useCallback(() => {
    if (!next) return;
    older.current?.abort();
    const controller = new AbortController();
    older.current = controller;
    listTraces({ limit: PAGE, service, errors, before: next }, controller.signal).then(
      (page) => {
        setTraces((current) => {
          const known = new Set(current.map((t) => t.trace_id));
          return [...current, ...page.traces.filter((t) => !known.has(t.trace_id))];
        });
        setNext(page.next);
      },
      (reason: unknown) => {
        if (!controller.signal.aborted) setError(String(reason));
      },
    );
  }, [next, service, errors]);

  return (
    <section>
      <div className="toolbar">
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
        <label>
          <input type="checkbox" checked={errors} onChange={(event) => setErrors(event.target.checked)} /> Errors
          only
        </label>
        <button type="button" onClick={() => setReload((n) => n + 1)}>
          Refresh
        </button>
      </div>
      {error && <p className="error">Could not load traces: {error}</p>}
      {!error && !loading && traces.length === 0 && (
        <p className="empty">
          No traces yet. Point an OTLP/HTTP exporter at <code>{window.location.origin}/v1/traces</code>, or run{" "}
          <code>agentwatch serve --demo</code>.
        </p>
      )}
      {traces.length > 0 && (
        <div className="table-wrap">
          <table className="traces">
            <thead>
              <tr>
                <th>Started</th>
                <th>Trace</th>
                <th className="num">Duration</th>
                <th className="num">Model calls</th>
                <th className="num">Tools</th>
                <th className="num">Input</th>
                <th className="num">Cached</th>
                <th className="num">Output</th>
                <th className="num">Cost</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {traces.map((trace) => (
                <tr key={trace.trace_id} data-testid="trace-row">
                  <td className="muted">{timestamp(trace.start_ns)}</td>
                  <td>
                    <a href={`#/trace/${trace.trace_id}`}>{trace.root_name || "(unnamed)"}</a>
                    <div className="muted small">
                      {trace.service_name}
                      {trace.models.length > 0 && ` · ${trace.models.join(", ")}`}
                    </div>
                  </td>
                  <td className="num">{duration(trace.duration_ns)}</td>
                  <td className="num">{trace.model_calls}</td>
                  <td className="num">{trace.tool_calls}</td>
                  <td className="num">{count(trace.input_tokens)}</td>
                  <td className="num">{share(trace.cache_read_tokens, trace.input_tokens)}</td>
                  <td className="num">{count(trace.output_tokens)}</td>
                  <td
                    className="num"
                    title={
                      trace.calls_without_cost > 0
                        ? `${trace.calls_without_cost} model call(s) reported no cost`
                        : undefined
                    }
                  >
                    {trace.calls_without_cost > 0 && trace.cost_usd !== null ? "≥ " : ""}
                    {usd(trace.cost_usd)}
                  </td>
                  <td>
                    {trace.error_count > 0 ? (
                      <span className="badge bad">{trace.error_count} error{trace.error_count > 1 ? "s" : ""}</span>
                    ) : (
                      <span className="badge ok">ok</span>
                    )}
                    {!trace.complete && <span className="badge warn" title="Root span not received">partial</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {next && (
        <button type="button" className="more" onClick={loadMore}>
          Load older traces
        </button>
      )}
    </section>
  );
}
