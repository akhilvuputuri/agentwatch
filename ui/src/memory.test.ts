import { describe, expect, it } from "vitest";
import { approxTokens, contentText, operationText } from "./Memory";

describe("memory display", () => {
  it("estimates tokens at four characters each, and none for nothing", () => {
    expect(approxTokens(0)).toBe(0);
    expect(approxTokens(1)).toBe(1);
    expect(approxTokens(400)).toBe(100);
  });
  it("shows content as text, and nothing when it was not captured", () => {
    expect(contentText("UTC+2")).toBe("UTC+2");
    expect(contentText({ offset: 2 })).toBe('{"offset":2}');
    expect(contentText(null)).toBeNull();
  });
  it("calls a search a read, never use", () => {
    expect(operationText("search_memory")).toBe("read");
    expect(operationText("delete_memory")).toBe("deleted");
  });
});
