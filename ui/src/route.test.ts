import { describe, expect, it } from "vitest";
import { links } from "./api";
import { route } from "./App";

describe("route", () => {
  it("parses every view", () => {
    expect(route("")).toEqual({ page: "traces" });
    expect(route("#/dashboard")).toEqual({ page: "dashboard" });
    expect(route("#/sessions")).toEqual({ page: "sessions" });
    expect(route("#/prices")).toEqual({ page: "prices" });
    expect(route("#/judges")).toEqual({ page: "judges" });
    expect(route("#/alerts")).toEqual({ page: "alerts" });
    expect(route("#/drift")).toEqual({ page: "drift" });
    expect(route("#/datasets")).toEqual({ page: "datasets" });
    expect(route(links.run("r 1"))).toEqual({ page: "run", runId: "r 1" });
    expect(route(links.compare("a/1", "b"))).toEqual({ page: "compare", baseline: "a/1", candidate: "b" });
    expect(route(links.dataset("a b/c"))).toEqual({ page: "dataset", datasetId: "a b/c" });
    expect(route(`#/trace/${"A".repeat(32)}`)).toEqual({ page: "trace", traceId: "a".repeat(32) });
    expect(route(links.trace("a".repeat(32), "B".repeat(16)))).toEqual({
      page: "trace",
      traceId: "a".repeat(32),
      spanId: "b".repeat(16),
    });
    expect(route("#/memory")).toEqual({ page: "memory" });
    expect(route(links.memoryItem("", "lesson:a b/c&d"))).toEqual({ page: "memory-item", store: "", id: "lesson:a b/c&d" });
    expect(route("#/memory/item?store=s")).toEqual({ page: "memory" });
  });
  it("round-trips session and user IDs of any shape", () => {
    const odd = "team/chat 1?x=y&z#w%";
    expect(route(links.session(odd))).toEqual({ page: "session", sessionId: odd });
    expect(route(links.traces({ session: odd, user: "u&1" }))).toEqual({ page: "traces", session: odd, user: "u&1" });
    expect(route(links.traces({}))).toEqual({ page: "traces" });
  });
  it("falls back on malformed encodings", () => {
    expect(route("#/session/%E0%A4%A")).toEqual({ page: "sessions" });
  });
});
