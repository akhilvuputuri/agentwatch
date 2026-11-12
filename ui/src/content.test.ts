import { describe, expect, it } from "vitest";
import type { SpanRecord } from "./api";
import {
  displayName,
  errorOrigins,
  errorText,
  lastCalled,
  messages,
  parsed,
  plainText,
  pretty,
  reply,
  spanInput,
  spanOutput,
  summarise,
} from "./content";

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

  it("leaves JSON as written when parsing would change a long number", () => {
    const text = '{"id":12345678901234567890,"ok":true}';
    expect(JSON.stringify(JSON.parse(text))).not.toBe(text); // the reason for the rule
    expect(pretty(text)).toBe(text);
    expect(pretty('{"id":123456789012345}')).toBe('{\n  "id": 123456789012345\n}');
    // The same for a tool call's arguments and a tool result held as text inside a message.
    const kept = messages([
      { role: "assistant", parts: [{ type: "tool_call", name: "send", arguments: '{"chat":12345678901234567890}' }] },
      { role: "tool", parts: [{ type: "tool_call_response", response: '{"id":98765432109876543210}' }] },
    ]);
    expect(kept?.[0]?.parts[0]).toMatchObject({ arguments: '{"chat":12345678901234567890}' });
    expect(kept?.[1]?.parts[0]).toMatchObject({ result: '{"id":98765432109876543210}' });
    expect(plainText(kept)).toContain("12345678901234567890");
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

  const call = (name: string, args: unknown) => ({ type: "tool_call", id: "c", name, arguments: args });
  const says = (...parts: unknown[]) => [{ role: "assistant", parts }];

  it("is the spoken argument when the output ends by calling a tool that says something", () => {
    expect(reply(says(call("finish_turn", { reply: "All set.", reason: "done" })))).toEqual({ text: "All set.", via: "finish_turn" });
    expect(reply(says(call("send_message", '{"message":" hi "}')))).toEqual({ text: "hi", via: "send_message" });
    // What the user received, not the model's preamble beside it.
    expect(reply(says({ type: "text", content: "Let me wrap up." }, call("finish_turn", { reply: "Real reply" })))).toEqual({
      text: "Real reply",
      via: "finish_turn",
    });
  });

  it("does not take another tool's arguments for a reply", () => {
    // A turn that stopped after asking for a tool never replied.
    expect(reply(says(call("get_weather", { city: "Paris" })))).toBeNull();
    expect(reply(says(call("write_file", { path: "a.txt", content: "the file body" })))).toBeNull();
    expect(reply(says(call("run", { text: "ls", output: "x" })))).toBeNull();
    expect(reply(says(call("finish_turn", { reply: "  " })))).toBeNull();
    // Text before a call to some other tool is the model's preamble, not a reply.
    expect(reply(says({ type: "text", content: "I'll check the weather." }, call("get_weather", { city: "Paris" })))).toBeNull();
    // A call followed by text ends in text, which is the latest thing said.
    expect(reply(says(call("get_weather", { city: "Paris" }), { type: "text", content: "Sunny." }))).toEqual({ text: "Sunny.", via: null });
    expect(lastCalled(says(call("a", {}), call("get_weather", {})))).toBe("get_weather");
    expect(lastCalled([text("assistant", "done")])).toBeNull();
    expect(lastCalled("plain")).toBeNull();
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
    expect(summary.stoppedAt).toBeNull();
    expect(summary.finished).toBe(false);
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

  it("finds the failure below a span that succeeded, not the failed span above it", () => {
    const root = span({ status_code: 2, status_message: "turn failed", genai: { operation: "invoke_agent" } });
    const step = span({ parent_span_id: root.span_id });
    const tool = span({ parent_span_id: step.span_id, status_code: 2, genai: { operation: "execute_tool", tool_name: "fetch" } });
    const summary = summarise([root, step, tool], root);
    expect(summary.firstError?.span_id).toBe(tool.span_id);
    expect(summary.errors).toBe(1);
    // A parent that is missing, or a cycle, ends the walk.
    const orphan = span({ parent_span_id: "f".repeat(16), status_code: 2 });
    const a = span({ status_code: 2 });
    const b = span({ parent_span_id: a.span_id, status_code: 2 });
    a.parent_span_id = b.span_id;
    expect(errorOrigins([orphan]).map((each) => each.span_id)).toEqual([orphan.span_id]);
    // Spans that only explain each other still failed: the earliest of them.
    expect(errorOrigins([b, a]).map((each) => each.span_id)).toEqual([a.span_id]);
  });

  it("says where a turn that never replied stopped, and that a recovered failure went on", () => {
    const stopped = span({ status_code: 2, genai: { operation: "invoke_agent" } });
    const asking = span({
      parent_span_id: stopped.span_id,
      attributes: {
        "gen_ai.output.messages": JSON.stringify([{ role: "assistant", parts: [{ type: "tool_call", name: "get_weather", arguments: { city: "Paris" } }] }]),
      },
    });
    const failed = summarise([stopped, asking], stopped);
    expect(failed.replied).toBeNull();
    expect(failed.stoppedAt).toBe("get_weather");
    expect(failed.finished).toBe(false);

    const root = span({ genai: { operation: "invoke_agent" }, attributes: { "gen_ai.output.messages": JSON.stringify([text("assistant", "done")]) } });
    const retry = span({ parent_span_id: root.span_id, status_code: 2 });
    const recovered = summarise([root, retry], root);
    expect(recovered.finished).toBe(true);
    expect(recovered.firstError?.span_id).toBe(retry.span_id);
  });

  it("takes plain text for the request only from the root", () => {
    const root = span({ genai: { operation: "invoke_agent" } });
    const search = span({ parent_span_id: root.span_id, attributes: { "input.value": "search: user prefs" } });
    const chat = span({ parent_span_id: root.span_id, attributes: { "gen_ai.input.messages": JSON.stringify([text("system", "s"), text("user", "the question")]) } });
    expect(summarise([root, search, chat], root).asked).toBe("the question");
    expect(summarise([root, search], root).asked).toBeNull();
    const plain = span({ attributes: { "input.value": "typed request" } });
    expect(summarise([plain], plain).asked).toBe("typed request");
  });

  it("is empty for a trace with no content", () => {
    expect(summarise([], undefined)).toEqual({
      asked: null,
      replied: null,
      stoppedAt: null,
      firstError: null,
      errors: 0,
      finished: false,
      modelCalls: 0,
      toolCalls: 0,
    });
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
