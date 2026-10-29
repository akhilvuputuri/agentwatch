import { useCallback, useEffect, useRef, useState } from "react";
import { getSession, links, listSessions, type SessionSummary, type TraceSummary } from "./api";
import { count, duration, timestamp, usd } from "./format";

const PAGE = 50;

const span = (first: string, last: string) => Number(BigInt(last) - BigInt(first));

export function SessionList() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const older = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    listSessions({ limit: PAGE }, controller.signal).then(
      (page) => {
        setSessions(page.sessions);
        setNext(page.next);
        setLoading(false);
      },
      (reason: unknown) => {
        if (!controller.signal.aborted) {
          setError(String(reason));
          setLoading(false);
        }
      },
    );
    return () => {
      controller.abort();
      older.current?.abort();
    };
  }, []);

  const loadMore = useCallback(() => {
    if (!next) return;
    older.current?.abort();
    const controller = new AbortController();
    older.current = controller;
    listSessions({ limit: PAGE, before: next }, controller.signal).then(
      (page) => {
        setSessions((current) => {
          const known = new Set(current.map((s) => s.session_id));
          return [...current, ...page.sessions.filter((s) => !known.has(s.session_id))];
        });
        setNext(page.next);
      },
      (reason: unknown) => {
        if (!controller.signal.aborted) setError(String(reason));
      },
    );
  }, [next]);

  if (error) return <p className="error">Could not load sessions: {error}</p>;
  if (!loading && sessions.length === 0)
    return (
      <p className="empty">
        No sessions yet. Traces join a session when a span sets <code>gen_ai.conversation.id</code> or{" "}
        <code>session.id</code>.
      </p>
    );
  return (
    <section>
      <div className="table-wrap">
        <table className="traces">
          <thead>
            <tr>
              <th>Last active</th>
              <th>Session</th>
              <th className="num">Traces</th>
              <th className="num">Span of time</th>
              <th className="num">Model calls</th>
              <th className="num">Input</th>
              <th className="num">Cost</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {sessions.map((s) => (
              <tr key={s.session_id} data-testid="session-row">
                <td className="muted">{timestamp(s.last_start_ns)}</td>
                <td>
                  <a href={links.session(s.session_id)}>{s.session_id}</a>
                  {s.users.length > 0 && <div className="muted small">{s.users.join(", ")}</div>}
                </td>
                <td className="num">{s.traces}</td>
                <td className="num">{duration(span(s.first_start_ns, s.last_end_ns))}</td>
                <td className="num">{s.model_calls}</td>
                <td className="num">{count(s.input_tokens)}</td>
                <td className="num">
                  {s.calls_without_cost > 0 && s.cost_usd !== null ? "≥ " : ""}
                  {usd(s.cost_usd)}
                </td>
                <td>
                  {s.error_count > 0 ? (
                    <span className="badge bad">{s.error_count} error{s.error_count > 1 ? "s" : ""}</span>
                  ) : (
                    <span className="badge ok">ok</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {next && (
        <button type="button" className="more" onClick={loadMore}>
          Load older sessions
        </button>
      )}
    </section>
  );
}

export function SessionView({ sessionId }: { sessionId: string }) {
  const [data, setData] = useState<{ session: SessionSummary; traces: TraceSummary[]; truncated: boolean } | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setData(null);
    setError(null);
    getSession(sessionId, controller.signal).then(setData, (reason: unknown) => {
      if (!controller.signal.aborted) setError(String(reason));
    });
    return () => controller.abort();
  }, [sessionId]);

  if (error) return <p className="error">Could not load session: {error}</p>;
  if (!data) return <p className="muted">Loading…</p>;
  const s = data.session;
  return (
    <section>
      <p>
        <a href="#/sessions">← All sessions</a>
      </p>
      <h1>Session {s.session_id}</h1>
      <p className="muted small">
        {timestamp(s.first_start_ns)} – {timestamp(s.last_start_ns)} · {s.traces} traces · {s.model_calls} model calls ·{" "}
        {count(s.input_tokens)} input tokens · {s.calls_without_cost > 0 && s.cost_usd !== null ? "≥ " : ""}
        {usd(s.cost_usd)}
        {s.users.length > 0 && <> · users {s.users.join(", ")}</>}
        {" · "}
        <a href={links.traces({ session: s.session_id })}>Open in trace list</a>
      </p>
      {data.truncated && <p className="muted small">Showing the most recent {data.traces.length} traces.</p>}
      <ol className="timeline" data-testid="session-traces">
        {data.traces.map((t) => (
          <li key={t.trace_id}>
            <span className="muted small">{timestamp(t.start_ns)}</span>
            <a href={`#/trace/${t.trace_id}`}>{t.root_name || "(unnamed)"}</a>
            <span className="muted small">
              {duration(t.duration_ns)} · {t.model_calls} model calls · {count(t.input_tokens)} in · {usd(t.cost_usd)}
            </span>
            {t.error_count > 0 && <span className="badge bad">{t.error_count} error{t.error_count > 1 ? "s" : ""}</span>}
          </li>
        ))}
      </ol>
    </section>
  );
}

