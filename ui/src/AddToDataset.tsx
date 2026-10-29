import { useEffect, useState } from "react";
import { addItemFromTrace, links, listDatasets, type Dataset } from "./api";

/** Adds the trace (its root span) or the selected span to a dataset as an item. */
export function AddToDataset({ traceId, spanId, spanName }: { traceId: string; spanId: string | null; spanName: string | null }) {
  const [open, setOpen] = useState(false);
  const [datasets, setDatasets] = useState<Dataset[] | null>(null);
  const [chosen, setChosen] = useState("");
  const [useSpan, setUseSpan] = useState(false);
  const [message, setMessage] = useState<{ text: string; datasetId?: string } | null>(null);
  // A confirmation belongs to the span it was for.
  useEffect(() => setMessage(null), [spanId, useSpan, chosen]);

  useEffect(() => {
    if (!open || datasets) return;
    const controller = new AbortController();
    listDatasets(controller.signal).then(
      (found) => {
        setDatasets(found);
        if (found[0]) setChosen(found[0].id);
      },
      (reason: unknown) => setMessage({ text: String(reason) }),
    );
    return () => controller.abort();
  }, [open, datasets]);

  const add = () => {
    addItemFromTrace(chosen, traceId, useSpan ? spanId : null).then(
      (item) => setMessage({ text: "Added.", datasetId: item.dataset_id }),
      (reason: unknown) => setMessage({ text: String(reason) }),
    );
  };

  if (!open)
    return (
      <button type="button" onClick={() => setOpen(true)}>
        Add to dataset
      </button>
    );
  return (
    <div className="add-to-dataset" role="group" aria-label="Add to dataset">
      {datasets && datasets.length === 0 ? (
        <span className="muted small">
          No datasets yet. <a href="#/datasets">Create one</a>.
        </span>
      ) : (
        <>
          <select value={chosen} onChange={(e) => setChosen(e.target.value)} aria-label="Dataset">
            {(datasets ?? []).map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </select>
          {spanId && (
            <label className="small">
              <input type="checkbox" checked={useSpan} onChange={(e) => setUseSpan(e.target.checked)} /> Selected span
              only <span className="muted">({spanName})</span>
            </label>
          )}
          <button type="button" onClick={add} disabled={!chosen}>
            Add
          </button>
        </>
      )}
      <button type="button" className="link" onClick={() => setOpen(false)}>
        Close
      </button>
      {message && (
        <span className="small" role="status">
          {message.text} {message.datasetId && <a href={links.dataset(message.datasetId)}>Open dataset</a>}
        </span>
      )}
    </div>
  );
}
