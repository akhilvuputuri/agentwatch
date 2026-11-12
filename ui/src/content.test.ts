import { describe, expect, it } from "vitest";
import type { SpanRecord } from "./api";
import { displayName, errorOrigins, errorText, messages, parsed, plainText, pretty, reply, spanInput, spanOutput, summarise } from "./content";

let next = 0;
function span(over: Omit<Partial<SpanRecord>, "genai"> & { genai?: Partial<SpanRecord["genai"]> } = {}): SpanRecord {
  next += 1;
  const { genai, ...rest } = over;
  return {
    trace_id: "t".repeat(32),
    span_id: next.toString(16).padStart(16, "0"),
    parent_span_id: null,
    name: "span",
    kind: 1,
    start_ns: String(next * 1000),
    end_ns: String(next * 1000 + 500),
    status_code: 0,
    status_message: "",
    service_name: "svc",
    scope_name: "",
    attributes: {},
    resource: {},
    events: [],
    links: [],
    genai: {
      operation: null,
      provider: null,
      model: null,
      input_tokens: null,
      output_tokens: null,
      cache_read_tokens: null,
      cache_write_tokens: null,
      reasoning_tokens: null,
      cost_usd: null,
      finish_reasons: [],
      tool_name: null,
      agent_name: null,
      error_type: null,
      conversation_id: null,
      user_id: null,
      cost_source: null,
      ...genai,
    },
    ...rest,
  };
}

const text = (role: string, content: string) => ({ role, parts: [{ type: "text", content }] });

describe("parsed and pretty", () => {
  it("parses JSON objects and lists held as text, and nothing else", () => {
    expect(parsed('{"a":1}')).toEqual({ a: 1 });
    expect(parsed(" [1] ")).toEqual([1]);
    expect(parsed("12")).toBe("12");
    expect(parsed("{not json")).toBe("{not json");
    expect(parsed(7)).toBe(7);
  });

  it("indents JSON and keeps plain text, line breaks included", () => {
    expect(pretty('{"a":{"b":1}}')).toBe('{\n  "a": {\n    "b": 1\n  }\n}');
    expect(pretty("line one\nline two")).toBe("line one\nline two");
    expect(pretty(null)).toBe("");
  });
});

describe("messages", () => {
  it("reads the GenAI parts shape from JSON text", () => {
    const value = JSON.stringify([
      text("user", "hi"),
      {
        role: "assistant",
        parts: [
          { type: "reasoning", content: "think" },
          { type: "tool_call", id: "c1", name: "search", arguments: '{"q":"x"}' },
          { type: "image" },
        ],
      },
      { role: "tool", parts: [{ type: "tool_call_response", id: "c1", response: { ok: true } }] },
    ]);
    expect(messages(value)).toEqual([
      { role: "user", parts: [{ type: "text", text: "hi" }] },
      {
        role: "assistant",
        parts: [
          { type: "reasoning", text: "think" },
          { type: "tool_call", id: "c1", name: "search", arguments: { q: "x" } },
          { type: "other", kind: "image" },
        ],
      },
      { role: "tool", parts: [{ type: "tool_result", id: "c1", result: { ok: true } }] },
    ]);
  });

  it("reads the chat-completions shape", () => {
    expect(
      messages([
        { role: "user", content: "hi" },
        { role: "assistant", content: null, tool_calls: [{ id: "c", function: { name: "f", arguments: "{}" } }] },
      ]),
    ).toEqual([
      { role: "user", parts: [{ type: "text", text: "hi" }] },
      { role: "assistant", parts: [{ type: "tool_call", id: "c", name: "f", arguments: {} }] },
    ]);
  });

  it("is null for anything that is not a list of messages", () => {
    expect(messages("plain text")).toBeNull();
    expect(messages([])).toBeNull();
    expect(messages([{ parts: "x" }])).toBeNull();
    expect(messages([text("user", "a"), 3])).toBeNull();
    expect(messages({ role: "user" })).toBeNull();
  });
});

describe("reply", () => {
  it("is the text when there is text", () => {
    expect(reply([text("assistant", "done")])).toEqual({ text: "done", via: null });
    expect(reply("free text")).toEqual({ text: "free text", via: null });
  });

  it("is the last tool call's spoken argument when the model only called a tool", () => {
    const call = (args: unknown) => [{ role: "assistant", parts: [{ type: "tool_call", id: "c", name: "finish_turn", arguments: args }] }];
    expect(reply(call({ reply: "All set.", reason: "done" }))).toEqual({ text: "All set.", via: "finish_turn" });
    expect(reply(call({ note: "only one string", n: 2 }))).toEqual({ text: "only one string", via: "finish_turn" });
    // No argument is plainly the reply: show them all.
    expect(reply(call({ a: "x", b: "y" }))).toEqual({ text: '{\n  "a": "x",\n  "b": "y"\n}', via: "finish_turn" });
  });

  it("is null when nothing was captured", () => {
    expect(reply(null)).toBeNull();
    expect(reply([{ role: "assistant", parts: [] }])).toBeNull();
  });
});

