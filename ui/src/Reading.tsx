/** Pieces for reading captured content: clamped text, a copy button, messages. */
import { useEffect, useState } from "react";
import { type Message, messages, type Part, pretty } from "./content";
import { count } from "./format";

/** Text kept as written (line breaks, indentation), cut to a few lines until asked for. */
export function Text({ text, lines = 8 }: { text: string; lines?: number }) {
  const [open, setOpen] = useState(false);
  const long = text.length > lines * 80 || text.split("\n").length > lines;
  return (
    <div className="text-block">
      <pre className={`text${long && !open ? " clamped" : ""}`} style={long && !open ? { maxHeight: `${lines * 1.5}em` } : undefined}>
        {text}
      </pre>
      {long && (
        <button type="button" className="link" onClick={() => setOpen((was) => !was)} aria-expanded={open}>
          {open ? "Show less" : `Show all (${count(text.length)} characters)`}
        </button>
      )}
    </div>
  );
}

export function Copy({ text, label = "Copy" }: { text: string; label?: string }) {
  const [state, setState] = useState<"idle" | "done" | "failed">("idle");
  useEffect(() => {
    if (state === "idle") return;
    const timer = window.setTimeout(() => setState("idle"), 1500);
    return () => window.clearTimeout(timer);
  }, [state]);
  const copy = () => {
    // Clipboard access needs a secure page and can be refused; say so instead of doing nothing.
    const written = navigator.clipboard?.writeText(text);
    if (!written) return setState("failed");
    written.then(
      () => setState("done"),
      () => setState("failed"),
    );
  };
  return (
    <button type="button" className="link copy" onClick={copy}>
      {state === "done" ? "Copied" : state === "failed" ? "Could not copy" : label}
    </button>
  );
}

function PartView({ part, lines }: { part: Part; lines: number }) {
  if (part.type === "text") return <Text text={part.text} lines={lines} />;
  if (part.type === "reasoning") {
    return (
      <details className="thinking">
        <summary>Thinking ({count(part.text.length)} characters)</summary>
        <Text text={part.text} lines={lines} />
      </details>
    );
  }
  if (part.type === "tool_call") {
    return (
      <div className="call">
        <div className="call-name">calls {part.name}</div>
        <Text text={pretty(part.arguments)} lines={Math.min(lines, 6)} />
      </div>
    );
  }
  if (part.type === "tool_result") {
    return (
      <div className="call">
        <div className="call-name">tool result</div>
        <Text text={pretty(part.result)} lines={Math.min(lines, 6)} />
      </div>
    );
  }
  return <p className="muted small">[{part.kind}]</p>;
}

/** How many of a long history's latest messages are shown before asking for the rest. */
const RECENT = 4;

export function MessageList({ list, lines = 8 }: { list: Message[]; lines?: number }) {
  const [all, setAll] = useState(false);
  const hidden = all ? 0 : Math.max(0, list.length - RECENT);
  return (
    <div className="messages">
      {hidden > 0 && (
        <button type="button" className="link" onClick={() => setAll(true)}>
          Show {hidden} earlier message{hidden === 1 ? "" : "s"}
        </button>
      )}
      {list.slice(hidden).map((message, index) => (
        <div key={hidden + index} className={`msg msg-${message.role.replace(/[^a-z]/g, "") || "other"}`}>
          <div className="msg-role">{message.role || "message"}</div>
          {message.parts.length === 0 && <p className="muted small">(empty)</p>}
          {message.parts.map((part, at) => (
            <PartView key={at} part={part} lines={lines} />
          ))}
        </div>
      ))}
    </div>
  );
}

/** A captured value: messages when it is a list of them, otherwise text (JSON indented). */
export function Content({ value, lines = 8 }: { value: unknown; lines?: number }) {
  const found = messages(value);
  if (found) return <MessageList list={found} lines={lines} />;
  return <Text text={pretty(value)} lines={lines} />;
}

/** True while the window is at most `width` pixels wide. */
export function useNarrow(width = 900): boolean {
  const query = `(max-width: ${width}px)`;
  const [narrow, setNarrow] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const media = window.matchMedia(query);
    const onChange = () => setNarrow(media.matches);
    onChange();
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, [query]);
  return narrow;
}
