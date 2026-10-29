import { useEffect, useState } from "react";
import { deleteRun, getRun, links, listRuns, type Run, type RunItemResult, type RunSummary } from "./api";
import { preview } from "./Datasets";
import { duration, scoreText, timestamp, usd } from "./format";

/** The dataset's experiment runs, newest first, with their score means. */
export function RunList({ datasetId }: { datasetId: string }) {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    listRuns(datasetId, controller.signal).then(setRuns, (reason: unknown) => {
      if (!controller.signal.aborted) setError(String(reason));
    });
    return () => controller.abort();
  }, [datasetId]);

  if (error) return <p className="error">{error}</p>;
  if (!runs) return null;
  const names = [...new Set(runs.flatMap((r) => r.scores.map((s) => s.name)))].sort();
  return (
    <>
      <h2 className="section">Experiment runs</h2>
      {runs.length === 0 ? (
        <p className="muted small">
          No runs yet. Run one with <code>agentwatch.client.run_experiment(dataset=…, name=…, task=…)</code>.
        </p>
      ) : (
        <div className="table-wrap">
          <table className="traces" data-testid="run-table">
            <thead>
              <tr>
                <th>Run</th>
                <th className="num">Items</th>
                <th className="num">Errors</th>
                <th className="num">Mean latency</th>
                <th className="num">Cost</th>
                {names.map((n) => (
                  <th key={n} className="num">
                    {n}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.run.id}>
                  <td>
                    <a href={links.run(r.run.id)}>{r.run.name}</a>
                    <div className="muted small">{timestamp(r.run.created_ns)}</div>
                  </td>
                  <td className="num">{r.items}</td>
                  <td className="num">{r.errors}</td>
                  <td className="num">{r.mean_latency_ms === null ? "–" : duration(r.mean_latency_ms * 1e6)}</td>
                  <td className="num">{usd(r.cost_usd)}</td>
                  {names.map((n) => {
                    const s = r.scores.find((x) => x.name === n);
                    return (
                      <td key={n} className="num" title={s ? `${s.count} scores` : undefined}>
                        {s && s.mean !== null ? (Math.round(s.mean * 100) / 100).toString() : "–"}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

/** One run: each item's input, expected and actual output, scores and trace. */
export function RunView({ runId }: { runId: string }) {
  const [data, setData] = useState<{ run: Run; items: RunItemResult[] } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getRun(runId, controller.signal).then(setData, (reason: unknown) => {
      if (!controller.signal.aborted) setError(String(reason));
    });
    return () => controller.abort();
  }, [runId]);

  const remove = () => {
    if (!data || !window.confirm(`Delete the run "${data.run.name}"? Its traces and scores stay.`)) return;
    deleteRun(data.run.id).then(
      () => {
        window.location.hash = links.dataset(data.run.dataset_id);
      },
      (reason: unknown) => setError(String(reason)),
    );
  };

  if (error) return <p className="error">{error}</p>;
  if (!data) return <p className="muted">Loading…</p>;
  return (
    <section>
      <p>
        <a href={links.dataset(data.run.dataset_id)}>← Dataset</a>
      </p>
      <h1>Run {data.run.name}</h1>
      <p className="muted small">
        {timestamp(data.run.created_ns)} · {data.items.length} items
        {Object.keys(data.run.metadata).length > 0 && <> · {JSON.stringify(data.run.metadata)}</>}
      </p>
      <div className="toolbar">
        <button type="button" onClick={remove}>
          Delete run
        </button>
      </div>
      <div className="table-wrap">
        <table className="traces items" data-testid="run-items">
          <thead>
            <tr>
              <th>Input</th>
              <th>Expected</th>
              <th>Output</th>
              <th>Scores</th>
              <th className="num">Latency</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {data.items.map((item) => (
              <tr key={item.item_id}>
                <td title={JSON.stringify(item.input)}>{preview(item.input)}</td>
                <td title={JSON.stringify(item.expected_output)}>{preview(item.expected_output)}</td>
                <td title={JSON.stringify(item.output)}>
                  {item.error ? <span className="error">{item.error}</span> : preview(item.output)}
                </td>
                <td>
                  <div className="score-badges">
                    {item.scores.map((s) => (
                      <span key={s.id} className="score-badge" title={s.comment ?? undefined}>
                        {s.name} {scoreText(s.data_type, s.value, s.label)}
                      </span>
                    ))}
                  </div>
                </td>
                <td className="num">{duration(item.latency_ms * 1e6)}</td>
                <td>
                  <a href={`#/trace/${item.trace_id}`}>trace</a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
