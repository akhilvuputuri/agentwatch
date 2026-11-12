/** Reading captured content off spans: messages, tool input and output, and a turn's summary. */
import { STATUS_ERROR, type SpanRecord } from "./api";

/** One piece of a message, reduced to what the views show. */
export type Part =
  | { type: "text"; text: string }
  | { type: "reasoning"; text: string }
  | { type: "tool_call"; id: string | null; name: string; arguments: unknown }
  | { type: "tool_result"; id: string | null; result: unknown }
  | { type: "other"; kind: string };

export interface Message {
  role: string;
  parts: Part[];
}

const INPUT_ATTRIBUTES = ["gen_ai.input.messages", "input.value"];
const OUTPUT_ATTRIBUTES = ["gen_ai.output.messages", "output.value"];
const TOOL_INPUT = "gen_ai.tool.call.arguments";
const TOOL_OUTPUT = "gen_ai.tool.call.result";

/** JSON text holding an object or a list is parsed; anything else is returned as it was. */
export function parsed(value: unknown): unknown {
  if (typeof value !== "string") return value;
  const text = value.trim();
  if (!text.startsWith("{") && !text.startsWith("[")) return value;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return value;
  }
}

/** Text for reading: JSON indented, plain text as it is. */
export function pretty(value: unknown): string {
  const data = parsed(value);
  if (data === null || data === undefined) return "";
  return typeof data === "string" ? data : JSON.stringify(data, null, 2);
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function part(raw: unknown): Part | null {
  if (typeof raw === "string") return { type: "text", text: raw };
  const p = record(raw);
  if (!p) return null;
  const kind = typeof p.type === "string" ? p.type : "";
  const text = typeof p.content === "string" ? p.content : typeof p.text === "string" ? p.text : null;
  if (kind === "text" || (kind === "" && text !== null)) return text === null ? null : { type: "text", text };
  if (kind === "reasoning" || kind === "thinking") return text === null ? null : { type: "reasoning", text };
  const id = typeof p.id === "string" ? p.id : null;
  if (kind === "tool_call") {
    return { type: "tool_call", id, name: typeof p.name === "string" ? p.name : "tool", arguments: parsed(p.arguments) };
  }
  if (kind === "tool_call_response") {
    return { type: "tool_result", id, result: parsed(p.response ?? p.result ?? p.content) };
  }
  return { type: "other", kind: kind || "part" };
}

function message(raw: unknown): Message | null {
  const m = record(raw);
  if (!m) return null;
  const role = typeof m.role === "string" ? m.role : "";
  const parts: Part[] = [];
  // The GenAI conventions' shape: typed parts.
  if (Array.isArray(m.parts)) {
    for (const each of m.parts) {
      const found = part(each);
      if (found) parts.push(found);
    }
  } else if (typeof m.content === "string") {
    // The chat-completions shape, which some instrumentations record unchanged.
    parts.push({ type: "text", text: m.content });
  } else if (Array.isArray(m.content)) {
    for (const each of m.content) {
      const found = part(each);
      if (found) parts.push(found);
    }
  } else if (!Array.isArray(m.tool_calls)) {
    return null;
  }
  if (Array.isArray(m.tool_calls)) {
    for (const each of m.tool_calls) {
      const call = record(each);
      const fn = record(call?.function);
      if (!call || !fn) continue;
      parts.push({
        type: "tool_call",
        id: typeof call.id === "string" ? call.id : null,
        name: typeof fn.name === "string" ? fn.name : "tool",
        arguments: parsed(fn.arguments),
      });
    }
  }
  return { role, parts };
}

/** A captured value as messages, or null when it is not a list of messages. */
export function messages(value: unknown): Message[] | null {
  const data = parsed(value);
  if (!Array.isArray(data) || data.length === 0) return null;
  const found: Message[] = [];
  for (const each of data) {
    const one = message(each);
    if (!one) return null;
    found.push(one);
  }
  return found;
}

function attribute(span: SpanRecord, names: string[]): unknown {
  for (const name of names) {
    const value = span.attributes[name];
    if (value !== undefined && value !== null && value !== "") return value;
  }
  return null;
}

/** What went into a span: a tool's arguments, else the captured input. Null when not captured. */
export function spanInput(span: SpanRecord): unknown {
  return attribute(span, [TOOL_INPUT, ...INPUT_ATTRIBUTES]);
}

/** What came out of a span: a tool's result, else the captured output. Null when not captured. */
export function spanOutput(span: SpanRecord): unknown {
  return attribute(span, [TOOL_OUTPUT, ...OUTPUT_ATTRIBUTES]);
}

/** Attribute names the Input and Output sections already show, so the raw table can say so. */
export const CONTENT_ATTRIBUTES = new Set([TOOL_INPUT, TOOL_OUTPUT, ...INPUT_ATTRIBUTES, ...OUTPUT_ATTRIBUTES]);

function texts(found: Message[], type: "text" | "reasoning" = "text"): string {
  return found
    .flatMap((m) => m.parts)
    .flatMap((p) => (p.type === type ? [p.text] : []))
    .join("\n\n")
    .trim();
}

/** What the user last said in a captured input; for anything else, its text. Null when empty. */
function asked(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  const found = messages(value);
  if (!found) return pretty(value) || null;
  const users = found.filter((m) => m.role === "user" && texts([m]));
  const last = users[users.length - 1];
  return (last ? texts([last]) : texts(found)) || null;
}

/** Argument names agents use for the text of a reply they send through a tool. */
const SPOKEN = ["reply", "message", "text", "answer", "response", "content", "output"];

export interface Reply {
  text: string;
  /** The tool the reply was sent through, when it was not plain text. */
  via: string | null;
}

/**
 * The reply in a captured output: its text, or, when the model answered only by calling a tool
 * (agents often end a turn that way), that call's text argument.
 */
export function reply(value: unknown): Reply | null {
  if (value === null || value === undefined) return null;
  const found = messages(value);
  if (!found) {
    const text = pretty(value);
    return text ? { text, via: null } : null;
  }
  const text = texts(found);
  if (text) return { text, via: null };
  const calls = found.flatMap((m) => m.parts).flatMap((p) => (p.type === "tool_call" ? [p] : []));
  const last = calls[calls.length - 1];
  if (!last) return null;
  const args = record(last.arguments);
  if (args) {
    const strings = Object.entries(args).filter((entry): entry is [string, string] => typeof entry[1] === "string");
    const named = SPOKEN.map((name) => strings.find(([key]) => key === name)).find((entry) => entry !== undefined);
    const spoken = named ?? (strings.length === 1 ? strings[0] : undefined);
    if (spoken && spoken[1].trim()) return { text: spoken[1].trim(), via: last.name };
  }
  return { text: pretty(last.arguments), via: last.name };
}

export interface Summary {
  /** What was asked: the root's captured input, else the earliest captured input. */
  asked: string | null;
  replied: Reply | null;
  /** The earliest failed span that no failed child explains. */
  firstError: SpanRecord | null;
  errors: number;
  modelCalls: number;
  toolCalls: number;
}

const MODEL_OPERATIONS = new Set(["chat", "completion", "text_completion", "generate_content"]);

export function isModelCall(span: SpanRecord): boolean {
  const g = span.genai;
  return g.operation ? MODEL_OPERATIONS.has(g.operation) : g.model !== null && g.input_tokens !== null;
}

function byStart(a: SpanRecord, b: SpanRecord): number {
  const diff = BigInt(a.start_ns) - BigInt(b.start_ns);
  return diff < 0n ? -1 : diff > 0n ? 1 : a.span_id.localeCompare(b.span_id);
}

/** The failed spans that started a failure: no failed child, earliest first. */
export function errorOrigins(spans: SpanRecord[]): SpanRecord[] {
  const failed = spans.filter((span) => span.status_code === STATUS_ERROR);
  const explained = new Set(failed.map((span) => span.parent_span_id));
  return failed.filter((span) => !explained.has(span.span_id)).sort(byStart);
}

/** What a turn was, read from its spans. `root` is the span the trace list names the trace by. */
export function summarise(spans: SpanRecord[], root: SpanRecord | undefined): Summary {
  const ordered = [...spans].sort(byStart);
  const first = <T,>(list: SpanRecord[], read: (span: SpanRecord) => T | null): T | null => {
    for (const span of list) {
      const found = read(span);
      if (found !== null) return found;
    }
    return null;
  };
  const input = (span: SpanRecord) => asked(attribute(span, INPUT_ATTRIBUTES));
  const output = (span: SpanRecord) => reply(attribute(span, OUTPUT_ATTRIBUTES));
  const origins = errorOrigins(spans);
  return {
    asked: (root && input(root)) ?? first(ordered, input),
    replied: (root && output(root)) ?? first([...ordered].reverse(), output),
    firstError: origins[0] ?? null,
    errors: origins.length,
    modelCalls: spans.filter(isModelCall).length,
    toolCalls: spans.filter((span) => span.genai.operation === "execute_tool").length,
  };
}

/** The name a span is best known by: its tool, agent or model, else its own name. */
export function displayName(span: SpanRecord): string {
  const g = span.genai;
  return g.tool_name ?? g.agent_name ?? g.model ?? span.name;
}

/** A captured value as text to copy: messages as "role:" blocks, anything else as `pretty`. */
export function plainText(value: unknown): string {
  const found = messages(value);
  if (!found) return pretty(value);
  return found
    .map((m) => {
      const body = m.parts.map((p) => {
        if (p.type === "text" || p.type === "reasoning") return p.text;
        if (p.type === "tool_call") return `[calls ${p.name}] ${pretty(p.arguments)}`;
        if (p.type === "tool_result") return `[tool result] ${pretty(p.result)}`;
        return `[${p.kind}]`;
      });
      return `${m.role || "message"}:\n${body.join("\n")}`;
    })
    .join("\n\n");
}

/** Why a failed span failed, as far as it says: status message, error type, exception event. */
export function errorText(span: SpanRecord): string {
  const lines: string[] = [];
  if (span.status_message) lines.push(span.status_message);
  for (const event of span.events) {
    if (event.name !== "exception") continue;
    const type = event.attributes["exception.type"];
    const text = event.attributes["exception.message"];
    const line = [type, text].filter((each) => typeof each === "string" && each).join(": ");
    if (line && !lines.includes(line)) lines.push(line);
    const stack = event.attributes["exception.stacktrace"];
    if (typeof stack === "string" && stack) lines.push(stack);
  }
  return lines.join("\n");
}
