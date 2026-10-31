import { useEffect, useState, type FormEvent } from "react";
import {
  createMonitor,
  deleteMonitor,
  listAlerts,
  listMonitors,
  updateMonitor,
  type Alert,
  type Monitor,
  type MonitorMetric,
  type NewMonitor,
} from "./api";
import { count, duration, timestamp, usd } from "./format";

const METRICS: { value: MonitorMetric; label: string }[] = [
  { value: "model_error_rate", label: "Model error rate" },
  { value: "tool_error_rate", label: "Tool error rate" },
  { value: "cost_usd", label: "Cost (USD)" },
  { value: "model_p95_ms", label: "Model p95 latency (ms)" },
  { value: "trace_count", label: "Trace count" },
  { value: "score_mean", label: "Score mean" },
];

const metricLabel = (metric: MonitorMetric) => METRICS.find((m) => m.value === metric)?.label ?? metric;

/** A figure in the metric's unit. `rate` is decided by the caller: a score's mean is a rate only sometimes. */
function figure(metric: MonitorMetric, value: number | null, rate: boolean): string {
  if (value === null) return "–";
  if (rate) return `${Math.round(value * 1000) / 10}%`;
  if (metric === "cost_usd") return usd(value);
  if (metric === "model_p95_ms") return duration(value * 1e6);
  if (metric === "trace_count") return count(value);
  return String(Math.round(value * 1000) / 1000);
}

const isErrorRate = (metric: MonitorMetric) => metric === "model_error_rate" || metric === "tool_error_rate";

function windowText(minutes: number): string {
  if (minutes % 1440 === 0) return minutes === 1440 ? "24 hours" : `${minutes / 1440} days`;
  if (minutes % 60 === 0) return minutes === 60 ? "1 hour" : `${minutes / 60} hours`;
  return `${minutes} min`;
}

const usesMinSamples = (metric: MonitorMetric) => metric !== "cost_usd" && metric !== "trace_count";

