import { useEffect, useState } from "react";
import { TraceList } from "./TraceList";
import { TraceView } from "./TraceView";

function route(): { traceId: string | null } {
  const match = /^#\/trace\/([0-9a-fA-F]{32})$/.exec(window.location.hash);
  return { traceId: match ? match[1]!.toLowerCase() : null };
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
        <span className="subtitle">traces</span>
      </header>
      <main>{current.traceId ? <TraceView traceId={current.traceId} /> : <TraceList />}</main>
    </div>
  );
}
