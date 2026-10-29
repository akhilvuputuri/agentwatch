import { describe, expect, it } from "vitest";
import { count, duration, msTick, nsToMs, period, share, usd, usdTick } from "./format";

describe("format", () => {
  it("durations", () => {
    expect(duration(500)).toBe("1 µs");
    expect(duration(2_500_000)).toBe("2.5 ms");
    expect(duration(250_000_000)).toBe("250 ms");
    expect(duration(3_210_000_000)).toBe("3.21 s");
    expect(duration(125_000_000_000)).toBe("2 m 5 s");
    expect(duration(2 * 3600e9 + 30 * 60e9)).toBe("2 h 30 m");
    expect(duration(46 * 3600e9 + 40 * 60e9)).toBe("1 d 23 h");
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
  it("names periods and axis ticks", () => {
    expect(period(60e9)).toBe("minute");
    expect(period(15 * 60e9)).toBe("15 minutes");
    expect(period(3600e9)).toBe("hour");
    expect(period(6 * 3600e9)).toBe("6 hours");
    expect(period(24 * 3600e9)).toBe("day");
    expect(period(7 * 24 * 3600e9)).toBe("week");
    expect(usdTick(0)).toBe("$0");
    expect(usdTick(0.8)).toBe("$0.8");
    expect(usdTick(0.025)).toBe("$0.025");
    expect(usdTick(2.5)).toBe("$2.5");
    expect(usdTick(150)).toBe("$150");
    expect(msTick(0)).toBe("0");
    expect(msTick(500)).toBe("500 ms");
    expect(msTick(12500)).toBe("12.5 s");
  });
  it("converts nanoseconds without losing milliseconds", () => {
    expect(nsToMs(1_761_000_000_123_456_789n)).toBe(1_761_000_000_123.456);
  });
});
