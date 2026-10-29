import { useEffect, useState, type FormEvent } from "react";
import { addPrice, deletePrice, listPrices, type NewPrice, type Price } from "./api";
import { timestamp } from "./format";

/** Model prices used to cost calls whose exporter reported no cost. */
export function Prices() {
  const [prices, setPrices] = useState<Price[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    listPrices(controller.signal).then(setPrices, (reason: unknown) => {
      if (!controller.signal.aborted) setError(String(reason));
    });
    return () => controller.abort();
  }, []);

  const remove = (price: Price) => {
    if (!window.confirm(`Delete the price for "${price.match_pattern}"? Costs calculated from it are removed.`)) return;
    deletePrice(price.id).then(
      () => setPrices((current) => (current ?? []).filter((p) => p.id !== price.id)),
      (reason: unknown) => setError(String(reason)),
    );
  };

  return (
    <section>
      <h1>Model prices</h1>
      <p className="muted small">
        When a model call reports tokens but no cost, its cost is calculated from the latest price here whose pattern
        matches the whole model name (regular expression, case-insensitive) and that took effect by the call's start.
        Prices are in US dollars per million tokens. Cache prices default to the input price. Changing prices
        recalculates every calculated cost; reported costs are never changed.
      </p>
      {error && <p className="error">{error}</p>}
      <PriceForm
        onAdded={(price) => {
          setPrices((current) => [...(current ?? []).filter((p) => p.id !== price.id), price]);
          setError(null);
        }}
        onError={setError}
      />
      {prices && prices.length === 0 && <p className="empty">No prices yet.</p>}
      {prices && prices.length > 0 && (
        <div className="table-wrap">
          <table className="traces" data-testid="price-table">
            <thead>
              <tr>
                <th>Model pattern</th>
                <th className="num">Input</th>
                <th className="num">Cache read</th>
                <th className="num">Cache write</th>
                <th className="num">Output</th>
                <th>Effective from</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {prices.map((p) => (
                <tr key={p.id}>
                  <td>
                    <code>{p.match_pattern}</code>
                  </td>
                  <td className="num">${p.input_per_million}</td>
                  <td className="num">{p.cache_read_per_million === null ? "–" : `$${p.cache_read_per_million}`}</td>
                  <td className="num">{p.cache_write_per_million === null ? "–" : `$${p.cache_write_per_million}`}</td>
                  <td className="num">${p.output_per_million}</td>
                  <td className="muted">{p.start_ns ? timestamp(p.start_ns) : "always"}</td>
                  <td>
                    <button type="button" className="link" onClick={() => remove(p)} aria-label={`Delete price ${p.match_pattern}`}>
                      Delete
                    </button>
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

function PriceForm({ onAdded, onError }: { onAdded: (price: Price) => void; onError: (message: string) => void }) {
  const [pattern, setPattern] = useState("");
  const [input, setInput] = useState("");
  const [output, setOutput] = useState("");
  const [cacheRead, setCacheRead] = useState("");
  const [cacheWrite, setCacheWrite] = useState("");
  const [from, setFrom] = useState("");
  const [saving, setSaving] = useState(false);

  const number = (text: string) => (text.trim() === "" ? null : Number(text));

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const values = [input, output, cacheRead, cacheWrite].map(number);
    if (values.some((v) => v !== null && (!Number.isFinite(v) || v < 0))) return onError("Prices must be numbers of 0 or more.");
    if (values[0] === null || values[1] === null) return onError("Input and output prices are required.");
    const body: NewPrice = {
      match_pattern: pattern.trim(),
      input_per_million: values[0]!,
      output_per_million: values[1]!,
      cache_read_per_million: values[2] ?? null,
      cache_write_per_million: values[3] ?? null,
    };
    if (from) body.start_ns = String(BigInt(new Date(from).getTime()) * 1_000_000n);
    setSaving(true);
    addPrice(body).then(
      (price) => {
        setSaving(false);
        setPattern("");
        setInput("");
        setOutput("");
        setCacheRead("");
        setCacheWrite("");
        setFrom("");
        onAdded(price);
      },
      (reason: unknown) => {
        setSaving(false);
        onError(String(reason));
      },
    );
  };

  return (
    <form className="score-form" onSubmit={submit} aria-label="Add price">
      <label className="wide">
        Model pattern <input value={pattern} onChange={(e) => setPattern(e.target.value)} placeholder="anthropic/claude-.*" required maxLength={200} />
      </label>
      <label>
        Input / 1M <input value={input} onChange={(e) => setInput(e.target.value)} inputMode="decimal" required size={8} />
      </label>
      <label>
        Output / 1M <input value={output} onChange={(e) => setOutput(e.target.value)} inputMode="decimal" required size={8} />
      </label>
      <label>
        Cache read / 1M <input value={cacheRead} onChange={(e) => setCacheRead(e.target.value)} inputMode="decimal" size={8} />
      </label>
      <label>
        Cache write / 1M <input value={cacheWrite} onChange={(e) => setCacheWrite(e.target.value)} inputMode="decimal" size={8} />
      </label>
      <label>
        Effective from <input type="datetime-local" value={from} onChange={(e) => setFrom(e.target.value)} />
      </label>
      <button type="submit" disabled={saving || !pattern.trim()}>
        {saving ? "Saving…" : "Add price"}
      </button>
    </form>
  );
}
