import type { SpanRecord } from "./api";

export interface Row {
  span: SpanRecord;
  depth: number;
  /** Offset from the trace start and duration, in nanoseconds. */
  offsetNs: number;
  durationNs: number;
  /** True when the span names a parent that is not in this trace. */
  orphan: boolean;
}

export interface Layout {
  rows: Row[];
  startNs: bigint;
  totalNs: number;
}

/**
 * Order spans depth-first under their parents, earliest child first. Spans whose
 * parent is missing are shown as extra roots and marked as orphans.
 */
export function layout(spans: SpanRecord[]): Layout {
  if (spans.length === 0) return { rows: [], startNs: 0n, totalNs: 0 };
  const byId = new Map(spans.map((span) => [span.span_id, span]));
  const children = new Map<string, SpanRecord[]>();
  const roots: SpanRecord[] = [];
  for (const span of spans) {
    const parent = span.parent_span_id;
    if (parent !== null && parent !== span.span_id && byId.has(parent)) {
      const list = children.get(parent) ?? [];
      list.push(span);
      children.set(parent, list);
    } else {
      roots.push(span);
    }
  }
  const byStart = (a: SpanRecord, b: SpanRecord) => {
    const diff = BigInt(a.start_ns) - BigInt(b.start_ns);
    return diff < 0n ? -1 : diff > 0n ? 1 : a.span_id.localeCompare(b.span_id);
  };
  let start = BigInt(spans[0]!.start_ns);
  let end = BigInt(spans[0]!.end_ns);
  for (const span of spans) {
    const s = BigInt(span.start_ns);
    const e = BigInt(span.end_ns);
    if (s < start) start = s;
    if (e > end) end = e;
  }
  const rows: Row[] = [];
  const seen = new Set<string>();
  const visit = (span: SpanRecord, depth: number) => {
    if (seen.has(span.span_id)) return; // guards against parent cycles
    seen.add(span.span_id);
    const s = BigInt(span.start_ns);
    const e = BigInt(span.end_ns);
    rows.push({
      span,
      depth,
      offsetNs: Number(s - start),
      durationNs: Number(e > s ? e - s : 0n),
      orphan: depth === 0 && span.parent_span_id !== null,
    });
    for (const child of (children.get(span.span_id) ?? []).sort(byStart)) visit(child, depth + 1);
  };
  // A span with no parent at all is the trace's root; orphans (parent never received) follow.
  const rootFirst = (a: SpanRecord, b: SpanRecord) =>
    Number(a.parent_span_id !== null) - Number(b.parent_span_id !== null) || byStart(a, b);
  for (const root of roots.sort(rootFirst)) visit(root, 0);
  // Spans caught in a parent cycle have no root; show them flat rather than dropping them.
  for (const span of [...spans].sort(byStart)) visit(span, 0);
  return { rows, startNs: start, totalNs: Number(end - start) };
}
