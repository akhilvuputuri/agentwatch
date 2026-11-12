import { type KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getTrace, type InContext, links, STATUS_ERROR, type Score, type SpanRecord, type TraceDetail } from "./api";
import {
  displayName,
  errorText,
  isModelCall,
  messages,
  plainText,
  pretty,
  spanInput,
  spanOutput,
  summarise,
  type Summary,
} from "./content";
import { count, duration, scoreText, timestamp, usd } from "./format";
import { AddToDataset } from "./AddToDataset";
import { ScorePanel } from "./ScorePanel";
import { approxTokens, contentText } from "./Memory";
import { Content, Copy, Text, useNarrow } from "./Reading";
import { go } from "./nav";
import { layout, type Row } from "./tree";

/** How the spans are laid out: read top to bottom as steps, or against time as a waterfall. */
type Mode = "steps" | "timeline";
const MODE_KEY = "agentwatch.trace.view";

function storedMode(): Mode | null {
  try {
    const value = window.localStorage.getItem(MODE_KEY);
    return value === "steps" || value === "timeline" ? value : null;
  } catch {
    return null; // storage can be blocked; the default for the screen width applies
  }
}

/** History state of an entry a tap on a span added: closing the span goes back over it. */
const OPENED = { agentwatchSpanOpened: true };

function wasOpenedHere(): boolean {
  const state: unknown = window.history.state;
  return typeof state === "object" && state !== null && "agentwatchSpanOpened" in state;
}

/** Spans that wrap others and repeat their cost, so trace totals leave them out. */
const WRAPPERS = new Set(["invoke_agent", "create_agent", "invoke_workflow"]);

