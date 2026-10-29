import { useEffect, useState } from "react";
import { compareRuns, links, type BinaryComparison, type Comparison, type NumericComparison, type Verdict } from "./api";
import { preview } from "./Datasets";
import { duration, scoreText } from "./format";

const VERDICT_CLASS: Record<Verdict, string> = {
  better: "ok",
  worse: "bad",
  "no detectable difference": "neutral",
  underpowered: "warn",
};
const VERDICT_ICON: Record<Verdict, string> = {
  better: "▲",
  worse: "▼",
  "no detectable difference": "=",
  underpowered: "?",
};

const pct = (value: number | null) => (value === null ? "–" : `${(value * 100).toFixed(1)}%`);
const signedPct = (value: number | null) =>
  value === null ? "–" : `${value > 0 ? "+" : ""}${(value * 100).toFixed(1)} pts`;
const num = (value: number | null) => (value === null ? "–" : Number(value.toPrecision(3)).toString());
const signed = (value: number | null) => (value === null ? "–" : `${value > 0 ? "+" : ""}${num(value)}`);

export function VerdictBadge({ verdict }: { verdict: Verdict }) {
  // Icon and label together, so the verdict never rests on colour alone.
  return (
    <span className={`badge verdict ${VERDICT_CLASS[verdict]}`} data-testid="verdict">
      {VERDICT_ICON[verdict]} {verdict}
    </span>
  );
}

function Notes({ c }: { c: BinaryComparison | NumericComparison }) {
  return (
    <>
      {c.below_margin && (
        <div className="tile-note">Real but smaller than the change that matters.</div>
      )}
      {c.dropped_failures > 0 && (
        <div className="tile-note warn-text">
          {c.dropped_failures} item{c.dropped_failures > 1 ? "s" : ""} left out because a task failed; see "task succeeded".
        </div>
      )}
    </>
  );
}

function BinaryCard({ c }: { c: BinaryComparison }) {
  return (
    <div className="tile compare-card" data-testid="compare-card">
      <div className="compare-head">
        <strong>{c.name}</strong> <VerdictBadge verdict={c.verdict} />
      </div>
      <div className="compare-values">
        {pct(c.baseline_rate)} → <strong>{pct(c.candidate_rate)}</strong> pass rate
      </div>
      <div className="tile-note">
        {signedPct(c.difference)}
        {c.difference_interval && ` (95% CI ${signedPct(c.difference_interval[0])} to ${signedPct(c.difference_interval[1])})`}
      </div>
      <div className="tile-note">
        {c.only_candidate} improved · {c.only_baseline} regressed · {c.pairs} pairs · exact McNemar p ={" "}
        {c.p_value < 0.001 ? "<0.001" : c.p_value.toFixed(3)}
      </div>
      <div className="tile-note">
        Could detect a change of {c.minimum_detectable === null ? "–" : signedPct(c.minimum_detectable).replace("+", "")} (80%
        power); a change of {signedPct(c.margin).replace("+", "")} matters
      </div>
      <Notes c={c} />
    </div>
  );
}

function NumericCard({ c }: { c: NumericComparison }) {
  const fmt = c.unit === "ms" ? (v: number | null) => (v === null ? "–" : duration(v * 1e6)) : num;
  return (
    <div className="tile compare-card" data-testid="compare-card">
      <div className="compare-head">
        <strong>{c.name}</strong> <VerdictBadge verdict={c.verdict} />
      </div>
      <div className="compare-values">
        {fmt(c.baseline_mean)} → <strong>{fmt(c.candidate_mean)}</strong> mean
        {c.lower_is_better && <span className="muted small"> (lower is better)</span>}
      </div>
      <div className="tile-note">
        {c.unit === "ms" ? (c.difference === null ? "–" : `${c.difference > 0 ? "+" : "−"}${fmt(Math.abs(c.difference))}`) : signed(c.difference)}
        {c.difference_interval &&
          c.unit !== "ms" &&
          ` (95% CI ${signed(c.difference_interval[0])} to ${signed(c.difference_interval[1])})`}
      </div>
      <div className="tile-note">
        {c.improved} improved · {c.regressed} regressed · {c.pairs} pairs
      </div>
      <div className="tile-note">
        Could detect {c.minimum_detectable === null ? "–" : fmt(c.minimum_detectable)}; a change of {fmt(c.margin)} matters
      </div>
      <Notes c={c} />
    </div>
  );
}

export function CompareView({ baseline, candidate }: { baseline: string; candidate: string }) {
  const [data, setData] = useState<Comparison | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [onlyChanged, setOnlyChanged] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    setData(null);
    compareRuns(baseline, candidate, controller.signal).then(setData, (reason: unknown) => {
      if (!controller.signal.aborted) setError(String(reason));
    });
    return () => controller.abort();
  }, [baseline, candidate]);

  if (error) return <p className="error">Could not compare: {error}</p>;
  if (!data) return <p className="muted">Comparing…</p>;
  const items = onlyChanged ? data.items.filter((i) => Object.keys(i.flips).length > 0) : data.items;
  return (
    <section>
      <p>
        <a href={links.dataset(data.baseline.dataset_id)}>← Dataset</a>
      </p>
      <h1>
        <a href={links.run(data.baseline.id)}>{data.baseline.name}</a> vs{" "}
        <a href={links.run(data.candidate.id)}>{data.candidate.name}</a>
      </h1>
      <p className="muted small">
        {data.pairs} items compared in both runs
        {data.only_baseline + data.only_candidate > 0 &&
          ` · ${data.only_baseline} only in ${data.baseline.name}, ${data.only_candidate} only in ${data.candidate.name}`}
        . A difference counts as real only when it is unlikely to be noise; "underpowered" means too few items to tell.
      </p>
      <div className="tiles compare-tiles">
        {data.comparisons.map((c) =>
          c.kind === "binary" ? <BinaryCard key={c.name} c={c} /> : <NumericCard key={c.name} c={c} />,
        )}
        <NumericCard c={data.latency} />
      </div>
      <div className="toolbar">
        <h2 className="section">Items</h2>
        <label>
          <input type="checkbox" checked={onlyChanged} onChange={(e) => setOnlyChanged(e.target.checked)} /> Only items
          that changed
        </label>
      </div>
      {items.length === 0 ? (
        <p className="muted small">No items changed.</p>
      ) : (
        <div className="table-wrap">
          <table className="traces items" data-testid="compare-items">
            <thead>
              <tr>
                <th>Input</th>
                <th>Expected</th>
                <th>{data.baseline.name}</th>
                <th>{data.candidate.name}</th>
                <th>Changes</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.item_id}>
                  <td>{preview(item.input)}</td>
                  <td>{preview(item.expected_output)}</td>
                  {[item.baseline, item.candidate].map((side, index) => (
                    <td key={index}>
                      {side.error ? <span className="error">{side.error}</span> : preview(side.output)}
                      <div className="score-badges">
                        {side.scores.map((s) => (
                          <span key={s.id} className="score-badge">
                            {s.name} {scoreText(s.data_type, s.value, s.label)}
                          </span>
                        ))}
                        <a className="small" href={`#/trace/${side.trace_id}`}>
                          trace
                        </a>
                      </div>
                    </td>
                  ))}
                  <td>
                    {Object.entries(item.flips).map(([name, change]) => (
                      <div key={name} className={`badge ${change === "improved" ? "ok" : "bad"}`}>
                        {change === "improved" ? "▲" : "▼"} {name}
                      </div>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
