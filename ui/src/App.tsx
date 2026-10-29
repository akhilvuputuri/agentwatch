import { useEffect, useState } from "react";
import { Dashboard } from "./Dashboard";
import { SessionList, SessionView } from "./Sessions";
import { TraceList } from "./TraceList";
import { TraceView } from "./TraceView";

export type Route =
  | { page: "traces"; session?: string; user?: string }
  | { page: "dashboard" }
  | { page: "sessions" }
  | { page: "session"; sessionId: string }
  | { page: "trace"; traceId: string };

export function route(hash: string): Route {
  if (hash === "#/dashboard") return { page: "dashboard" };
  if (hash === "#/sessions") return { page: "sessions" };
  const session = /^#\/session\/(.+)$/.exec(hash);
  if (session) {
    try {
      return { page: "session", sessionId: decodeURIComponent(session[1]!) };
    } catch {
      return { page: "sessions" };
    }
  }
  const trace = /^#\/trace\/([0-9a-fA-F]{32})$/.exec(hash);
  if (trace) return { page: "trace", traceId: trace[1]!.toLowerCase() };
  const query = /^#\/\?(.*)$/.exec(hash);
  if (query) {
    const params = new URLSearchParams(query[1]);
    const filter: { page: "traces"; session?: string; user?: string } = { page: "traces" };
    const s = params.get("session");
    const u = params.get("user");
    if (s) filter.session = s;
    if (u) filter.user = u;
    return filter;
  }
  return { page: "traces" };
}

export function App() {
  const [current, setCurrent] = useState(() => route(window.location.hash));
  useEffect(() => {
    const onChange = () => setCurrent(route(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  const section =
    current.page === "dashboard" ? "dashboard" : current.page === "sessions" || current.page === "session" ? "sessions" : "traces";
  return (
    <div className="app">
      <header className="top">
        <a className="brand" href="#/">
          agentwatch
        </a>
        <nav className="nav">
          <a href="#/" aria-current={section === "traces" ? "page" : undefined}>
            Traces
          </a>
          <a href="#/sessions" aria-current={section === "sessions" ? "page" : undefined}>
            Sessions
          </a>
          <a href="#/dashboard" aria-current={section === "dashboard" ? "page" : undefined}>
            Dashboard
          </a>
        </nav>
      </header>
      <main>
        {current.page === "dashboard" ? (
          <Dashboard />
        ) : current.page === "sessions" ? (
          <SessionList />
        ) : current.page === "session" ? (
          <SessionView sessionId={current.sessionId} />
        ) : current.page === "trace" ? (
          <TraceView traceId={current.traceId} />
        ) : (
          <TraceList
            key={`${current.session ?? ""}|${current.user ?? ""}`}
            {...(current.session ? { session: current.session } : {})}
            {...(current.user ? { user: current.user } : {})}
          />
        )}
      </main>
    </div>
  );
}
