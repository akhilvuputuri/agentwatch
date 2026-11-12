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

/** JavaScript holds integers exactly only up to 2^53, about 16 digits. */
const LONG_NUMBER = /\d{16,}/;

/**
 * Text for reading: JSON indented, plain text as it is. JSON text with a number too long to
 * survive parsing is left as it was written, since indenting it would change the number shown
 * and copied.
 */
export function pretty(value: unknown): string {
  if (typeof value === "string" && LONG_NUMBER.test(value)) return value;
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

/**
 * What the user last said in a captured input. Anything that is not a list of messages is taken
 * as the request itself only with `plain` (the root span's own input): on another span it may be
 * a search query or a tool's argument.
 */
function asked(value: unknown, plain: boolean): string | null {
  if (value === null || value === undefined) return null;
  const found = messages(value);
  if (!found) return plain ? pretty(value) || null : null;
  const users = found.filter((m) => m.role === "user" && texts([m]));
  const last = users[users.length - 1];
  return (last ? texts([last]) : plain ? texts(found) : "") || null;
}

/**
 * Argument names that mean "say this to the user" when an agent ends a turn by calling a tool
 * (`finish_turn {reply}`, `send_message {message}`). Kept short on purpose: `content`, `text`
 * or `output` are as often a file's body or a command's input, and showing those as the reply
 * to a turn that never replied is worse than showing none.
 */
const SPOKEN = ["reply", "answer", "response", "message"];

export interface Reply {
  text: string;
  /** The tool the reply was sent through, when it was not plain text. */
  via: string | null;
}

/**
 * The reply in a captured output. When the output ends in a tool call with a SPOKEN argument,
 * that argument: it is what the user received, and any text beside it is the model's preamble.
 * Otherwise the output's text. An output that only calls some other tool holds no reply: the
 * turn stopped before answering (see `lastCalled`).
 */
export function reply(value: unknown): Reply | null {
  if (value === null || value === undefined) return null;
  const found = messages(value);
  if (!found) {
    const text = pretty(value);
    return text ? { text, via: null } : null;
  }
  const parts = found.flatMap((m) => m.parts);
  const last = parts[parts.length - 1];
  if (last?.type === "tool_call") {
    const args = record(last.arguments);
    for (const name of SPOKEN) {
      const spoken = args?.[name];
      if (typeof spoken === "string" && spoken.trim()) return { text: spoken.trim(), via: last.name };
    }
  }
  const text = texts(found);
  return text ? { text, via: null } : null;
}

/** The tool a captured output last called, when it is a list of messages that calls one. */
export function lastCalled(value: unknown): string | null {
  const calls = (messages(value) ?? []).flatMap((m) => m.parts).flatMap((p) => (p.type === "tool_call" ? [p.name] : []));
  return calls[calls.length - 1] ?? null;
}

export interface Summary {
  /** What was asked: the root's captured input, else the earliest captured user message. */
  asked: string | null;
  replied: Reply | null;
  /** Without a reply: the tool the latest captured output called, where the turn stopped. */
  stoppedAt: string | null;
  /** The earliest failed span that no failed span below it explains. */
  firstError: SpanRecord | null;
  errors: number;
  /** The root span did not fail, so whatever failed, the turn went on past it. */
  finished: boolean;
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

/**
 * The failed spans that started a failure, earliest first: those with no failed span anywhere
 * below them. A failed span above another failed one, however many spans that succeeded lie
 * between, only reports it.
 */
export function errorOrigins(spans: SpanRecord[]): SpanRecord[] {
  const byId = new Map(spans.map((span) => [span.span_id, span]));
  const failed = spans.filter((span) => span.status_code === STATUS_ERROR);
  const explained = new Set<string>();
  for (const span of failed) {
    const seen = new Set([span.span_id]); // guards against parent cycles
    let above = span.parent_span_id === null ? undefined : byId.get(span.parent_span_id);
    while (above && !seen.has(above.span_id)) {
      seen.add(above.span_id);
      explained.add(above.span_id);
      above = above.parent_span_id === null ? undefined : byId.get(above.parent_span_id);
    }
  }
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
  const output = (span: SpanRecord) => attribute(span, OUTPUT_ATTRIBUTES);
  const origins = errorOrigins(spans);
  // The root's own output is the turn's, when it has one; else the latest span's.
  const latest = (root && output(root)) ?? first([...ordered].reverse(), output);
  const replied = reply(latest);
  return {
    asked:
      (root && asked(attribute(root, INPUT_ATTRIBUTES), true)) ??
      first(ordered, (span) => asked(attribute(span, INPUT_ATTRIBUTES), false)),
    replied,
    stoppedAt: replied ? null : lastCalled(latest),
    firstError: origins[0] ?? null,
    errors: origins.length,
    finished: root !== undefined && root.status_code !== STATUS_ERROR,
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