export function TraceView({ traceId, spanId }: { traceId: string; spanId?: string }) {
  const [detail, setDetail] = useState<TraceDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [chosenMode, setChosenMode] = useState<Mode | null>(storedMode);
  const narrow = useNarrow();
  // The span whose panel was last open, to give the focus back to its row when it closes.
  const opened = useRef<string | null>(null);
  const spans = detail?.spans ?? null;
  const setScores = useCallback(
    (update: (scores: Score[]) => Score[]) =>
      setDetail((current) => (current ? { ...current, scores: update(current.scores) } : current)),
    [],
  );

  useEffect(() => {
    const controller = new AbortController();
    setDetail(null);
    setError(null);
    getTrace(traceId, controller.signal).then(setDetail, (reason: unknown) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason));
    });
    return () => controller.abort();
  }, [traceId, attempt]);

  const view = useMemo(() => layout(spans ?? []), [spans]);
  const root = view.rows[0]?.span;
  const summary = useMemo(() => summarise(spans ?? [], root), [spans, root]);
  // The span in the address, else the one that failed first, else the root.
  const named = view.rows.find((row) => row.span.span_id === spanId);
  const chosen = named ?? view.rows.find((row) => row.span.span_id === summary.firstError?.span_id) ?? view.rows[0];
  // On a narrow screen a span opens over the page, and only when the address names one of
  // this trace's spans.
  const sheet = narrow && named !== undefined;
  const sheetSpan = sheet ? named.span.span_id : null;

  useEffect(() => {
    if (sheetSpan === null) {
      // Closed: back to the row it was opened from, where a keyboard or reader user was.
      const last = opened.current;
      opened.current = null;
      if (last) document.querySelector<HTMLElement>(`[data-span="${last}"]`)?.focus();
      return;
    }
    opened.current = sheetSpan;
    const before = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = before;
    };
  }, [sheetSpan]);

  // Escape closes the panel wherever the focus is, including on the page after a tap on text.
  useEffect(() => {
    if (sheetSpan === null) return;
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (wasOpenedHere()) window.history.back();
      else go(links.trace(traceId), "replace", null);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [sheetSpan, traceId]);

  if (error) return <LoadError reason={error} onRetry={() => setAttempt((n) => n + 1)} />;
  if (!detail || !spans) return <p className="muted">Loading…</p>;

  const mode: Mode = chosenMode ?? (narrow ? "steps" : "timeline");
  const setMode = (next: Mode) => {
    setChosenMode(next);
    try {
      window.localStorage.setItem(MODE_KEY, next);
    } catch {
      // not remembered; nothing else depends on it
    }
  };
  const select = (id: string) => {
    // On a narrow screen a new history entry, so the browser's Back closes the span and returns
    // here. On a wide one replaced, not added: Back should leave the trace, not walk through
    // every span looked at.
    if (narrow) go(links.trace(traceId, id), "push", OPENED);
    else go(links.trace(traceId, id), "replace");
  };
  // Previous and next keep the entry (and whether a tap added it), so Back still closes.
  const step = (id: string) => go(links.trace(traceId, id), "replace");
  const close = () => {
    // Over the entry a tap added, also after a reload; a span reached by a link has no such
    // entry, so its address is replaced with the trace's.
    if (wasOpenedHere()) window.history.back();
    else go(links.trace(traceId), "replace", null);
  };
  const arrows = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    // Only from a span's own row, and only the bare key: text selection, a "Show all" button
    // or a folded section inside the list keep their keys.
    if (event.shiftKey || event.altKey || event.ctrlKey || event.metaKey) return;
    if (!(event.target instanceof HTMLElement) || !event.target.hasAttribute("data-span")) return;
    const at = view.rows.findIndex((row) => row.span.span_id === chosen?.span.span_id);
    const next = view.rows[at + (event.key === "ArrowDown" ? 1 : -1)];
    if (!next) return;
    event.preventDefault();
    select(next.span.span_id);
    event.currentTarget.querySelector<HTMLElement>(`[data-span="${next.span.span_id}"]`)?.focus();
  };

  const sessionId = detail.session_id;
  const userId = detail.user_id;
  const models = spans.filter(isModelCall);
  const inputTokens = models.reduce((total, span) => total + (span.genai.input_tokens ?? 0), 0);
  const priced = spans.filter((span) => span.genai.cost_usd !== null && !WRAPPERS.has(span.genai.operation ?? ""));
  const cost = priced.reduce((total, span) => total + (span.genai.cost_usd ?? 0), 0);
  const chosenAt = view.rows.findIndex((row) => row.span.span_id === chosen?.span.span_id);
  const list = { rows: view.rows, chosenId: chosen?.span.span_id ?? null, onSelect: select };
  return (
    <section className="trace">
      <p>
        <a href="#/">← All traces</a>
      </p>
      <h1>{root?.name ?? traceId}</h1>
      <p className="muted small">
        {root && timestamp(root.start_ns)} · {duration(view.totalNs)} · {summary.modelCalls} model call
        {summary.modelCalls === 1 ? "" : "s"} · {summary.toolCalls} tool call{summary.toolCalls === 1 ? "" : "s"}
        {inputTokens > 0 && <> · {count(inputTokens)} tokens in</>}
        {priced.length > 0 && (
          <>
            {" · "}
            {priced.length < models.length ? "≥ " : ""}
            {usd(cost)}
          </>
        )}
        {sessionId && (
          <>
            {" · "}
            <a href={links.session(sessionId)}>session {sessionId}</a>
          </>
        )}
        {userId && (
          <>
            {" · "}
            <a href={links.traces({ user: userId })}>user {userId}</a>
          </>
        )}
      </p>
      <TurnSummary summary={summary} onOpen={select} />
      {detail.scores.length > 0 && (
        <div className="score-badges trace-scores" data-testid="trace-scores">
          {detail.scores.map((score) => (
            <span key={score.id} className="score-badge" title={score.comment ?? undefined}>
              {score.name} {scoreText(score.data_type, score.value, score.label)}
            </span>
          ))}
        </div>
      )}
      <div className="trace-bar">
        <div className="segmented" role="group" aria-label="Layout">
          <button type="button" aria-pressed={mode === "steps"} onClick={() => setMode("steps")}>
            Steps
          </button>
          <button type="button" aria-pressed={mode === "timeline"} onClick={() => setMode("timeline")}>
            Timeline
          </button>
        </div>
        <span className="muted small">{spans.length} spans</span>
      </div>
      <div className={narrow ? "split one" : "split"}>
        <div onKeyDown={narrow ? undefined : arrows}>
          {mode === "steps" ? <Steps {...list} /> : <Waterfall {...list} totalNs={view.totalNs} />}
        </div>
        {!narrow && chosen && (
          <SpanDetail key={chosen.span.span_id} row={chosen} inContext={detail.in_context[chosen.span.span_id] ?? []} />
        )}
      </div>
      {sheet && (
        <div
          className="sheet"
          role="dialog"
          aria-modal="true"
          aria-label={`Span ${displayName(named.span)}`}
        >
          <div className="sheet-head">
            <button type="button" onClick={close} autoFocus>
              ← Back to trace
            </button>
            <span className="sheet-steps">
              <button
                type="button"
                aria-label="Previous span"
                disabled={chosenAt <= 0}
                onClick={() => step(view.rows[chosenAt - 1]!.span.span_id)}
              >
                ‹
              </button>
              <span className="muted small">
                {chosenAt + 1} of {view.rows.length}
              </span>
              <button
                type="button"
                aria-label="Next span"
                disabled={chosenAt >= view.rows.length - 1}
                onClick={() => step(view.rows[chosenAt + 1]!.span.span_id)}
              >
                ›
              </button>
            </span>
          </div>
          <SpanDetail key={named.span.span_id} row={named} inContext={detail.in_context[named.span.span_id] ?? []} />
        </div>
      )}
      <details className="trace-tools" open={!narrow}>
        <summary>Scores and datasets</summary>
        <AddToDataset key={traceId} traceId={traceId} spanId={chosen?.span.span_id ?? null} spanName={chosen?.span.name ?? null} />
        <ScorePanel
          traceId={traceId}
          spanId={chosen?.span.span_id ?? null}
          spanName={chosen?.span.name ?? null}
          scores={detail.scores}
          onChange={setScores}
        />
      </details>
      <p className="muted small">
        trace <code>{traceId}</code> <Copy text={traceId} label="Copy ID" />
      </p>
    </section>
  );
}

