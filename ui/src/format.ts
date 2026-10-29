/** Formatting helpers shared by the views. */

export function nsToMs(ns: bigint): number {
  return Number(ns / 1000n) / 1000;
}

export function duration(ns: number): string {
  if (!Number.isFinite(ns) || ns < 0) return "–";
  const ms = ns / 1e6;
  if (ms < 1) return `${(ns / 1e3).toFixed(0)} µs`;
  if (ms < 1000) return `${ms.toFixed(ms < 10 ? 1 : 0)} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(s < 10 ? 2 : 1)} s`;
  // Whole smaller units, rounded down, so a remainder never reads "60 s" or "24 h".
  const whole = Math.floor(s);
  if (whole < 3600) return `${Math.floor(whole / 60)} m ${whole % 60} s`;
  if (whole < 86400) return `${Math.floor(whole / 3600)} h ${Math.floor((whole % 3600) / 60)} m`;
  return `${Math.floor(whole / 86400)} d ${Math.floor((whole % 86400) / 3600)} h`;
}

export function count(value: number | null | undefined): string {
  if (value === null || value === undefined) return "–";
  if (value < 1000) return String(value);
  if (value < 1_000_000) return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0)}k`;
  return `${(value / 1_000_000).toFixed(1)}M`;
}

export function usd(value: number | null | undefined): string {
  if (value === null || value === undefined) return "–";
  if (value === 0) return "$0";
  if (value < 0.01) return `$${value.toFixed(4)}`;
  return `$${value.toFixed(value < 1 ? 3 : 2)}`;
}

export function share(part: number, whole: number): string {
  if (whole <= 0) return "–";
  return `${Math.round((part / whole) * 100)}%`;
}

export function timestamp(ns: string): string {
  const date = new Date(nsToMs(BigInt(ns)));
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** A bucket width in words: "minute", "15 minutes", "hour", "6 hours", "day", "week". */
export function period(ns: number): string {
  const minutes = Math.round(ns / 6e10);
  const units: [number, string][] = [
    [7 * 24 * 60, "week"],
    [24 * 60, "day"],
    [60, "hour"],
    [1, "minute"],
  ];
  for (const [size, name] of units) {
    if (minutes >= size && minutes % size === 0) {
      const n = minutes / size;
      return n === 1 ? name : `${n} ${name}s`;
    }
  }
  return duration(ns);
}

/** Axis money: as few decimals as the step needs. */
export function usdTick(value: number): string {
  if (value === 0) return "$0";
  if (value >= 100) return `$${value.toFixed(0)}`;
  if (value >= 1) return `$${Number(value.toFixed(2))}`;
  return `$${Number(value.toPrecision(2))}`;
}

/** Axis duration from milliseconds: 0, 500 ms, 2.5 s. */
export function msTick(value: number): string {
  if (value === 0) return "0";
  return value < 1000 ? `${Number(value.toPrecision(3))} ms` : `${Number((value / 1000).toPrecision(3))} s`;
}

/** A score for display: the mean for numbers, yes/no (or a share) for booleans, else the label. */
export function scoreText(type: "NUMERIC" | "BOOLEAN" | "CATEGORICAL", value: number | null, label: string | null, count = 1): string {
  if (type === "BOOLEAN" && value !== null) {
    if (count > 1) return `${Math.round(value * 100)}% yes`;
    return value >= 0.5 ? "yes" : "no";
  }
  if (type === "NUMERIC" && value !== null) {
    const text = Math.abs(value) >= 100 || Number.isInteger(value) ? String(Math.round(value * 100) / 100) : value.toFixed(2);
    return label ? `${text} (${label})` : text;
  }
  return label ?? "–";
}
