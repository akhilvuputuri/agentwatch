import { describe, expect, it } from "vitest";
import { links } from "./api";
import { route } from "./App";

describe("route", () => {
  it("parses every view", () => {
    expect(route("")).toEqual({ page: "traces" });
    expect(route("#/dashboard")).toEqual({ page: "dashboard" });
    expect(route("#/sessions")).toEqual({ page: "sessions" });
    expect(route("#/prices")).toEqual({ page: "prices" });
    expect(route(`#/trace/${"A".repeat(32)}`)).toEqual({ page: "trace", traceId: "a".repeat(32) });
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
