import { describe, expect, it } from "vitest";
import { preview } from "./Datasets";

describe("preview", () => {
  it("shows message text and survives odd shapes", () => {
    expect(preview([{ role: "user", parts: [{ type: "text", content: "hi" }] }])).toBe("user: hi");
    expect(preview([{ parts: "x" }])).toBe('[{"parts":"x"}]');
    expect(preview([])).toBe("[]");
    expect(preview(null)).toBe("–");
    expect(preview("a".repeat(200), 10)).toBe("aaaaaaaaa…");
  });
});
