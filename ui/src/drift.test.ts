import { describe, expect, it } from "vitest";
import { boundText, driftAlertText } from "./Alerts";
import type { Alert, DetectedChange } from "./api";
import { beforeAfter, changeService } from "./Drift";

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

describe("changeService", () => {
  const change = (service: string | null): DetectedChange => ({ kind: "version", name: "2", service, first_seen_ns: "1" });
  it("uses the change's own service, or the page's when it has none", () => {
    expect(changeService(change("api"), "other")).toBe("api");
    expect(changeService(change(null), "other")).toBe("other");
  });
});

describe("a falling drift monitor", () => {
  it("shows a negative bound with its sign and unit", () => {
    expect(boundText("cost_per_trace", -0.5, false)).toBe("−$0.500");
    expect(boundText("model_p95_ms", -250, false)).toBe("−250 ms");
    expect(boundText("model_error_rate", -0.125, true)).toBe("−12.5%");
    expect(boundText("model_error_rate", 0.125, true)).toBe("+12.5%");
  });
});

describe("a drift alert", () => {
  const alert = (fields: Partial<Alert>): Alert =>
    ({ kind: "firing", metric: "model_error_rate", mode: "drift", comparison: "above", value: 0.4, baseline_value: 0.1, ...fields }) as Alert;
  it("says what moved when firing, and that the rise has gone when resolved", () => {
    expect(driftAlertText(alert({}), true)).toBe("from 10% to 40%");
    expect(driftAlertText(alert({ kind: "resolved", value: 0.4, baseline_value: 0.39 }), true)).toBe("no longer a rise: before 39%, now 40%");
    expect(driftAlertText(alert({ kind: "resolved", value: 0.13, baseline_value: 0.1, threshold: 0.05 }), true)).toBe("no longer a rise of at least 5%: before 10%, now 13%");
    expect(driftAlertText(alert({ kind: "resolved", value: null, baseline_value: null }), true)).toBe("–");
  });
});
