import { useEffect, useState } from "react";
import {
  getDriftAround,
  getDriftSummary,
  listChanges,
  listServices,
  type DetectedChange,
  type DriftComparison,
  type DriftMetric,
  type DriftSummary,
  type DriftWindows,
} from "./api";
import { count, duration, timestamp, usd } from "./format";

const DAYS = [1, 7, 30];
const DAY_NS = 24n * 3600n * 1_000_000_000n;
const RETRY_MS = 3000;
const RETRIES = 20;

const LABELS: Record<DriftMetric, string> = {
  model_error_rate: "Model error rate",
  tool_error_rate: "Tool error rate",
  model_mean_ms: "Model mean latency",
  model_p95_ms: "Model p95 latency",
  cost_per_trace: "Cost per trace",
  score_mean: "Score",
};

/** Before and after a change, the same length each side: `days`, or less if it is recent. Null if it has no "after". */
export function beforeAfter(firstSeenNs: string, days: number, nowNs: bigint): DriftWindows | null {
  const at = BigInt(firstSeenNs);
  const available = nowNs - at;
  if (available <= 0n || at <= 0n) return null;
  let length = BigInt(days) * DAY_NS;
  if (available < length) length = available;
  if (at < length) length = at;
  return {
    baseline_from: String(at - length),
    baseline_to: String(at),
    current_from: String(at),
    current_to: String(at + length),
  };
}

/** The service to compare a change within: its own when it has one, as a service version does. */
export function changeService(change: DetectedChange, selected: string): string {
  return change.service ?? selected;
}

/** The server runs one comparison at a time and answers 429 meanwhile, so a page waits its turn. */
async function waitingTurn<T>(work: () => Promise<T>, signal: AbortSignal): Promise<T> {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await work();
    } catch (reason) {
      if (attempt >= RETRIES || signal.aborted || !String(reason).includes("429")) throw reason;
      await new Promise((resolve) => window.setTimeout(resolve, RETRY_MS));
    }
  }
}

function figure(c: DriftComparison, value: number | null): string {
  if (value === null) return "–";
  if (c.proportion) return `${Math.round(value * 1000) / 10}%`;
  if (c.metric === "cost_per_trace") return usd(Math.abs(value));
  if (c.metric === "model_mean_ms" || c.metric === "model_p95_ms") return duration(Math.abs(value) * 1e6);
  return String(Math.round(Math.abs(value) * 1000) / 1000);
}

function signed(c: DriftComparison, value: number | null): string {
  if (value === null) return "–";
  return `${value < 0 ? "−" : "+"}${figure(c, Math.abs(value))}`;
}

/** The verdict in words; it never says better or worse, since only the reader knows which way is bad. */
function verdictText(c: DriftComparison): string {
  if (c.verdict === "underpowered") return "too little data: under 10 traces on a side";
  if (c.verdict === "changed") return `changed: ${c.direction}`;
  if (c.detectable === null) return "no detectable difference; can't say how large a change would show";
  return `no detectable difference; a change of about ${figure(c, c.detectable)} would usually show`;
}

const label = (c: DriftComparison) => (c.score_name ? `${LABELS.score_mean}: ${c.score_name}` : LABELS[c.metric]);

/** How the page's intervals allow for testing several metrics at once. */
const levelText = (summary: DriftSummary) =>
  `Each interval here is at ${Math.round(summary.confidence * 10000) / 100}%, not 95%, because ${summary.comparisons.length} comparisons are tested at once.`;