describe("summarise", () => {
  it("takes the root's messages, counts calls and finds the first error no child explains", () => {
    const root = span({
      name: "invoke_agent a",
      status_code: 2,
      genai: { operation: "invoke_agent" },
      attributes: {
        "gen_ai.input.messages": JSON.stringify([text("system", "be brief"), text("user", "first"), text("user", "book it")]),
        "gen_ai.output.messages": JSON.stringify([text("assistant", "booked")]),
      },
    });
    const chat = span({ parent_span_id: root.span_id, genai: { operation: "chat", model: "m", input_tokens: 5 } });
    const helper = span({ parent_span_id: root.span_id, status_code: 2, genai: { operation: "invoke_agent" } });
    const tool = span({ parent_span_id: helper.span_id, status_code: 2, genai: { operation: "execute_tool", tool_name: "book" } });
    const late = span({ parent_span_id: root.span_id, status_code: 2, genai: { operation: "execute_tool", tool_name: "mail" } });
    const all = [late, tool, helper, chat, root];
    const summary = summarise(all, root);
    expect(summary.asked).toBe("book it");
    expect(summary.replied).toEqual({ text: "booked", via: null });
    expect(summary.firstError?.span_id).toBe(tool.span_id);
    expect(summary.errors).toBe(2);
    expect(summary.modelCalls).toBe(1);
    expect(summary.toolCalls).toBe(2);
    expect(errorOrigins(all).map((each) => each.span_id)).toEqual([tool.span_id, late.span_id]);
  });

  it("falls back to the earliest input and the latest output on other spans", () => {
    const root = span({ genai: { operation: "invoke_agent" } });
    const first = span({
      parent_span_id: root.span_id,
      attributes: { "gen_ai.input.messages": JSON.stringify([text("user", "q")]), "gen_ai.output.messages": JSON.stringify([text("assistant", "early")]) },
    });
    const last = span({ parent_span_id: root.span_id, attributes: { "output.value": "late" } });
    const summary = summarise([root, first, last], root);
    expect(summary.asked).toBe("q");
    expect(summary.replied).toEqual({ text: "late", via: null });
    expect(summary.firstError).toBeNull();
  });

  it("is empty for a trace with no content", () => {
    expect(summarise([], undefined)).toEqual({ asked: null, replied: null, firstError: null, errors: 0, modelCalls: 0, toolCalls: 0 });
  });
});

describe("span content", () => {
  it("prefers a tool's arguments and result", () => {
    const tool = span({ attributes: { "gen_ai.tool.call.arguments": '{"a":1}', "gen_ai.tool.call.result": "ok", "input.value": "x" } });
    expect(spanInput(tool)).toBe('{"a":1}');
    expect(spanOutput(tool)).toBe("ok");
    expect(spanInput(span())).toBeNull();
    expect(spanOutput(span({ attributes: { "gen_ai.output.messages": "" } }))).toBeNull();
  });

  it("names a span by its tool, agent or model", () => {
    expect(displayName(span({ name: "execute_tool x", genai: { tool_name: "x" } }))).toBe("x");
    expect(displayName(span({ name: "chat m", genai: { model: "m" } }))).toBe("m");
    expect(displayName(span({ name: "plain" }))).toBe("plain");
  });

  it("gathers what a failed span says about why", () => {
    const failed = span({
      status_code: 2,
      status_message: "boom",
      events: [
        { name: "exception", time_ns: "1", attributes: { "exception.type": "ValueError", "exception.message": "bad", "exception.stacktrace": "at a\nat b" } },
        { name: "retry", time_ns: "2", attributes: {} },
      ],
    });
    expect(errorText(failed)).toBe("boom\nValueError: bad\nat a\nat b");
    expect(errorText(span({ status_code: 2 }))).toBe("");
  });

  it("writes messages as text to copy", () => {
    const value = [text("user", "hi"), { role: "assistant", parts: [{ type: "tool_call", name: "f", arguments: { a: 1 } }] }];
    expect(plainText(value)).toBe('user:\nhi\n\nassistant:\n[calls f] {\n  "a": 1\n}');
    expect(plainText('{"a":1}')).toBe('{\n  "a": 1\n}');
  });
});
