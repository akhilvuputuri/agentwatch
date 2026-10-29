import { describe, expect, it } from "vitest";
import { niceTicks } from "./ticks";

describe("niceTicks", () => {
  it("covers the maximum with clean steps from zero", () => {
    expect(niceTicks(9.3)).toEqual([0, 2.5, 5, 7.5, 10]);
    expect(niceTicks(1200)).toEqual([0, 500, 1000, 1500]);
    expect(niceTicks(0.37)).toEqual([0, 0.1, 0.2, 0.3, 0.4]);
    expect(niceTicks(4)).toEqual([0, 1, 2, 3, 4]);
  });
  it("handles empty data", () => {
    expect(niceTicks(0)).toEqual([0, 1]);
    expect(niceTicks(Number.NaN)).toEqual([0, 1]);
  });
});
