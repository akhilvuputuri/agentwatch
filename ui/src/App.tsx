import { useEffect, useState } from "react";
import { Alerts } from "./Alerts";
import { getAuthSession, signOut, type TraceFilter } from "./api";
import { CompareView } from "./Compare";
import { Dashboard } from "./Dashboard";
import { DatasetList, DatasetView } from "./Datasets";
import { Drift } from "./Drift";
import { Judges } from "./Judges";
import { MemoryItemView, MemoryList } from "./Memory";
import { Prices } from "./Prices";
import { RunView } from "./Runs";
import { SessionList, SessionView } from "./Sessions";
import { TraceList } from "./TraceList";
import { TraceView } from "./TraceView";

export type Route =
  | ({ page: "traces" } & TraceFilter)
  | { page: "dashboard"; range?: string; service?: string }
  | { page: "prices" }
  | { page: "judges" }
  | { page: "alerts" }
  | { page: "drift" }
  | { page: "memory" }
  | { page: "memory-item"; store: string; id: string }
  | { page: "datasets" }
  | { page: "dataset"; datasetId: string }
  | { page: "run"; runId: string }
  | { page: "compare"; baseline: string; candidate: string }
  | { page: "sessions" }
  | { page: "session"; sessionId: string }
  | { page: "trace"; traceId: string; spanId?: string };

export function route(hash: string): Route {
  const dashboard = /^#\/dashboard(?:\?(.*))?$/.exec(hash);
  if (dashboard) {
    const params = new URLSearchParams(dashboard[1] ?? "");
    const view: { page: "dashboard"; range?: string; service?: string } = { page: "dashboard" };
    const range = params.get("range");
    const service = params.get("service");
    if (range) view.range = range;
    if (service) view.service = service;
    return view;
  }
  if (hash === "#/sessions") return { page: "sessions" };
  if (hash === "#/prices") return { page: "prices" };
  if (hash === "#/judges") return { page: "judges" };
  if (hash === "#/alerts") return { page: "alerts" };
  if (hash === "#/drift") return { page: "drift" };
  if (hash === "#/datasets") return { page: "datasets" };
  if (hash === "#/memory") return { page: "memory" };
  const memoryItem = /^#\/memory\/item\?(.*)$/.exec(hash);
  if (memoryItem) {
    const params = new URLSearchParams(memoryItem[1]);
    const id = params.get("id");
    return id ? { page: "memory-item", store: params.get("store") ?? "", id } : { page: "memory" };
  }
  const compare = /^#\/compare\/([^/]+)\/([^/]+)$/.exec(hash);
  if (compare) {
    try {
      return {
        page: "compare",
        baseline: decodeURIComponent(compare[1]!),
        candidate: decodeURIComponent(compare[2]!),
      };
    } catch {
      return { page: "datasets" };
    }
  }
  const run = /^#\/run\/(.+)$/.exec(hash);
  if (run) {
    try {
      return { page: "run", runId: decodeURIComponent(run[1]!) };
    } catch {
      return { page: "datasets" };
    }
  }
  const dataset = /^#\/dataset\/(.+)$/.exec(hash);
  if (dataset) {
    try {
      return { page: "dataset", datasetId: decodeURIComponent(dataset[1]!) };
    } catch {
      return { page: "datasets" };
    }
  }
  const session = /^#\/session\/(.+)$/.exec(hash);
  if (session) {
    try {
      return { page: "session", sessionId: decodeURIComponent(session[1]!) };
    } catch {
      return { page: "sessions" };
    }
  }
  const trace = /^#\/trace\/([0-9a-fA-F]{32})(?:\/([0-9a-fA-F]{16}))?$/.exec(hash);
  if (trace) {
    const found: { page: "trace"; traceId: string; spanId?: string } = { page: "trace", traceId: trace[1]!.toLowerCase() };
    if (trace[2]) found.spanId = trace[2].toLowerCase();
    return found;
  }
  const query = /^#\/\?(.*)$/.exec(hash);
  if (query) {
    const params = new URLSearchParams(query[1]);
    const filter: { page: "traces" } & TraceFilter = { page: "traces" };
    const s = params.get("session");
    const u = params.get("user");
    const service = params.get("service");
    if (s) filter.session = s;
    if (u) filter.user = u;
    if (service) filter.service = service;
    if (params.get("errors") === "1") filter.errors = true;
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
  const [user, setUser] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    getAuthSession(controller.signal)
      .then((session) => setUser(session.user))
      .catch(() => undefined);
    return () => controller.abort();
  }, []);
  const section =
    current.page === "dashboard" || current.page === "prices" || current.page === "judges" || current.page === "alerts" || current.page === "drift"
      ? current.page
      : current.page === "memory" || current.page === "memory-item"
        ? "memory"
        : current.page === "sessions" || current.page === "session"
        ? "sessions"
        : current.page === "datasets" ||
            current.page === "dataset" ||
            current.page === "run" ||
            current.page === "compare"
          ? "datasets"
          : "traces";
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
          <a href="#/memory" aria-current={section === "memory" ? "page" : undefined}>
            Memory
          </a>
          <a href="#/datasets" aria-current={section === "datasets" ? "page" : undefined}>
            Datasets
          </a>
          <a href="#/dashboard" aria-current={section === "dashboard" ? "page" : undefined}>
            Dashboard
          </a>
          <a href="#/judges" aria-current={section === "judges" ? "page" : undefined}>
            Judges
          </a>
          <a href="#/drift" aria-current={section === "drift" ? "page" : undefined}>
            Drift
          </a>
          <a href="#/alerts" aria-current={section === "alerts" ? "page" : undefined}>
            Alerts
          </a>
          <a href="#/prices" aria-current={section === "prices" ? "page" : undefined}>
            Prices
          </a>
        </nav>
        {user !== null ? (
          <button type="button" className="signout" title={`Signed in as ${user}`} onClick={() => void signOut()}>
            Sign out
          </button>
        ) : null}
      </header>
      <main>
        {current.page === "dashboard" ? (
          <Dashboard
            {...(current.range ? { range: current.range } : {})}
            {...(current.service ? { service: current.service } : {})}
          />
        ) : current.page === "prices" ? (
          <Prices />
        ) : current.page === "judges" ? (
          <Judges />
        ) : current.page === "alerts" ? (
          <Alerts />
        ) : current.page === "drift" ? (
          <Drift />
        ) : current.page === "memory" ? (
          <MemoryList />
        ) : current.page === "memory-item" ? (
          <MemoryItemView key={`${current.store}|${current.id}`} store={current.store} id={current.id} />
        ) : current.page === "datasets" ? (
          <DatasetList />
        ) : current.page === "dataset" ? (
          <DatasetView datasetId={current.datasetId} />
        ) : current.page === "run" ? (
          <RunView runId={current.runId} />
        ) : current.page === "compare" ? (
          <CompareView baseline={current.baseline} candidate={current.candidate} />
        ) : current.page === "sessions" ? (
          <SessionList />
        ) : current.page === "session" ? (
          <SessionView sessionId={current.sessionId} />
        ) : current.page === "trace" ? (
          <TraceView traceId={current.traceId} {...(current.spanId ? { spanId: current.spanId } : {})} />
        ) : (
          <TraceList
            key={`${current.session ?? ""}|${current.user ?? ""}`}
            {...(current.session ? { session: current.session } : {})}
            {...(current.user ? { user: current.user } : {})}
            {...(current.service ? { service: current.service } : {})}
            {...(current.errors ? { errors: true } : {})}
          />
        )}
      </main>
    </div>
  );
}
