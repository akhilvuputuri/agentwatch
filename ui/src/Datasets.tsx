import { useEffect, useState, type FormEvent } from "react";
import {
  addItem,
  createDataset,
  deleteDataset,
  getDataset,
  links,
  listDatasets,
  listItems,
  setItemStatus,
  type Dataset,
  type DatasetItem,
} from "./api";
import { timestamp } from "./format";

/** A one-line preview of a JSON value; message lists show their text parts. */
export function preview(value: unknown, limit = 140): string {
  if (value === null || value === undefined) return "–";
  let text: string;
  if (Array.isArray(value) && value.every((m) => m && typeof m === "object" && "parts" in m)) {
    text = (value as { role?: string; parts: { content?: unknown }[] }[])
      .map((m) => `${m.role ?? ""}: ${m.parts.map((p) => (typeof p.content === "string" ? p.content : "")).join(" ")}`)
      .join(" / ");
  } else {
    text = typeof value === "string" ? value : JSON.stringify(value);
  }
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

export function DatasetList() {
  const [datasets, setDatasets] = useState<Dataset[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    listDatasets(controller.signal).then(setDatasets, (reason: unknown) => {
      if (!controller.signal.aborted) setError(String(reason));
    });
    return () => controller.abort();
  }, []);

  const create = (event: FormEvent) => {
    event.preventDefault();
    createDataset(name.trim(), description.trim()).then(
      (dataset) => {
        setDatasets((current) => [dataset, ...(current ?? [])]);
        setName("");
        setDescription("");
        setError(null);
      },
      (reason: unknown) => setError(String(reason)),
    );
  };

  return (
    <section>
      <h1>Datasets</h1>
      <p className="muted small">
        Collections of inputs and expected outputs to run experiments on. Add items here, through the API, or from
        any trace with "Add to dataset".
      </p>
      {error && <p className="error">{error}</p>}
      <form className="score-form" onSubmit={create} aria-label="New dataset">
        <label>
          Name <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={200} />
        </label>
        <label className="wide">
          Description <input value={description} onChange={(e) => setDescription(e.target.value)} maxLength={2000} />
        </label>
        <button type="submit" disabled={!name.trim()}>
          Create dataset
        </button>
      </form>
      {datasets && datasets.length === 0 && <p className="empty">No datasets yet.</p>}
      {datasets && datasets.length > 0 && (
        <div className="table-wrap">
          <table className="traces">
            <thead>
              <tr>
                <th>Dataset</th>
                <th className="num">Items</th>
                <th className="num">Archived</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {datasets.map((d) => (
                <tr key={d.id} data-testid="dataset-row">
                  <td>
                    <a href={links.dataset(d.id)}>{d.name}</a>
                    {d.description && <div className="muted small">{d.description}</div>}
                  </td>
                  <td className="num">{d.items}</td>
                  <td className="num">{d.archived_items}</td>
                  <td className="muted">{timestamp(d.created_ns)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export function DatasetView({ datasetId }: { datasetId: string }) {
  const [dataset, setDataset] = useState<Dataset | null>(null);
  const [items, setItems] = useState<DatasetItem[] | null>(null);
  const [archived, setArchived] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getDataset(datasetId, controller.signal).then(setDataset, (reason: unknown) => {
      if (!controller.signal.aborted) setError(String(reason));
    });
    return () => controller.abort();
  }, [datasetId]);

  useEffect(() => {
    const controller = new AbortController();
    listItems(datasetId, archived, controller.signal).then(setItems, (reason: unknown) => {
      if (!controller.signal.aborted) setError(String(reason));
    });
    return () => controller.abort();
  }, [datasetId, archived]);

  const toggle = (item: DatasetItem) => {
    const status: DatasetItem["status"] = item.status === "ACTIVE" ? "ARCHIVED" : "ACTIVE";
    setItemStatus(item.id, status).then(
      () =>
        setItems((current) =>
          (current ?? [])
            .map((i) => (i.id === item.id ? { ...i, status } : i))
            .filter((i) => archived || i.status === "ACTIVE"),
        ),
      (reason: unknown) => setError(String(reason)),
    );
  };

  const remove = () => {
    if (!dataset || !window.confirm(`Delete the dataset "${dataset.name}" and its items?`)) return;
    deleteDataset(dataset.id).then(
      () => {
        window.location.hash = "#/datasets";
      },
      (reason: unknown) => setError(String(reason)),
    );
  };

  if (error) return <p className="error">{error}</p>;
  if (!dataset || !items) return <p className="muted">Loading…</p>;
  return (
    <section>
      <p>
        <a href="#/datasets">← All datasets</a>
      </p>
      <h1>{dataset.name}</h1>
      {dataset.description && <p className="muted">{dataset.description}</p>}
      <div className="toolbar">
        <label>
          <input type="checkbox" checked={archived} onChange={(e) => setArchived(e.target.checked)} /> Show archived
        </label>
        <button type="button" onClick={remove}>
          Delete dataset
        </button>
      </div>
      <ItemForm
        datasetId={dataset.id}
        onAdded={(item) => setItems((current) => [...(current ?? []), item])}
        onError={setError}
      />
      {items.length === 0 ? (
        <p className="empty">No items yet.</p>
      ) : (
        <div className="table-wrap">
          <table className="traces items" data-testid="item-table">
            <thead>
              <tr>
                <th>Input</th>
                <th>Expected output</th>
                <th>Source</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id} className={item.status === "ARCHIVED" ? "archived" : undefined}>
                  <td title={JSON.stringify(item.input)}>{preview(item.input)}</td>
                  <td title={JSON.stringify(item.expected_output)}>{preview(item.expected_output)}</td>
                  <td className="small">
                    {item.source_trace_id ? <a href={`#/trace/${item.source_trace_id}`}>trace</a> : <span className="muted">added</span>}
                  </td>
                  <td>
                    <button type="button" className="link" onClick={() => toggle(item)}>
                      {item.status === "ACTIVE" ? "Archive" : "Restore"}
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

/** Values are JSON, or plain text when they are not valid JSON. */
function parse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function ItemForm({
  datasetId,
  onAdded,
  onError,
}: {
  datasetId: string;
  onAdded: (item: DatasetItem) => void;
  onError: (message: string) => void;
}) {
  const [input, setInput] = useState("");
  const [expected, setExpected] = useState("");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    addItem(datasetId, { input: parse(input), ...(expected.trim() ? { expected_output: parse(expected) } : {}) }).then(
      (item) => {
        setInput("");
        setExpected("");
        onAdded(item);
      },
      (reason: unknown) => onError(String(reason)),
    );
  };
  return (
    <form className="score-form" onSubmit={submit} aria-label="Add item">
      <label className="wide">
        Input (JSON or text) <textarea value={input} onChange={(e) => setInput(e.target.value)} rows={2} required />
      </label>
      <label className="wide">
        Expected output <textarea value={expected} onChange={(e) => setExpected(e.target.value)} rows={2} />
      </label>
      <button type="submit" disabled={!input.trim()}>
        Add item
      </button>
    </form>
  );
}
