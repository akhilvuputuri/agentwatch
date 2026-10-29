import { useEffect, useState } from "react";
import { Dashboard } from "./Dashboard";
import { TraceList } from "./TraceList";
import { TraceView } from "./TraceView";

type Route = { page: "traces" } | { page: "dashboard" } | { page: "trace"; traceId: string };

function route(): Route {
  const hash = window.location.hash;
  if (hash === "#/dashboard") return { page: "dashboard" };
  const match = /^#\/trace\/([0-9a-fA-F]{32})$/.exec(hash);
  return match ? { page: "trace", traceId: match[1]!.toLowerCase() } : { page: "traces" };
}

export function App() {
  const [current, setCurrent] = useState(route);
  useEffect(() => {
    const onChange = () => setCurrent(route());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return (
    <div className="app">
      <header className="top">
        <a className="brand" href="#/">
          agentwatch
        </a>
        <nav className="nav">
          <a href="#/" aria-current={current.page !== "dashboard" ? "page" : undefined}>
            Traces
          </a>
          <a href="#/dashboard" aria-current={current.page === "dashboard" ? "page" : undefined}>
            Dashboard
          </a>
        </nav>
      </header>
      <main>
        {current.page === "dashboard" ? (
          <Dashboard />
        ) : current.page === "trace" ? (
          <TraceView traceId={current.traceId} />
        ) : (
          <TraceList />
        )}
      </main>
    </div>
  );
}
