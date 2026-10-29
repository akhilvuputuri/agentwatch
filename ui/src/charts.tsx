import { useEffect, useRef, useState, type ReactNode } from "react";
import { niceTicks } from "./ticks";

const HEIGHT = 190;
const MARGIN = { top: 10, right: 12, bottom: 24, left: 52 };

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(600);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setWidth(Math.max(240, Math.floor(entry.contentRect.width)));
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

export interface Tooltip {
  /** The headline value, shown first and strong. */
  value: string;
  label: string;
  rows?: { key: string; value: string; color?: string }[];
}

function TooltipBox({ tip, x, width }: { tip: Tooltip; x: number; width: number }) {
  // Keep the box inside the chart: flip to the left of the pointer near the right edge.
  const left = x > width - 180 ? Math.max(0, x - 172) : x + 12;
  return (
    <div className="chart-tip" style={{ left }} role="status">
      {tip.rows ? null : <strong>{tip.value}</strong>}
      {tip.rows?.map((row) => (
        <div key={row.key} className="tip-row">
          {row.color && <span className="tip-key" style={{ background: row.color }} />}
          <strong>{row.value}</strong> <span>{row.key}</span>
        </div>
      ))}
      <div className="tip-label">{tip.label}</div>
    </div>
  );
}

function Axes({
  ticks,
  scale,
  innerWidth,
  format,
  xLabels,
}: {
  ticks: number[];
  scale: (v: number) => number;
  innerWidth: number;
  format: (v: number) => string;
  xLabels: { x: number; text: string }[];
}) {
  return (
    <g className="axes">
      {ticks.map((tick) => (
        <g key={tick} transform={`translate(0,${scale(tick)})`}>
          <line x1={MARGIN.left} x2={MARGIN.left + innerWidth} className="grid" />
          <text x={MARGIN.left - 6} dy="0.32em" textAnchor="end">
            {format(tick)}
          </text>
        </g>
      ))}
      {xLabels.map((label, index) => (
        <text
          key={index}
          x={label.x}
          y={HEIGHT - 6}
          textAnchor={index === 0 ? "start" : index === xLabels.length - 1 ? "end" : "middle"}
        >
          {label.text}
        </text>
      ))}
    </g>
  );
}

/** A few evenly spaced labels for the x axis, by index; fewer on narrow charts. */
function xLabelIndices(count: number, width: number) {
  const wanted = width < 520 ? 2 : width < 760 ? 3 : 4;
  if (count <= 1) return [0];
  const step = (count - 1) / (wanted - 1);
  return [...new Set(Array.from({ length: wanted }, (_, i) => Math.round(i * step)))];
}

export interface BarDatum {
  label: string;
  value: number | null;
  tooltip: Tooltip;
}

