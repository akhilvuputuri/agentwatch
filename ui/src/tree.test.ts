import { describe, expect, it } from "vitest";
import type { SpanRecord } from "./api";
import { layout } from "./tree";

function span(id: string, parent: string | null, start: number, end: number): SpanRecord {
  const base = 1_761_000_000_000_000_000n;
  return {
    trace_id: "t".repeat(32),
    span_id: id,
    parent_span_id: parent,
    name: id,
    kind: 1,
    start_ns: String(base + BigInt(start)),
    end_ns: String(base + BigInt(end)),
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
    },
  };
}

describe("layout", () => {
  it("orders children depth-first by start time", () => {
    const result = layout([span("c2", "root", 50, 60), span("root", null, 0, 100), span("c1", "root", 10, 20), span("g", "c1", 12, 15)]);
    expect(result.rows.map((row) => [row.span.span_id, row.depth])).toEqual([
      ["root", 0],
      ["c1", 1],
      ["g", 2],
      ["c2", 1],
    ]);
    expect(result.totalNs).toBe(100);
    expect(result.rows[1]!.offsetNs).toBe(10);
  });

  it("keeps nanosecond precision beyond Number's exact range", () => {
    const result = layout([span("a", null, 0, 3), span("b", "a", 1, 2)]);
    expect(result.rows[1]!.offsetNs).toBe(1);
    expect(result.rows[1]!.durationNs).toBe(1);
  });

  it("shows spans with a missing parent as orphan roots after the true root", () => {
    const result = layout([span("a", null, 3, 10), span("b", "missing", 1, 6)]);
    expect(result.rows.map((row) => [row.span.span_id, row.depth, row.orphan])).toEqual([
      ["a", 0, false],
      ["b", 0, true],
    ]);
  });

  it("does not drop spans caught in a parent cycle", () => {
    const result = layout([span("a", "b", 0, 10), span("b", "a", 1, 5)]);
    expect(result.rows).toHaveLength(2);
  });

  it("handles an empty trace", () => {
    expect(layout([]).rows).toEqual([]);
  });
});