function ComparisonTable({ comparisons, testId }: { comparisons: DriftComparison[]; testId: string }) {
  return (
    <div className="table-wrap">
      <table className="traces" data-testid={testId}>
        <thead>
          <tr>
            <th>Metric</th>
            <th className="num">Baseline</th>
            <th className="num">Current</th>
            <th className="num">Difference</th>
            <th className="num">Traces</th>
            <th>Verdict</th>
          </tr>
        </thead>
        <tbody>
          {comparisons.map((c) => (
            <tr key={label(c)}>
              <td>{label(c)}</td>
              <td className="num">{figure(c, c.baseline_value)}</td>
              <td className="num">{figure(c, c.current_value)}</td>
              <td className="num">
                {signed(c, c.difference)}
                {c.interval && (
                  <div className="muted small">
                    {signed(c, c.interval[0])} to {signed(c, c.interval[1])}
                  </div>
                )}
              </td>
              <td className="num">
                {count(c.baseline_traces)} / {count(c.current_traces)}
                {c.truncated && <div className="muted small">most recent only</div>}
              </td>
              <td>
                <span className={`badge ${c.verdict === "changed" ? "warn" : "neutral"}`}>{c.verdict === "changed" ? "changed" : c.verdict === "underpowered" ? "too little data" : "no difference seen"}</span>
                <div className="muted small">{verdictText(c)}</div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** A metric now against the same length before, and what changed in between. */
export function Drift() {
  const [days, setDays] = useState(7);
  const [service, setService] = useState("");
  const [services, setServices] = useState<string[]>([]);
  const [summary, setSummary] = useState<DriftSummary | null>(null);
  const [changes, setChanges] = useState<DetectedChange[] | null>(null);
  const [chosen, setChosen] = useState<DetectedChange | null>(null);
  const [around, setAround] = useState<DriftSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    listServices(controller.signal).then(setServices, () => undefined);
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const fail = (reason: unknown) => {
      if (!controller.signal.aborted) setError(String(reason));
    };
    setError(null);
    setSummary(null);
    waitingTurn(() => getDriftSummary(days, service, controller.signal), controller.signal).then(setSummary, fail);
    listChanges(service, controller.signal).then(setChanges, fail);
    return () => controller.abort();
  }, [days, service]);

  useEffect(() => {
    setAround(null);
    if (!chosen) return;
    const windows = beforeAfter(chosen.first_seen_ns, days, BigInt(Date.now()) * 1_000_000n);
    if (!windows) return;
    const controller = new AbortController();
    // All traffic of the change's service, with scores, since a new model moves those most.
    waitingTurn(() => getDriftAround(windows, changeService(chosen, service), controller.signal), controller.signal).then(setAround, (reason: unknown) => {
      if (!controller.signal.aborted) setError(String(reason));
    });
    return () => controller.abort();
  }, [chosen, days, service]);

  return (
    <section>
      <h1>Drift</h1>
      <p className="muted small">
        Each metric now, against the same length of time directly before. Traffic differs between the two, so this resamples
        whole traces: a metric reads as changed only if the interval of the difference excludes zero. That says it moved,
        not why, nor whether that is bad: two windows can differ because of the day of the week, and a day's rate varies
        by more than this allows for. "No difference seen" gives the size of change that would usually show.
      </p>
      <div className="toolbar">
        <label>
          Compare the last{" "}
          <select value={days} onChange={(event) => setDays(Number(event.target.value))}>
            {DAYS.map((d) => (
              <option key={d} value={d}>
                {d === 1 ? "1 day" : `${d} days`}
              </option>
            ))}
          </select>{" "}
          with the same before
        </label>{" "}
        <label>
          Service{" "}
          <select value={service} onChange={(event) => setService(event.target.value)}>
            <option value="">All</option>
            {services.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
      </div>
      {error && <p className="error">{error}</p>}
      {!summary && !error && <p className="muted">Comparing…</p>}
      {summary && <p className="muted small">{levelText(summary)}</p>}
      {summary && <ComparisonTable comparisons={summary.comparisons} testId="drift-table" />}
      <h2>Detected changes</h2>
      {changes && changes.length === 0 && <p className="empty">No models or service versions seen yet.</p>}
      {changes && changes.length > 0 && (
        <div className="table-wrap">
          <table className="traces" data-testid="change-table">
            <thead>
              <tr>
                <th>First seen</th>
                <th>Kind</th>
                <th>Name</th>
              </tr>
            </thead>
            <tbody>
              {changes.map((c) => (
                <tr key={`${c.kind}|${c.service ?? ""}|${c.name}`} aria-selected={chosen === c}>
                  <td className="muted">{timestamp(c.first_seen_ns)}</td>
                  <td>{c.kind === "model" ? "model" : "service version"}</td>
                  <td>
                    <button type="button" className="link" onClick={() => setChosen(c)}>
                      {c.name}
                    </button>
                    {c.service && <span className="muted small"> · {c.service}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {chosen && (
        <>
          <h2>
            Before and after {chosen.name} ({timestamp(chosen.first_seen_ns)})
          </h2>
          {around && <p className="muted small">{levelText(around)}</p>}
          {around ? <ComparisonTable comparisons={around.comparisons} testId="around-table" /> : <p className="muted">Comparing…</p>}
        </>
      )}
    </section>
  );
}
