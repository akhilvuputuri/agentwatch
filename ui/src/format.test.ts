import { describe, expect, it } from "vitest";
import { count, duration, nsToMs, share, usd } from "./format";

describe("format", () => {
  it("durations", () => {
    expect(duration(500)).toBe("1 µs");
    expect(duration(2_500_000)).toBe("2.5 ms");
    expect(duration(250_000_000)).toBe("250 ms");
    expect(duration(3_210_000_000)).toBe("3.21 s");
    expect(duration(125_000_000_000)).toBe("2 m 5 s");
    expect(duration(-1)).toBe("–");
  });
  it("counts and money", () => {
    expect(count(999)).toBe("999");
    expect(count(1500)).toBe("1.5k");
    expect(count(25_000)).toBe("25k");
    expect(count(null)).toBe("–");
    expect(usd(0.00123)).toBe("$0.0012");
    expect(usd(0.5)).toBe("$0.500");
    expect(usd(12.345)).toBe("$12.35");
    expect(usd(null)).toBe("–");
    expect(share(1, 4)).toBe("25%");
    expect(share(1, 0)).toBe("–");
  });
  it("converts nanoseconds without losing milliseconds", () => {
    expect(nsToMs(1_761_000_000_123_456_789n)).toBe(1_761_000_000_123.456);
  });
});