/** Why a trace did not load, in words, with a way to try again. */
function LoadError({ reason, onRetry }: { reason: string; onRetry: () => void }) {
  const missing = reason.startsWith("404");
  const unreachable = /failed to fetch|networkerror|load failed/i.test(reason);
  return (
    <section className="load-error" role="alert">
      <h1>{missing ? "This trace is not here" : "Could not load the trace"}</h1>
      <p>
        {missing
          ? "It may not have arrived yet (spans are stored when a turn ends), or it is older than the retention period and was removed."
          : unreachable
            ? "agentwatch did not answer. Check the connection and try again."
            : reason}
      </p>
      <p className="load-error-actions">
        <button type="button" onClick={onRetry}>
          Try again
        </button>
        <a href="#/">← All traces</a>
      </p>
    </section>
  );
}

/** What was asked, what was replied, and the first thing that failed. */
function TurnSummary({ summary, onOpen }: { summary: Summary; onOpen: (spanId: string) => void }) {
  const failed = summary.firstError;
  if (!summary.asked && !summary.replied && !failed) return null;
  // What the span says about failing (unless it only repeats the type), then what it returned:
  // a failed tool's result usually holds the reason.
  const said = failed ? errorText(failed) : "";
  const why = failed
    ? [said === failed.genai.error_type ? "" : said, pretty(spanOutput(failed))].filter(Boolean).join("\n")
    : "";
  return (
    <section className="turn" aria-label="Turn summary" data-testid="turn-summary">
      {summary.asked && (
        <div className="turn-part">
          <h3>Asked</h3>
          <Text text={summary.asked} lines={5} />
        </div>
      )}
      {summary.replied && (
        <div className="turn-part">
          <h3>Replied{summary.replied.via ? ` (through ${summary.replied.via})` : ""}</h3>
          <Text text={summary.replied.text} lines={7} />
        </div>
      )}
      {!summary.replied && (summary.asked || failed) && (
        <div className="turn-part">
          <h3>Replied</h3>
          <p className="muted small">
            No reply was captured.{summary.stoppedAt ? ` The last model output called ${summary.stoppedAt}.` : ""}
          </p>
        </div>
      )}
      {failed && (
        <div className="turn-part turn-error" data-testid="first-error">
          <h3>
            First error{summary.errors > 1 ? ` (of ${summary.errors})` : ""}
            {summary.finished ? " · the turn went on" : ""}
          </h3>
          <p>
            <button type="button" className="link turn-open" onClick={() => onOpen(failed.span_id)}>
              {failed.genai.operation ?? "span"} {displayName(failed)}
            </button>
            {failed.genai.error_type && <span className="badge bad">{failed.genai.error_type}</span>}
          </p>
          {why && <Text text={why} lines={4} />}
        </div>
      )}
    </section>
  );
}