/** One series of columns over time. Empty buckets draw nothing. */
export function BarChart({
  data,
  format,
  axisLabel,
  ariaLabel,
}: {
  data: BarDatum[];
  format: (v: number) => string;
  axisLabel: (index: number) => string;
  ariaLabel: string;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const innerWidth = width - MARGIN.left - MARGIN.right;
  const innerHeight = HEIGHT - MARGIN.top - MARGIN.bottom;
  const max = Math.max(0, ...data.map((d) => d.value ?? 0));
  const ticks = niceTicks(max);
  const top = ticks[ticks.length - 1] ?? 1;
  const scale = (v: number) => MARGIN.top + innerHeight - (v / top) * innerHeight;
  const band = innerWidth / Math.max(1, data.length);
  const barWidth = Math.max(1, Math.min(24, band - 2));
  const xLabels = xLabelIndices(data.length, width).map((i) => ({
    x: MARGIN.left + band * i + band / 2,
    text: axisLabel(i),
  }));
  return (
    <div className="chart" ref={ref}>
      <svg width={width} height={HEIGHT} role="img" aria-label={ariaLabel}>
        <Axes ticks={ticks} scale={scale} innerWidth={innerWidth} format={format} xLabels={xLabels} />
        {data.map((d, i) => {
          const x = MARGIN.left + band * i + (band - barWidth) / 2;
          const value = d.value ?? 0;
          const y = scale(value);
          const base = scale(0);
          const height = base - y;
          const radius = Math.min(4, barWidth / 2, height);
          return (
            <g key={i}>
              {value > 0 && (
                <path
                  className={`bar-mark${hover === i ? " active" : ""}`}
                  d={`M${x},${base} V${y + radius} Q${x},${y} ${x + radius},${y} H${x + barWidth - radius} Q${x + barWidth},${y} ${x + barWidth},${y + radius} V${base} Z`}
                />
              )}
              <rect
                x={MARGIN.left + band * i}
                y={MARGIN.top}
                width={band}
                height={innerHeight}
                fill="transparent"
                onMouseEnter={() => setHover(i)}
                onMouseLeave={() => setHover(null)}
              />
            </g>
          );
        })}
        <line x1={MARGIN.left} x2={MARGIN.left + innerWidth} y1={scale(0)} y2={scale(0)} className="baseline" />
      </svg>
      {hover !== null && data[hover] && (
        <TooltipBox tip={data[hover].tooltip} x={MARGIN.left + band * hover + band / 2} width={width} />
      )}
    </div>
  );
}

export interface LineSeries {
  name: string;
  /** A CSS color, normally a `var(--series-n)` token. */
  color: string;
  values: (number | null)[];
}

/** Lines over time with a crosshair; gaps where a bucket has no value. */
export function LineChart({
  series,
  format,
  tooltipFormat = format,
  axisLabel,
  bucketLabel,
  ariaLabel,
}: {
  series: LineSeries[];
  format: (v: number) => string;
  tooltipFormat?: (v: number) => string;
  axisLabel: (index: number) => string;
  bucketLabel: (index: number) => string;
  ariaLabel: string;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const count = Math.max(1, ...series.map((s) => s.values.length));
  const innerWidth = width - MARGIN.left - MARGIN.right - 44; // room for end labels
  const innerHeight = HEIGHT - MARGIN.top - MARGIN.bottom;
  const max = Math.max(0, ...series.flatMap((s) => s.values.map((v) => v ?? 0)));
  const ticks = niceTicks(max);
  const top = ticks[ticks.length - 1] ?? 1;
  const y = (v: number) => MARGIN.top + innerHeight - (v / top) * innerHeight;
  const step = count > 1 ? innerWidth / (count - 1) : 0;
  const x = (i: number) => MARGIN.left + (count > 1 ? step * i : innerWidth / 2);
  const xLabels = xLabelIndices(count, width).map((i) => ({ x: x(i), text: axisLabel(i) }));

  const paths = series.map((s) => {
    let d = "";
    let pen = false;
    s.values.forEach((v, i) => {
      if (v === null) {
        pen = false;
        return;
      }
      d += `${pen ? "L" : "M"}${x(i)},${y(v)} `;
      pen = true;
    });
    return d;
  });
  const lastIndex = (s: LineSeries) => {
    for (let i = s.values.length - 1; i >= 0; i--) if (s.values[i] !== null) return i;
    return -1;
  };
  const onMove = (event: React.MouseEvent<SVGRectElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    const offset = event.clientX - box.left;
    setHover(Math.max(0, Math.min(count - 1, Math.round(count > 1 ? offset / step : 0))));
  };

  return (
    <div className="chart" ref={ref}>
      <ul className="legend">
        {series.map((s) => (
          <li key={s.name}>
            <span className="legend-key" style={{ background: s.color }} />
            {s.name}
          </li>
        ))}
      </ul>
      <svg width={width} height={HEIGHT} role="img" aria-label={ariaLabel}>
        <Axes ticks={ticks} scale={y} innerWidth={innerWidth} format={format} xLabels={xLabels} />
        {hover !== null && (
          <line className="crosshair" x1={x(hover)} x2={x(hover)} y1={MARGIN.top} y2={MARGIN.top + innerHeight} />
        )}
        {series.map((s, index) => (
          <path key={s.name} d={paths[index]} className="line-mark" style={{ stroke: s.color }} />
        ))}
        {series.flatMap((s) =>
          // A value with no neighbour draws no line, so it gets a dot of its own.
          s.values.map((v, i) =>
            v !== null && (s.values[i - 1] ?? null) === null && (s.values[i + 1] ?? null) === null ? (
              <circle key={`${s.name}-${i}`} cx={x(i)} cy={y(v)} r={3} style={{ fill: s.color }} />
            ) : null,
          ),
        )}
        {series.map((s) => {
          const i = lastIndex(s);
          const v = i >= 0 ? s.values[i] : null;
          if (v === null || v === undefined) return null;
          return (
            <g key={s.name}>
              <circle cx={x(i)} cy={y(v)} r={4} className="end-dot" style={{ fill: s.color }} />
              <text x={x(i) + 8} y={y(v)} dy="0.32em" className="end-label">
                {s.name}
              </text>
            </g>
          );
        })}
        {hover !== null &&
          series.map((s) => {
            const v = s.values[hover];
            return v === null || v === undefined ? null : (
              <circle key={s.name} cx={x(hover)} cy={y(v)} r={4} className="end-dot" style={{ fill: s.color }} />
            );
          })}
        <rect
          x={MARGIN.left}
          y={MARGIN.top}
          width={innerWidth}
          height={innerHeight}
          fill="transparent"
          onMouseMove={onMove}
          onMouseLeave={() => setHover(null)}
        />
      </svg>
      {hover !== null && (
        <TooltipBox
          x={x(hover)}
          width={width}
          tip={{
            value: "",
            label: bucketLabel(hover),
            rows: series.map((s) => {
              const v = s.values[hover];
              return { key: s.name, value: v === null || v === undefined ? "–" : tooltipFormat(v), color: s.color };
            }),
          }}
        />
      )}
    </div>
  );
}

/** A collapsible data table under a chart, so no reading depends on the chart alone. */
export function ChartTable({ columns, rows }: { columns: string[]; rows: ReactNode[][] }) {
  return (
    <details className="chart-table">
      <summary>Data table</summary>
      <div className="table-wrap">
        <table className="traces">
          <thead>
            <tr>
              {columns.map((column, index) => (
                <th key={column} className={index ? "num" : undefined}>
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={index}>
                {row.map((cell, cellIndex) => (
                  <td key={cellIndex} className={cellIndex ? "num" : undefined}>
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}