/** Monitors over recent metrics, and the alerts they raised. */
export function Alerts() {
  const [monitors, setMonitors] = useState<Monitor[] | null>(null);
  const [alerts, setAlerts] = useState<Alert[] | null>(null);
  // Apart, so a refresh that works clears a failed refresh but not what a person needs to fix.
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const load = () =>
      Promise.all([listMonitors(controller.signal), listAlerts(controller.signal)]).then(
        ([foundMonitors, foundAlerts]) => {
          setMonitors(foundMonitors);
          setAlerts(foundAlerts);
          setLoadError(null);
        },
        (reason: unknown) => {
          if (!controller.signal.aborted) setLoadError(String(reason));
        },
      );
    void load();
    // Monitors run in the background; keep their state current.
    const timer = window.setInterval(() => void load(), 15_000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, []);

  const toggle = (monitor: Monitor) => {
    if (
      monitor.enabled &&
      monitor.state.state === "FIRING" &&
      !window.confirm(`Disable "${monitor.name}"? It is firing, so a resolved alert is sent.`)
    )
      return;
    updateMonitor(monitor.id, { enabled: !monitor.enabled }).then(
      (updated) => {
        setMonitors((current) => (current ?? []).map((m) => (m.id === updated.id ? updated : m)));
        setActionError(null);
      },
      (reason: unknown) => setActionError(String(reason)),
    );
  };

  const remove = (monitor: Monitor) => {
    const firing = monitor.state.state === "FIRING" ? " It is firing, so a resolved alert is sent." : "";
    if (!window.confirm(`Delete the monitor "${monitor.name}"? The alerts it raised stay.${firing}`)) return;
    deleteMonitor(monitor.id).then(
      () => {
        setMonitors((current) => (current ?? []).filter((m) => m.id !== monitor.id));
        setActionError(null);
      },
      (reason: unknown) => setActionError(String(reason)),
    );
  };

  return (
    <section>
      <h1>Alerts</h1>
      <p className="muted small">
        A monitor compares a metric over a trailing window with a threshold, every minute. Rates fire only when the
        statistical bound (95% Wilson) clears the threshold, not on the value alone, so a few unlucky calls cannot set
        one off. A firing monitor resolves once its value has stayed on the good side for its cooldown; evaluations with
        too few samples do not count toward that. A client collects alerts by polling the API: agentwatch sends nothing
        itself.
      </p>
      {loadError && <p className="error">{loadError}</p>}
      {actionError && <p className="error">{actionError}</p>}
      <MonitorForm
        onAdded={(monitor) => {
          setMonitors((current) => [...(current ?? []), monitor]);
          setActionError(null);
        }}
        onError={setActionError}
        onEdit={() => setActionError(null)}
      />
      {monitors && monitors.length === 0 && <p className="empty">No monitors yet.</p>}
      {monitors && monitors.length > 0 && (
        <div className="table-wrap">
          <table className="traces" data-testid="monitor-table">
            <thead>
              <tr>
                <th>Monitor</th>
                <th>Rule</th>
                <th>State</th>
                <th className="num">Value</th>
                <th className="num">Bound</th>
                <th className="num">Samples</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {monitors.map((m) => (
                <MonitorRow key={m.id} monitor={m} onToggle={() => toggle(m)} onRemove={() => remove(m)} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <h2>Recent alerts</h2>
      {alerts && alerts.length === 0 && <p className="empty">No alerts yet.</p>}
      {alerts && alerts.length > 0 && (
        <div className="table-wrap">
          <table className="traces" data-testid="alert-table">
            <thead>
              <tr>
                <th>When</th>
                <th>Monitor</th>
                <th>Kind</th>
                <th className="num">Value</th>
                <th>Threshold</th>
                <th>Delivered</th>
              </tr>
            </thead>
            <tbody>
              {alerts.map((a) => {
                const rate = isErrorRate(a.metric) || a.bound !== null;
                return (
                  <tr key={a.id}>
                    <td className="muted">{timestamp(a.created_ns)}</td>
                    <td>
                      {a.monitor_name}
                      <div className="muted small">{metricLabel(a.metric)}</div>
                    </td>
                    <td>
                      <span className={`badge ${a.kind === "firing" ? "bad" : "ok"}`}>{a.kind}</span>
                      {a.reason && <div className="muted small">monitor {a.reason}</div>}
                    </td>
                    <td className="num">{figure(a.metric, a.value, rate)}</td>
                    <td>
                      {a.comparison} {figure(a.metric, a.threshold, rate)}
                    </td>
                    <td className="muted">{a.delivered_ns === null ? "not yet" : timestamp(a.delivered_ns)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function MonitorRow({
  monitor,
  onToggle,
  onRemove,
}: {
  monitor: Monitor;
  onToggle: () => void;
  onRemove: () => void;
}) {
  const s = monitor.state;
  const rate = isErrorRate(monitor.metric) || s.bound !== null;
  return (
    <tr>
      <td>
        {monitor.name}
        <div className="muted small">
          {metricLabel(monitor.metric)}
          {monitor.score_name ? ` · ${monitor.score_name}` : ""}
          {monitor.service ? ` · ${monitor.service}` : ""}
        </div>
      </td>
      <td>
        {monitor.comparison} {figure(monitor.metric, monitor.threshold, rate)}
        <div className="muted small">
          over {windowText(monitor.window_minutes)}
          {usesMinSamples(monitor.metric) ? ` · at least ${count(monitor.min_samples)} samples` : ""}
          {monitor.cooldown_minutes === 0 ? " · no cooldown" : ` · cooldown ${windowText(monitor.cooldown_minutes)}`}
        </div>
      </td>
      <td>
        {monitor.enabled ? (
          <span className={`badge ${s.state === "FIRING" ? "bad" : "ok"}`}>{s.state}</span>
        ) : (
          <span className="badge neutral">off</span>
        )}
        {monitor.enabled && s.insufficient && <div className="muted small">too few samples</div>}
        {monitor.enabled && s.state === "FIRING" && s.good_since_ns !== null && (
          <div className="muted small">recovering since {timestamp(s.good_since_ns)}</div>
        )}
      </td>
      <td className="num">{figure(monitor.metric, s.value, rate)}</td>
      <td className="num">{s.bound === null ? "–" : figure(monitor.metric, s.bound, true)}</td>
      <td className="num">{monitor.metric === "trace_count" ? "–" : count(s.samples)}</td>
      <td>
        <button type="button" className="link" onClick={onToggle}>
          {monitor.enabled ? "Disable" : "Enable"}
        </button>{" "}
        <button type="button" className="link" onClick={onRemove} aria-label={`Delete monitor ${monitor.name}`}>
          Delete
        </button>
      </td>
    </tr>
  );
}

function MonitorForm({
  onAdded,
  onError,
  onEdit,
}: {
  onAdded: (monitor: Monitor) => void;
  onError: (message: string) => void;
  onEdit: () => void;
}) {
  const [name, setName] = useState("");
  const [metric, setMetric] = useState<MonitorMetric>("model_error_rate");
  const [scoreName, setScoreName] = useState("");
  const [windowMinutes, setWindowMinutes] = useState("60");
  const [comparison, setComparison] = useState<NewMonitor["comparison"]>("above");
  const [threshold, setThreshold] = useState("");
  // Blank takes the server's default for the metric.
  const [minSamples, setMinSamples] = useState("");
  const [cooldown, setCooldown] = useState("60");
  const [service, setService] = useState("");
  const [saving, setSaving] = useState(false);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const minutes = Number(windowMinutes);
    const limit = Number(threshold);
    const samples = Number(minSamples);
    const cooldownMinutes = Number(cooldown);
    if (!Number.isInteger(minutes) || minutes < 1) return onError("The window must be a whole number of minutes.");
    if (threshold.trim() === "" || !Number.isFinite(limit)) return onError("The threshold must be a number.");
    if (minSamples.trim() !== "" && (!Number.isInteger(samples) || samples < 1)) return onError("Min samples must be a whole number of at least 1.");
    if (cooldown.trim() === "" || !Number.isInteger(cooldownMinutes) || cooldownMinutes < 0) return onError("The cooldown must be a whole number of minutes, or 0.");
    setSaving(true);
    createMonitor({
      name: name.trim(),
      metric,
      score_name: metric === "score_mean" ? scoreName.trim() : null,
      service: service.trim() || null,
      window_minutes: minutes,
      comparison,
      threshold: limit,
      cooldown_minutes: cooldownMinutes,
      ...(minSamples.trim() === "" || !usesMinSamples(metric) ? {} : { min_samples: samples }),
    }).then(
      (monitor) => {
        setSaving(false);
        setName("");
        onAdded(monitor);
      },
      (reason: unknown) => {
        setSaving(false);
        onError(String(reason));
      },
    );
  };

  return (
    <form className="score-form" onSubmit={submit} onChange={onEdit} aria-label="Add monitor">
      <label>
        Name <input value={name} onChange={(e) => setName(e.target.value)} placeholder="model errors" required maxLength={200} />
      </label>
      <label>
        Metric
        <select value={metric} onChange={(e) => setMetric(e.target.value as MonitorMetric)}>
          {METRICS.map((m) => (
            <option key={m.value} value={m.value}>
              {m.label}
            </option>
          ))}
        </select>
      </label>
      {metric === "score_mean" && (
        <label>
          Score name <input value={scoreName} onChange={(e) => setScoreName(e.target.value)} placeholder="helpful" required maxLength={200} />
        </label>
      )}
      <label>
        Window (minutes) <input value={windowMinutes} onChange={(e) => setWindowMinutes(e.target.value)} inputMode="numeric" size={6} />
      </label>
      <label>
        Fires when
        <select value={comparison} onChange={(e) => setComparison(e.target.value as NewMonitor["comparison"])}>
          <option value="above">above</option>
          <option value="below">below</option>
        </select>
      </label>
      <label>
        Threshold{isErrorRate(metric) ? " (0.2 is 20%)" : ""} <input value={threshold} onChange={(e) => setThreshold(e.target.value)} inputMode="decimal" size={8} required />
      </label>
      {usesMinSamples(metric) && (
        <label>
          Min samples <input value={minSamples} onChange={(e) => setMinSamples(e.target.value)} inputMode="numeric" size={6} placeholder={isErrorRate(metric) ? "3" : "10"} />
        </label>
      )}
      <label>
        Cooldown (minutes) <input value={cooldown} onChange={(e) => setCooldown(e.target.value)} inputMode="numeric" size={6} />
      </label>
      <label>
        Service <input value={service} onChange={(e) => setService(e.target.value)} placeholder="all" size={12} />
      </label>
      <button type="submit" disabled={saving || !name.trim() || !threshold.trim() || (metric === "score_mean" && !scoreName.trim())}>
        {saving ? "Saving…" : "Add monitor"}
      </button>
    </form>
  );
}