interface ListProps {
  rows: Row[];
  chosenId: string | null;
  onSelect: (spanId: string) => void;
}

function Waterfall({ rows, chosenId, onSelect, totalNs }: ListProps & { totalNs: number }) {
  return (
    <div className="waterfall" role="tree" aria-label="Span tree">
      {rows.map((row) => (
        <SpanRow
          key={row.span.span_id}
          row={row}
          totalNs={totalNs}
          selected={row.span.span_id === chosenId}
          onSelect={() => onSelect(row.span.span_id)}
        />
      ))}
    </div>
  );
}

function opClass(span: SpanRecord): string {
  return (span.genai.operation ?? "span").replace(/[^a-z_]/g, "");
}

/** The operation as a coloured word; narrow screens show its dot and keep the word for readers. */
function Op({ span }: { span: SpanRecord }) {
  return (
    <span className={`op op-${opClass(span)}`}>
      <span className="op-word">{span.genai.operation ?? "span"}</span>
    </span>
  );
}

function SpanRow({
  row,
  totalNs,
  selected,
  onSelect,
}: {
  row: Row;
  totalNs: number;
  selected: boolean;
  onSelect: () => void;
}) {
  const { span } = row;
  const left = totalNs > 0 ? (row.offsetNs / totalNs) * 100 : 0;
  const width = totalNs > 0 ? Math.max((row.durationNs / totalNs) * 100, 0.3) : 100;
  return (
    <button
      type="button"
      role="treeitem"
      aria-selected={selected}
      aria-level={row.depth + 1}
      data-testid="span-row"
      data-span={span.span_id}
      className={`span-row${selected ? " selected" : ""}`}
      onClick={onSelect}
    >
      <span className="label" style={{ paddingLeft: `${row.depth * 14}px` }}>
        <Op span={span} />
        <span className="name">{displayName(span)}</span>
        {span.status_code === STATUS_ERROR && <span className="badge bad">error</span>}
        {row.orphan && <span className="badge warn">parent missing</span>}
      </span>
      <span className="track">
        <span
          className={`bar bar-${opClass(span)}${span.status_code === STATUS_ERROR ? " bar-error" : ""}`}
          style={{ left: `${left}%`, width: `${Math.min(width, 100 - left)}%` }}
        />
      </span>
      <span className="dur">{duration(row.durationNs)}</span>
    </button>
  );
}

/** The spans in order as things that happened: what each model call said, what each tool got and gave. */
function Steps({ rows, chosenId, onSelect }: ListProps) {
  return (
    <div className="steps" role="list" aria-label="Steps">
      {rows.map((row) => (
        <Step key={row.span.span_id} row={row} selected={row.span.span_id === chosenId} onSelect={() => onSelect(row.span.span_id)} />
      ))}
    </div>
  );
}

function Step({ row, selected, onSelect }: { row: Row; selected: boolean; onSelect: () => void }) {
  const { span } = row;
  const g = span.genai;
  const failed = span.status_code === STATUS_ERROR;
  const facts: string[] = [duration(row.durationNs)];
  if (g.input_tokens !== null) facts.push(`${count(g.input_tokens)} in`);
  if (g.output_tokens !== null) facts.push(`${count(g.output_tokens)} out`);
  if (g.cost_usd !== null && !WRAPPERS.has(g.operation ?? "")) facts.push(usd(g.cost_usd));
  return (
    <div
      role="listitem"
      className={`step${selected ? " selected" : ""}${failed ? " failed" : ""}`}
      style={{ marginLeft: `${Math.min(row.depth, 4) * 10}px` }}
      data-testid="step"
    >
      <button type="button" className="step-head" data-span={span.span_id} aria-current={selected ? "true" : undefined} onClick={onSelect}>
        <span className="label">
          <Op span={span} />
          <span className="name">{displayName(span)}</span>
          {failed && <span className="badge bad">error</span>}
          {row.orphan && <span className="badge warn">parent missing</span>}
        </span>
        <span className="dur">{facts.join(" · ")}</span>
      </button>
      <StepBody span={span} failed={failed} />
    </div>
  );
}

