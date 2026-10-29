import { useEffect, useState, type FormEvent } from "react";
import {
  createJudge,
  deleteJudge,
  listJudgements,
  listJudges,
  updateJudge,
  type Judge,
  type Judgement,
  type NewJudge,
} from "./api";
import { count, timestamp, usd } from "./format";

const DEFAULT_PROMPT = `The user said:
{input}

The assistant answered:
{output}

Does the answer do what the user asked, correctly and completely?`;

/** LLM judges: a model scores a sample of new traces. */
export function Judges() {
  const [judges, setJudges] = useState<Judge[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const load = () =>
      listJudges(controller.signal).then(setJudges, (reason: unknown) => {
        if (!controller.signal.aborted) setError(String(reason));
      });
    void load();
    // Judges run in the background; keep their counts current.
    const timer = window.setInterval(() => void load(), 15_000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, []);

  const replace = (judge: Judge) =>
    setJudges((current) => (current ?? []).map((j) => (j.id === judge.id ? judge : j)));

  const toggle = (judge: Judge) => {
    if (
      !judge.enabled &&
      !window.confirm(
        `Switch on "${judge.name}"? It sends the captured messages of up to ${judge.daily_limit} sampled traces a day to ${judge.base_url}, and each is a paid model call if that endpoint charges.`,
      )
    )
      return;
    updateJudge(judge.id, { enabled: !judge.enabled }).then(replace, (reason: unknown) => setError(String(reason)));
  };

  const remove = (judge: Judge) => {
    if (!window.confirm(`Delete the judge "${judge.name}"? The scores it stored stay.`)) return;
    deleteJudge(judge.id).then(
      () => setJudges((current) => (current ?? []).filter((j) => j.id !== judge.id)),
      (reason: unknown) => setError(String(reason)),
    );
  };

  return (
    <section>
      <h1>Judges</h1>
      <p className="muted small">
        A judge asks a model about each sampled new trace and stores the answer as a score named after the judge. It
        reads the trace's captured input and output, so traces without captured messages are skipped. Judging sends
        those messages to the judge's endpoint: the only thing agentwatch sends off this machine. Judges see traces
        that start after they were created, stop for the day at their limit of model calls, and start switched off.
      </p>
      {error && <p className="error">{error}</p>}
      <JudgeForm
        onAdded={(judge) => {
          setJudges((current) => [...(current ?? []), judge]);
          setError(null);
        }}
        onError={setError}
      />
      {judges && judges.length === 0 && <p className="empty">No judges yet.</p>}
      {judges && judges.length > 0 && (
        <div className="table-wrap">
          <table className="traces" data-testid="judge-table">
            <thead>
              <tr>
                <th>Score name</th>
                <th>Model</th>
                <th className="num">Sample</th>
                <th className="num">Calls today</th>
                <th className="num">Scored</th>
                <th className="num">Failed</th>
                <th className="num">No content</th>
                <th className="num">Spend</th>
                <th>Status</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {judges.map((j) => (
                <JudgeRow
                  key={j.id}
                  judge={j}
                  open={open === j.id}
                  onOpen={() => setOpen(open === j.id ? null : j.id)}
                  onToggle={() => toggle(j)}
                  onRemove={() => remove(j)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
      {open && <JudgementList judgeId={open} />}
    </section>
  );
}

function JudgeRow({
  judge,
  open,
  onOpen,
  onToggle,
  onRemove,
}: {
  judge: Judge;
  open: boolean;
  onOpen: () => void;
  onToggle: () => void;
  onRemove: () => void;
}) {
  const a = judge.activity;
  const status = judge.problem ? judge.problem : judge.enabled ? "on" : "off";
  return (
    <tr aria-selected={open}>
      <td>
        <button type="button" className="link" onClick={onOpen}>
          {judge.name}
        </button>
        <div className="muted small">{judge.score_type === "BOOLEAN" ? "true / false" : "0 to 1"}</div>
      </td>
      <td>
        <code>{judge.model}</code>
        <div className="muted small">{judge.base_url}</div>
      </td>
      <td className="num">{Math.round(judge.sample_rate * 1000) / 10}%</td>
      <td className="num">
        {count(a.calls)} / {count(judge.daily_limit)}
      </td>
      <td className="num">{count(a.counts.SCORED)}</td>
      <td className="num">{count(a.counts.FAILED)}</td>
      <td className="num">{count(a.counts.NO_CONTENT)}</td>
      <td className="num">
        {a.calls === 0 ? "–" : usd(a.cost_usd)}
        {a.calls_without_cost > 0 && <div className="muted small">{count(a.calls_without_cost)} unpriced</div>}
      </td>
      <td className={judge.problem ? "error" : judge.enabled ? undefined : "muted"}>{status}</td>
      <td>
        <button type="button" className="link" onClick={onToggle}>
          {judge.enabled ? "Switch off" : "Switch on"}
        </button>{" "}
        <button type="button" className="link" onClick={onRemove} aria-label={`Delete judge ${judge.name}`}>
          Delete
        </button>
      </td>
    </tr>
  );
}

function JudgementList({ judgeId }: { judgeId: string }) {
  const [rows, setRows] = useState<Judgement[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setRows(null);
    listJudgements(judgeId, controller.signal).then(setRows, (reason: unknown) => {
      if (!controller.signal.aborted) setError(String(reason));
    });
    return () => controller.abort();
  }, [judgeId]);
  if (error) return <p className="error">{error}</p>;
  if (!rows) return <p className="muted">Loading…</p>;
  const shown = rows.filter((r) => r.outcome !== "NOT_SAMPLED");
  if (!shown.length) return <p className="empty">Nothing judged yet.</p>;
  return (
    <div className="table-wrap">
      <h2>Recent judgements</h2>
      <table className="traces" data-testid="judgement-table">
        <thead>
          <tr>
            <th>When</th>
            <th>Trace</th>
            <th>Outcome</th>
            <th className="num">Score</th>
            <th>Explanation or reason</th>
            <th className="num">Cost</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((r) => (
            <tr key={r.trace_id}>
              <td className="muted">{timestamp(r.created_ns)}</td>
              <td>
                <a href={`#/trace/${r.trace_id}`}>
                  <code>{r.trace_id.slice(0, 12)}</code>
                </a>
              </td>
              <td>{r.outcome.toLowerCase().replace("_", " ")}</td>
              <td className="num">{r.value === null ? "–" : r.value}</td>
              <td className="small">{r.detail ?? ""}</td>
              <td className="num">{r.cost_usd === null ? "–" : usd(r.cost_usd)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function JudgeForm({ onAdded, onError }: { onAdded: (judge: Judge) => void; onError: (message: string) => void }) {
  const [name, setName] = useState("");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("https://openrouter.ai/api/v1");
  const [keyEnv, setKeyEnv] = useState("OPENROUTER_API_KEY");
  const [prompt, setPrompt] = useState(DEFAULT_PROMPT);
  const [scoreType, setScoreType] = useState<NewJudge["score_type"]>("BOOLEAN");
  const [samplePercent, setSamplePercent] = useState("10");
  const [limit, setLimit] = useState("50");
  const [service, setService] = useState("");
  const [saving, setSaving] = useState(false);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const rate = Number(samplePercent) / 100;
    const daily = Number(limit);
    if (!Number.isFinite(rate) || rate <= 0 || rate > 1) return onError("Sample must be above 0% and at most 100%.");
    if (!Number.isInteger(daily) || daily < 1) return onError("The daily limit must be a whole number of at least 1.");
    setSaving(true);
    createJudge({
      name: name.trim(),
      model: model.trim(),
      base_url: baseUrl.trim(),
      api_key_env: keyEnv.trim() || null,
      prompt,
      score_type: scoreType,
      sample_rate: rate,
      daily_limit: daily,
      service: service.trim() || null,
    }).then(
      (judge) => {
        setSaving(false);
        setName("");
        onAdded(judge);
      },
      (reason: unknown) => {
        setSaving(false);
        onError(String(reason));
      },
    );
  };

  return (
    <form className="score-form" onSubmit={submit} aria-label="Add judge">
      <label>
        Score name <input value={name} onChange={(e) => setName(e.target.value)} placeholder="helpful" required maxLength={200} />
      </label>
      <label>
        Model <input value={model} onChange={(e) => setModel(e.target.value)} placeholder="provider/model" required maxLength={200} />
      </label>
      <label className="wide">
        Endpoint (OpenAI-compatible) <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} required />
      </label>
      <label>
        API key variable <input value={keyEnv} onChange={(e) => setKeyEnv(e.target.value)} placeholder="none" size={20} />
      </label>
      <label>
        Score
        <select value={scoreType} onChange={(e) => setScoreType(e.target.value as NewJudge["score_type"])}>
          <option value="BOOLEAN">true / false</option>
          <option value="NUMERIC">0 to 1</option>
        </select>
      </label>
      <label>
        Sample % <input value={samplePercent} onChange={(e) => setSamplePercent(e.target.value)} inputMode="decimal" size={4} />
      </label>
      <label>
        Calls a day <input value={limit} onChange={(e) => setLimit(e.target.value)} inputMode="numeric" size={5} />
      </label>
      <label>
        Service <input value={service} onChange={(e) => setService(e.target.value)} placeholder="all" size={12} />
      </label>
      <label className="full">
        Question ({"{input}"} and {"{output}"} are replaced by the trace's messages)
        <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={6} required />
      </label>
      <button type="submit" disabled={saving || !name.trim() || !model.trim()}>
        {saving ? "Saving…" : "Add judge (switched off)"}
      </button>
    </form>
  );
}
