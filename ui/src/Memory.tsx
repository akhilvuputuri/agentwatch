import { useCallback, useEffect, useRef, useState } from "react";
import {
  deleteMemoryItem,
  getMemoryItem,
  links,
  listMemory,
  type MemoryEvent,
  type MemoryItem,
  type MemoryItemDetail,
} from "./api";
import { timestamp } from "./format";

const PAGE = 50;

/** A record's content as one line of text: strings as they are, anything else as JSON. */
export function contentText(content: unknown): string | null {
  if (content === null || content === undefined) return null;
  return typeof content === "string" ? content : JSON.stringify(content);
}

/** About how many tokens a number of characters is: an estimate, at four characters a token. */
export function approxTokens(characters: number): number {
  return characters > 0 ? Math.max(1, Math.round(characters / 4)) : 0;
}

const OPERATIONS: Record<string, string> = {
  create_memory: "created",
  update_memory: "updated",
  upsert_memory: "saved",
  delete_memory: "deleted",
  search_memory: "read",
};

export function operationText(operation: string): string {
  return OPERATIONS[operation] ?? operation;
}

function kind(item: { metadata: Record<string, unknown> | null }): string | null {
  const value = item.metadata?.["kind"];
  return typeof value === "string" ? value : null;
}

export function MemoryList() {
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [stores, setStores] = useState<string[]>([]);
  const [store, setStore] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [next, setNext] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const older = useRef<AbortController | null>(null);

  useEffect(() => {
    const timer = setTimeout(() => setSearch(query.trim()), 250);
    return () => clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    const filter = {
      limit: PAGE,
      ...(store !== null ? { store } : {}),
      ...(search ? { q: search } : {}),
    };
    listMemory(filter, controller.signal).then(
      (page) => {
        setItems(page.items);
        setStores(page.stores);
        setNext(page.next);
        setLoading(false);
      },
      (reason: unknown) => {
        if (!controller.signal.aborted) {
          setError(String(reason));
          setLoading(false);
        }
      },
    );
    return () => {
      controller.abort();
      older.current?.abort();
    };
  }, [store, search]);

  const loadMore = useCallback(() => {
    if (!next) return;
    older.current?.abort();
    const controller = new AbortController();
    older.current = controller;
    const filter = {
      limit: PAGE,
      before: next,
      ...(store !== null ? { store } : {}),
      ...(search ? { q: search } : {}),
    };
    listMemory(filter, controller.signal).then(
      (page) => {
        setItems((current) => {
          const known = new Set(current.map((i) => `${i.store_id}\u0000${i.record_id}`));
          return [...current, ...page.items.filter((i) => !known.has(`${i.store_id}\u0000${i.record_id}`))];
        });
        setNext(page.next);
      },
      (reason: unknown) => {
        if (!controller.signal.aborted) setError(String(reason));
      },
    );
  }, [next, store, search]);

  if (error) return <p className="error">Could not load memory: {error}</p>;
  if (!loading && items.length === 0 && store === null && !search)
    return (
      <p className="empty">
        No memory yet. Items appear when a span sets <code>gen_ai.operation.name</code> to a memory operation such as{" "}
        <code>create_memory</code> or <code>search_memory</code>.
      </p>
    );
  return (
    <section>
      <h1>Memory</h1>
      <p className="muted small">
        What the agent remembers, from its memory spans. "Reads" counts its <code>search_memory</code> reads. A model
        call's span lists the items in its prompt; whether a model used one is not recorded.
      </p>
      <div className="toolbar">
        <label>
          Store{" "}
          <select
            value={store ?? "\u0000all"}
            onChange={(event) => setStore(event.target.value === "\u0000all" ? null : event.target.value)}
          >
            <option value={"\u0000all"}>All stores</option>
            {stores.map((name) => (
              <option key={name} value={name}>
                {name || "(unnamed)"}
              </option>
            ))}
          </select>
        </label>
        <label>
          Search{" "}
          <input
            type="search"
            value={query}
            placeholder="ID or content"
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
      </div>
      {!loading && items.length === 0 ? (
        <p className="muted">No items match.</p>
      ) : (
        <div className="table-wrap">
          <table className="traces">
            <thead>
              <tr>
                <th>Last activity</th>
                <th>Item</th>
                <th>Content</th>
                <th className="num">Changes</th>
                <th className="num">Reads</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => {
                const text = contentText(item.content);
                const itemKind = kind(item);
                return (
                  <tr key={`${item.store_id}\u0000${item.record_id}`} data-testid="memory-row">
                    <td className="muted">{timestamp(item.last_ns)}</td>
                    <td>
                      <a href={links.memoryItem(item.store_id, item.record_id)}>{item.record_id}</a>
                      <div className="muted small">
                        {item.store_id || "(unnamed store)"}
                        {itemKind && <> · {itemKind}</>}
                      </div>
                    </td>
                    <td className="memory-content">{text ?? <span className="muted">not captured</span>}</td>
                    <td className="num">{item.writes}</td>
                    <td className="num">{item.reads}</td>
                    <td>
                      {item.deleted ? (
                        <span className="badge warn">deleted</span>
                      ) : (
                        <span className="badge ok">current</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {next && (
        <button type="button" className="more" onClick={loadMore}>
          Load older items
        </button>
      )}
    </section>
  );
}

export function MemoryItemView({ store, id }: { store: string; id: string }) {
  const [data, setData] = useState<MemoryItemDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getMemoryItem(store, id, controller.signal).then(setData, (reason: unknown) => {
      if (!controller.signal.aborted) setError(String(reason));
    });
    return () => controller.abort();
  }, [store, id]);

  if (error) return <p className="error">Could not load memory item: {error}</p>;
  if (!data) return <p className="muted">Loading…</p>;
  const { item, changes, reads } = data;
  const text = contentText(item.content);
  const itemKind = kind(item);
  return (
    <section>
      <p>
        <a href="#/memory">← All memory</a>
      </p>
      <h1>{item.record_id}</h1>
      <p className="muted small">
        {item.store_id || "(unnamed store)"}
        {itemKind && <> · {itemKind}</>} · {item.writes} change
        {item.writes === 1 ? "" : "s"} · read {item.reads} time
        {item.reads === 1 ? "" : "s"} · first seen {timestamp(item.first_ns)}
        {item.deleted && (
          <>
            {" · "}
            <span className="badge warn">deleted</span>
          </>
        )}
      </p>
      <p className="memory-current">{text ?? <span className="muted">Content not captured.</span>}</p>
      <h2>Changes</h2>
      {changes.length === 0 ? (
        <p className="muted small">
          No change recorded: it was only read. It may predate the traces agentwatch received.
        </p>
      ) : (
        <ol className="timeline" data-testid="memory-changes">
          {changes.map((event, index) => (
            <MemoryEventRow key={`${event.trace_id}:${event.span_id}:${index}`} event={event} />
          ))}
        </ol>
      )}
      {data.changes_truncated && <p className="muted small">Showing the most recent {changes.length} changes.</p>}
      <h2>Recent reads</h2>
      <p className="muted small">
        Each opens the trace at the read; the model calls after it in the same agent list what was in their prompt.
      </p>
      {reads.length === 0 ? (
        <p className="muted small">Not read yet.</p>
      ) : (
        <ol className="timeline" data-testid="memory-reads">
          {reads.map((event, index) => (
            <MemoryEventRow key={`${event.trace_id}:${event.span_id}:${index}`} event={event} />
          ))}
        </ol>
      )}
      {data.reads_truncated && (
        <p className="muted small">
          The most recent {reads.length} of {item.reads}.
        </p>
      )}
      <h2>Delete</h2>
      <p className="muted small">
        agentwatch keeps memory history when traces are pruned. Deleting removes this item's history and its records in
        the stored memory spans, and it stays deleted if those spans arrive again. Its text can still be in captured
        prompts and tool results until those traces are pruned. The agent's own memory is not changed.
      </p>
      <DeleteItem store={store} id={id} />
    </section>
  );
}

function DeleteItem({ store, id }: { store: string; id: string }) {
  const [error, setError] = useState<string | null>(null);
  const remove = () => {
    if (!window.confirm(`Delete the history agentwatch recorded for ${id}?`)) return;
    deleteMemoryItem(store, id).then(
      () => {
        window.location.hash = "#/memory";
      },
      (reason: unknown) => setError(String(reason)),
    );
  };
  return (
    <>
      <button type="button" onClick={remove}>
        Delete this item's history
      </button>
      {error && <p className="error">Could not delete: {error}</p>}
    </>
  );
}

function MemoryEventRow({ event }: { event: MemoryEvent }) {
  const text = contentText(event.content);
  const read = event.operation === "search_memory";
  return (
    <li>
      <span className="muted small">{timestamp(event.time_ns)}</span>
      <span className={`badge ${read ? "neutral" : event.operation === "delete_memory" ? "warn" : "ok"}`}>
        {operationText(event.operation)}
      </span>
      {event.trace_stored ? (
        <a href={links.trace(event.trace_id, event.span_id)}>{event.span_name ?? "span"}</a>
      ) : (
        <span className="muted small">trace no longer kept</span>
      )}
      <span className="muted small">{event.service_name}</span>
      {!read && text !== null && <div className="memory-content">{text}</div>}
      {event.links.length > 0 && (
        <div className="muted small">
          from{" "}
          {event.links.map((link, index) => (
            <span key={`${link.trace_id}:${link.span_id}`}>
              {index > 0 && ", "}
              <a href={links.trace(link.trace_id, link.span_id)}>{link.trace_id.slice(0, 8)}</a>
            </span>
          ))}
        </div>
      )}
    </li>
  );
}