function StepBody({ span, failed }: { span: SpanRecord; failed: boolean }) {
  const why = failed ? errorText(span) : "";
  if (span.genai.operation === "execute_tool") {
    const input = spanInput(span);
    const output = spanOutput(span);
    if (input === null && output === null && !why) return null;
    return (
      <div className="step-body">
        {input !== null && (
          <div className="io">
            <span className="io-label">Input</span>
            <Text text={pretty(input)} lines={4} />
          </div>
        )}
        {output !== null && (
          <div className="io">
            <span className="io-label">Output</span>
            <Text text={pretty(output)} lines={6} />
          </div>
        )}
        {why && <Text text={why} lines={4} />}
      </div>
    );
  }
  if (isModelCall(span)) {
    const said = messages(spanOutput(span));
    const parts = said?.flatMap((message) => message.parts) ?? [];
    const calls = parts.flatMap((part) => (part.type === "tool_call" ? [part.name] : []));
    const shown = parts.filter((part) => part.type === "text" || part.type === "reasoning");
    if (shown.length === 0 && calls.length === 0 && !why) return null;
    return (
      <div className="step-body">
        {shown.map((part, index) =>
          part.type === "reasoning" ? (
            <details key={index} className="thinking">
              <summary>Thinking ({count(part.text.length)} characters)</summary>
              <Text text={part.text} lines={8} />
            </details>
          ) : (
            part.type === "text" && <Text key={index} text={part.text} lines={10} />
          ),
        )}
        {calls.length > 0 && <p className="muted small calls">calls {calls.join(", ")}</p>}
        {why && <Text text={why} lines={4} />}
      </div>
    );
  }
  return why ? (
    <div className="step-body">
      <Text text={why} lines={4} />
    </div>
  ) : null;
}

function ContentSection({ title, value }: { title: string; value: unknown }) {
  return (
    <section className="content" data-testid={`span-${title.toLowerCase()}`}>
      <div className="content-head">
        <h3>{title}</h3>
        <Copy text={plainText(value)} />
      </div>
      <Content value={value} />
    </section>
  );
}

