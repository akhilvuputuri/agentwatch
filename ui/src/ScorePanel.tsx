import { useState, type FormEvent } from "react";
import { addScore, deleteScore, type NewScore, type Score, type ScoreType } from "./api";
import { scoreText, timestamp } from "./format";

interface Props {
  traceId: string;
  /** The span selected in the waterfall; a new score can be attached to it. */
  spanId: string | null;
  spanName: string | null;
  scores: Score[];
  onChange: (update: (scores: Score[]) => Score[]) => void;
}

/** The trace's scores, and a form to add one as a human annotation. */
export function ScorePanel({ traceId, spanId, spanName, scores, onChange }: Props) {
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const remove = (score: Score) => {
    deleteScore(score.id).then(
      () => onChange((current) => current.filter((s) => s.id !== score.id)),
      (reason: unknown) => setError(String(reason)),
    );
  };

  return (
    <section className="scores" aria-label="Scores">
      <div className="scores-head">
        <h2>Scores</h2>
        <button type="button" onClick={() => setAdding((open) => !open)} aria-expanded={adding}>
          {adding ? "Cancel" : "Add score"}
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      {adding && (
        <ScoreForm
          traceId={traceId}
          spanId={spanId}
          spanName={spanName}
          onAdded={(score) => {
            onChange((current) => [...current, score]);
            setAdding(false);
            setError(null);
          }}
          onError={setError}
        />
      )}
      {scores.length === 0 ? (
        <p className="muted small">No scores yet.</p>
      ) : (
        <table className="attrs" data-testid="score-table">
          <tbody>
            {scores.map((score) => (
              <tr key={score.id}>
                <th>{score.name}</th>
                <td>
                  <strong>{scoreText(score.data_type, score.value, score.label)}</strong>
                  {score.comment && <div className="muted small">{score.comment}</div>}
                </td>
                <td className="muted small">
                  {score.source.toLowerCase()}
                  {score.span_id && <> · span {score.span_id.slice(0, 8)}</>} · {timestamp(score.created_ns)}
                </td>
                <td>
                  <button type="button" className="link" onClick={() => remove(score)} aria-label={`Delete score ${score.name}`}>
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function ScoreForm({
  traceId,
  spanId,
  spanName,
  onAdded,
  onError,
}: {
  traceId: string;
  spanId: string | null;
  spanName: string | null;
  onAdded: (score: Score) => void;
  onError: (message: string) => void;
}) {
  const [name, setName] = useState("");
  const [type, setType] = useState<ScoreType>("NUMERIC");
  const [value, setValue] = useState("");
  const [comment, setComment] = useState("");
  const [onSpan, setOnSpan] = useState(false);
  const [saving, setSaving] = useState(false);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const body: NewScore = { trace_id: traceId, name: name.trim() };
    if (type === "NUMERIC") {
      const number = Number(value);
      if (value.trim() === "" || !Number.isFinite(number)) return onError("Enter a number.");
      body.value = number;
    } else if (type === "BOOLEAN") {
      body.value = value === "yes";
    } else {
      if (!value.trim()) return onError("Enter a label.");
      body.label = value.trim();
    }
    if (comment.trim()) body.comment = comment.trim();
    if (onSpan && spanId) body.span_id = spanId;
    setSaving(true);
    addScore(body).then(
      (id) => {
        setSaving(false);
        onAdded({
          id,
          trace_id: traceId,
          span_id: body.span_id ?? null,
          name: body.name,
          data_type: type,
          value: typeof body.value === "boolean" ? (body.value ? 1 : 0) : (body.value ?? null),
          label: body.label ?? null,
          source: "ANNOTATION",
          comment: body.comment ?? null,
          created_ns: String(BigInt(Date.now()) * 1_000_000n),
        });
      },
      (reason: unknown) => {
        setSaving(false);
        onError(String(reason));
      },
    );
  };

  return (
    <form className="score-form" onSubmit={submit}>
      <label>
        Name <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={200} />
      </label>
      <label>
        Type{" "}
        <select
          value={type}
          onChange={(e) => {
            setType(e.target.value as ScoreType);
            setValue(e.target.value === "BOOLEAN" ? "yes" : "");
          }}
        >
          <option value="NUMERIC">Number</option>
          <option value="BOOLEAN">Yes / no</option>
          <option value="CATEGORICAL">Label</option>
        </select>
      </label>
      <label>
        Value{" "}
        {type === "BOOLEAN" ? (
          <select value={value} onChange={(e) => setValue(e.target.value)}>
            <option value="yes">Yes</option>
            <option value="no">No</option>
          </select>
        ) : (
          <input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            inputMode={type === "NUMERIC" ? "decimal" : "text"}
            required
          />
        )}
      </label>
      <label className="wide">
        Comment <input value={comment} onChange={(e) => setComment(e.target.value)} maxLength={2000} />
      </label>
      {spanId && (
        <label>
          <input type="checkbox" checked={onSpan} onChange={(e) => setOnSpan(e.target.checked)} /> On span{" "}
          <span className="muted">{spanName}</span>
        </label>
      )}
      <button type="submit" disabled={saving || !name.trim()}>
        {saving ? "Saving…" : "Save"}
      </button>
    </form>
  );
}
