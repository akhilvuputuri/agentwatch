import { describe, expect, it } from "vitest";
import { beforeAfter } from "./Drift";

const DAY = 24n * 3600n * 1_000_000_000n;

describe("beforeAfter", () => {
  const at = 100n * DAY;
  it("uses the same length each side", () => {
    expect(beforeAfter(String(at), 7, at + 30n * DAY)).toEqual({
      baseline_from: String(at - 7n * DAY),
      baseline_to: String(at),
      current_from: String(at),
      current_to: String(at + 7n * DAY),
    });
  });
  it("shortens both sides when the change is recent", () => {
    expect(beforeAfter(String(at), 7, at + DAY)).toEqual({
      baseline_from: String(at - DAY),
      baseline_to: String(at),
      current_from: String(at),
      current_to: String(at + DAY),
    });
  });
  it("has nothing to compare for a change not yet past", () => {
    expect(beforeAfter(String(at), 7, at)).toBeNull();
  });
});