function SpanDetail({ row, inContext }: { row: Row; inContext: InContext[] }) {
  const { span } = row;
  const g = span.genai;
  const facts: [string, string][] = [];
  if (g.operation) facts.push(["Operation", g.operation]);
  if (g.provider) facts.push(["Provider", g.provider]);
  if (g.model) facts.push(["Model", g.model]);
  if (g.input_tokens !== null) facts.push(["Input tokens", count(g.input_tokens)]);
  if (g.cache_read_tokens !== null) facts.push(["Cache read", count(g.cache_read_tokens)]);
  if (g.cache_write_tokens !== null) facts.push(["Cache write", count(g.cache_write_tokens)]);
  if (g.output_tokens !== null) facts.push(["Output tokens", count(g.output_tokens)]);
  if (g.reasoning_tokens !== null) facts.push(["Reasoning", count(g.reasoning_tokens)]);
  if (g.cost_usd !== null)
    facts.push(["Cost", `${usd(g.cost_usd)}${g.cost_source === "calculated" ? " (from model price)" : ""}`]);
  if (g.finish_reasons.length) facts.push(["Finish", g.finish_reasons.join(", ")]);
  if (g.error_type) facts.push(["Error type", g.error_type]);
  facts.push(["Duration", duration(row.durationNs)]);
  facts.push(["Offset", duration(row.offsetNs)]);
  const failed = span.status_code === STATUS_ERROR;
  const said = failed ? errorText(span) : "";
  const why = said === g.error_type ? "" : said;
  const input = spanInput(span);
  const output = spanOutput(span);
  const name = displayName(span);
  return (
    <aside className="detail" data-testid="span-detail">
      <h2>
        {name} {failed && <span className="badge bad">error</span>}
      </h2>
      {span.name !== name && <p className="muted small">{span.name}</p>}
      {failed && (why || g.error_type) && (
        <section className="content span-error" data-testid="span-error">
          <div className="content-head">
            <h3>Error{g.error_type ? `: ${g.error_type}` : ""}</h3>
            {why && <Copy text={why} />}
          </div>
          {why && <Text text={why} lines={12} />}
        </section>
      )}
      <dl className="facts">
        {facts.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
      {input !== null && <ContentSection title="Input" value={input} />}
      {output !== null && <ContentSection title="Output" value={output} />}
      {inContext.length > 0 && <MemoryInContext entries={inContext} />}
      {span.events.length > 0 && (
        <>
          <h3>Events</h3>
          <ul className="events">
            {span.events.map((event, index) => (
              <li key={index}>
                <strong>{event.name}</strong> <span className="muted">{timestamp(event.time_ns)}</span>
                <Attributes values={event.attributes} />
              </li>
            ))}
          </ul>
        </>
      )}
      <details className="raw" open={input === null && output === null}>
        <summary>Attributes ({Object.keys(span.attributes).length})</summary>
        <Attributes values={span.attributes} />
      </details>
      <details className="raw">
        <summary>Resource ({Object.keys(span.resource).length})</summary>
        <Attributes values={span.resource} />
      </details>
      <p className="muted small">
        span <code>{span.span_id}</code>
        {span.parent_span_id && (
          <>
            {" "}
            · parent <code>{span.parent_span_id}</code>
          </>
        )}
      </p>
    </aside>
  );
}

/** Values longer than this are cut to a few lines in the raw table until asked for. */
const LONG_VALUE = 300;

function Attributes({ values }: { values: Record<string, unknown> }) {
  const entries = Object.entries(values).sort(([a], [b]) => a.localeCompare(b));
  if (entries.length === 0) return <p className="muted small">None</p>;
  return (
    <table className="attrs">
      <tbody>
        {entries.map(([key, value]) => (
          <tr key={key}>
            <th>{key}</th>
            <td>
              <Value text={typeof value === "string" ? value : JSON.stringify(value)} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Value({ text }: { text: string }) {
  return text.length > LONG_VALUE ? <Text text={text} lines={4} /> : <>{text}</>;
}

/** The memory records this model call had in its prompt, as the instrumentation reported them. */
function MemoryInContext({ entries }: { entries: InContext[] }) {
  const sized = entries.filter((entry) => entry.characters !== null);
  const characters = sized.reduce((total, entry) => total + (entry.characters ?? 0), 0);
  return (
    <>
      <h3>Memory in context</h3>
      <p className="muted small" data-testid="memory-in-context">
        {entries.length} item{entries.length === 1 ? "" : "s"} read into this call's prompt
        {sized.length > 0 && (
          <>
            {" "}
            · {count(characters)} characters{sized.length < entries.length ? " (of those with a size)" : ""}, about{" "}
            {count(approxTokens(characters))} tokens
          </>
        )}
        . Being in context does not mean the model used an item.
      </p>
      <table className="attrs">
        <tbody>
          {entries.map((entry, index) => {
            const text = contentText(entry.content);
            return (
              <tr key={`${entry.search_span_id}:${index}`}>
                <th>
                  {entry.record_id ? (
                    <a href={links.memoryItem(entry.store_id, entry.record_id)}>{entry.record_id}</a>
                  ) : (
                    <span className="muted">no ID</span>
                  )}
                  <div className="muted small">{entry.search_span_name}</div>
                </th>
                <td>
                  {text ?? <span className="muted">content not captured</span>}
                  {entry.characters !== null && (
                    <div className="muted small">
                      {count(entry.characters)} characters, about {count(approxTokens(entry.characters))} tokens
                    </div>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </>
  );
}
