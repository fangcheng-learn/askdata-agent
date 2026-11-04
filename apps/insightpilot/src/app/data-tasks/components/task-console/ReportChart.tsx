"use client";

import React from "react";
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Legend, Line, LineChart,
  Pie, PieChart, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis,
} from "recharts";
import {movementStyle} from './ReportNumbers';

export type ReportChartPoint = { label: string; value: number; series?: string; x?: number };
export type ReportChartData = {
  type: "bar" | "grouped_bar" | "line" | "area" | "pie" | "donut" | "scatter";
  title: string; unit?: string; currency?: string; precision?: number; source: string;
  x_unit?: string; x_precision?: number; x_title?: string; value_title?: string;
  total_points?: number; points: ReportChartPoint[];
};
const types = ["bar", "grouped_bar", "line", "area", "pie", "donut", "scatter"];
const colors = ["#386b8c", "#d29748", "#548270", "#8675a3", "#b46868", "#64748b"];

export function decodeReportChart(encoded: string): ReportChartData | null {
  try {
    if (encoded.length > 100_000) return null;
    const value = JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(encoded), c => c.charCodeAt(0)))) as ReportChartData;
    if (!types.includes(value.type) || typeof value.title !== "string" || typeof value.source !== "string"
      || !Array.isArray(value.points) || !value.points.length || value.points.length > 360
      || !value.points.every(p => typeof p.label === "string" && typeof p.value === "number" && Number.isFinite(p.value)
        && (p.series === undefined || typeof p.series === "string"))
      || (value.type === "scatter" && !value.points.every(p => typeof p.x === "number" && Number.isFinite(p.x)))
      || (["pie", "donut"].includes(value.type) && value.points.some(p => p.value < 0))) return null;
    return value;
  } catch { return null; }
}

/** Raw units come from the source; presentation never changes stored evidence. */
export function chartUnit(unit?: string, precision = 2) {
  const cents = /^([A-Z]{3}) cents(\/.*)?$/u.exec(unit ?? "");
  const ratio = unit === "ratio";
  return {
    scale: ratio ? 100 : cents ? .01 : 1,
    label: ratio ? "%" : cents ? `${cents[1]}${cents[2] ?? ""}` : unit ?? "",
    format: (value: number) => new Intl.NumberFormat("zh-CN", {
      maximumFractionDigits: ratio || cents ? 2 : Math.max(0, Math.min(precision, 6)),
    }).format(value),
  };
}

/** Use generated keys, never category/series text as object properties. Missing observations stay null. */
export function groupChartPoints(points: ReportChartPoint[]) {
  const series = Array.from(new Set(points.map(p => p.series ?? "数值")));
  const labels = Array.from(new Set(points.map(p => p.label)));
  const rows = labels.map(label => {
    const row: Record<string, string | number | null> = { label };
    series.forEach((name, i) => {
      const values = points.filter(p => p.label === label && (p.series ?? "数值") === name);
      row[`v${i}`] = values.length === 1 ? values[0]!.value : null;
    });
    return row;
  });
  return { rows, series };
}

type ComparisonRow = { label: string; before: number | null; after: number | null; delta: number | null; percent: number | null };

/** Compare two explicit ISO months only; arbitrary series are never treated as before/after. */
export function chartComparison(chart: ReportChartData): { periods: string[]; rows: ComparisonRow[] } | null {
  if (!["bar", "grouped_bar", "line", "area"].includes(chart.type)) return null;
  const month = (s: string) => /^\d{4}-(?:0[1-9]|1[0-2])$/u.test(s);
  const points = chart.points;
  const series = Array.from(new Set(points.map(p => p.series)));
  const labels = Array.from(new Set(points.map(p => p.label)));
  const seriesIsTime = series.length === 2 && series.every(s => typeof s === "string" && month(s));
  const labelsAreTime = labels.length === 2 && labels.every(month);
  if (!seriesIsTime && !labelsAreTime) return null;
  const periods = (seriesIsTime ? series as string[] : labels).slice().sort();
  const names = seriesIsTime ? labels : series.map(s => s ?? "数值");
  const rows: ComparisonRow[] = [];
  for (const label of names) {
    const values = periods.map(period => points.filter(p => seriesIsTime
      ? p.label === label && p.series === period : p.label === period && (p.series ?? "数值") === label));
    if (values.some(v => v.length > 1)) return null; // Never silently aggregate duplicates.
    const before = values[0]?.[0]?.value ?? null, after = values[1]?.[0]?.value ?? null;
    const delta = before !== null && after !== null ? after - before : null;
    rows.push({ label, before, after, delta, percent: delta !== null && before !== null && before > 0 ? delta / before * 100 : null });
  }
  return { periods, rows };
}

function ChangeValue({ value, format, suffix = "" }: { value: number | null; format: (n: number) => string; suffix?: string }) {
  if (value === null) return <>—</>;
  return <span className="font-semibold tabular-nums" data-metric-annotation={value>0?'up':value<0?'down':'flat'} style={{...movementStyle(value>0?'up':value<0?'down':'flat'),whiteSpace:"nowrap"}}>
    {value > 0 ? "+" : value < 0 ? "−" : ""}{format(Math.abs(value))}{suffix}
  </span>;
}

export function ReportChart({ encoded, hideTitle=false }: { encoded: string; hideTitle?:boolean }) {
  const chart = decodeReportChart(encoded);
  if (!chart) return <p role="status" className="my-4 text-sm text-muted">此图暂时无法展示，正文与原始结果仍可查看。</p>;
  const y = chartUnit(chart.unit, chart.precision);
  const x = chartUnit(chart.x_unit, chart.x_precision);
  const points = chart.points.map(p => ({ ...p, value: p.value * y.scale, ...(p.x !== undefined ? { x: p.x * x.scale } : {}) }));
  const grouped = groupChartPoints(points);
  const comparison = chartComparison(chart);
  const isGrouped = chart.type === "grouped_bar" || (chart.type === "bar" && points.some(p => p.series !== undefined));
  const trend = chart.type === "line" || chart.type === "area";
  const pie = chart.type === "pie" || chart.type === "donut";
  const displayLabel = (p: ReportChartPoint) => p.series ? `${p.label} · ${p.series}` : p.label;
  const tooltip = <Tooltip cursor={{ fill: "rgba(127,127,127,.06)" }} formatter={v => [`${y.format(Number(v))} ${y.label}`]} />;
  const axis = { axisLine: false, tickLine: false, tick: { fontSize: 11, fill: "#737d8c" } };
  const seriesComponents = grouped.series.map((name, i) => chart.type === "line"
    ? <Line key={name} type="linear" dataKey={`v${i}`} name={name} stroke={colors[i % colors.length]} strokeWidth={2} dot={{ r: 3 }} connectNulls={false} isAnimationActive={false} />
    : chart.type === "area"
      ? <Area key={name} type="linear" dataKey={`v${i}`} name={name} stroke={colors[i % colors.length]} fill={colors[i % colors.length]} fillOpacity={.12} connectNulls={false} isAnimationActive={false} />
      : <Bar key={name} dataKey={`v${i}`} name={name} fill={colors[i % colors.length]} maxBarSize={32} radius={[3, 3, 0, 0]} isAnimationActive={false} />);
  const categoryAxes = <><CartesianGrid vertical={false} strokeDasharray="3 3" stroke="#e5e7eb" />
    <XAxis {...axis} dataKey="label" minTickGap={16} />
    <YAxis {...axis} tickFormatter={y.format} width={72} />{tooltip}
    {grouped.series.length > 1 && <Legend wrapperStyle={{ fontSize: 12 }} />}</>;
  const chartElement = pie ? <PieChart><Pie data={points.map(p => ({ ...p, label: displayLabel(p) }))} dataKey="value" nameKey="label"
      outerRadius={95} innerRadius={chart.type === "donut" ? 58 : 0} isAnimationActive={false}>
      {points.map((p, i) => <Cell key={i} fill={colors[i % colors.length]} />)}
    </Pie>{tooltip}<Legend wrapperStyle={{ fontSize: 12 }} /></PieChart>
    : chart.type === "scatter" ? <ScatterChart margin={{ left: 8, right: 24, top: 12, bottom: 24 }}>
      <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
      <XAxis {...axis} type="number" dataKey="x" name={chart.x_title ?? "横轴"} unit={x.label} tickFormatter={x.format} />
      <YAxis {...axis} type="number" dataKey="value" name={chart.value_title ?? "纵轴"} unit={y.label} tickFormatter={y.format} width={80} />
      <Tooltip content={({ active, payload }) => {
        const p = payload?.[0]?.payload as ReportChartPoint | undefined;
        return active && p ? <div className="rounded-md border border-border bg-surface p-3 text-xs shadow-sm">
          <strong>{displayLabel(p)}</strong><div>{chart.x_title}: {x.format(p.x!)} {x.label}</div>
          <div>{chart.value_title}: {y.format(p.value)} {y.label}</div>
        </div> : null;
      }} /><Scatter data={points} fill={colors[0]} isAnimationActive={false} />
    </ScatterChart>
    : chart.type === "line" ? <LineChart data={grouped.rows} margin={{ right: 24, top: 12 }}>{categoryAxes}{seriesComponents}</LineChart>
    : chart.type === "area" ? <AreaChart data={grouped.rows} margin={{ right: 24, top: 12 }}>{categoryAxes}{seriesComponents}</AreaChart>
    : isGrouped ? <BarChart data={grouped.rows} margin={{ right: 24, top: 12 }}>{categoryAxes}{seriesComponents}</BarChart>
    : <BarChart data={points} layout="vertical" margin={{ left: 8, right: 24, top: 0, bottom: 0 }}>
      <XAxis {...axis} type="number" tickFormatter={y.format} />
      <YAxis {...axis} type="category" dataKey="label" width={105} />{tooltip}
      <Bar dataKey="value" name={chart.value_title ?? "数值"} maxBarSize={22} radius={[0, 3, 3, 0]} isAnimationActive={false}>
        {points.map((p, i) => <Cell key={i} fill={p.value < 0 ? "#b54848" : colors[0]} />)}
      </Bar>
    </BarChart>;
  return <figure className="mx-0 my-6 min-w-0 border-y border-border py-5 [&_table]:w-full [&_table]:border-collapse [&_th]:border-b [&_th]:border-border [&_th]:px-4 [&_th]:py-3 [&_td]:border-b [&_td]:border-border/60 [&_td]:px-4 [&_td]:py-3" aria-label={chart.title}>
    <figcaption className="mb-4 flex flex-wrap items-baseline justify-between gap-3" style={hideTitle?{justifyContent:'flex-end'}:undefined}>
      {!hideTitle && <span className="text-sm font-semibold">{chart.title}</span>}
      <span className="text-xs text-muted">{chart.type === "scatter" ? `${chart.x_title} (${x.label || "原始值"}) / ${chart.value_title} (${y.label || "原始值"})` : y.label}</span>
    </figcaption>
    <div className="w-full" style={{ height: pie || trend || isGrouped || chart.type === "scatter" ? 300 : Math.max(180, points.length * 34 + 40) }}>
      <ResponsiveContainer width="100%" height="100%">{chartElement}</ResponsiveContainer>
    </div>
    {chart.total_points !== undefined && chart.total_points > points.length && <p className="mt-2 text-xs text-muted">展示 {points.length} / {chart.total_points} 个观测值，完整结果见数据明细。</p>}
    {comparison ? <details className="mt-4 text-xs text-muted" style={{marginTop:20}}>
      <summary className="cursor-pointer">查看数值与变化</summary>
      <div style={{overflowX:"auto"}}>
      <table aria-label={`${chart.title}变化对比`} style={{width:"100%",fontVariantNumeric:"tabular-nums"}}>
        <thead><tr><th>对象</th>{comparison.periods.map(period => <th key={period} style={{textAlign:"right",whiteSpace:"nowrap"}}>{period} {y.label && `(${y.label})`}</th>)}
          <th style={{textAlign:"right",whiteSpace:"nowrap"}}>变化幅度</th>{y.label !== "%" && <th style={{textAlign:"right",whiteSpace:"nowrap"}}>增减量{y.label && ` (${y.label})`}</th>}</tr></thead>
        <tbody>{comparison.rows.map(row => <tr key={row.label}>
          <td>{row.label === "数值" ? chart.title : row.label}</td>
          <td style={{textAlign:"right",whiteSpace:"nowrap"}}>{row.before === null ? "—" : y.format(row.before*y.scale)}</td>
          <td style={{textAlign:"right",whiteSpace:"nowrap"}}>{row.after === null ? "—" : y.format(row.after*y.scale)}</td>
          <td style={{textAlign:"right"}}><ChangeValue value={row.percent} format={n => new Intl.NumberFormat("zh-CN",{maximumFractionDigits:1}).format(n)} suffix="%"/></td>
          {y.label !== "%" && <td style={{textAlign:"right"}}><ChangeValue value={row.delta === null ? null : row.delta*y.scale} format={y.format}/></td>}
        </tr>)}</tbody>
      </table>
      {comparison.rows.some(r => r.percent === null) && <p className="text-xs text-muted">缺失值不按零处理；基期为零或负值时不计算增减幅度。</p>}
    </div></details> : <details className="mt-3 text-xs text-muted"><summary className="cursor-pointer">查看图表数值</summary>
      <div className="overflow-x-auto"><table><thead><tr><th>对象</th>{chart.type === "scatter" && <th>{chart.x_title} ({x.label})</th>}<th>数值 {y.label && `(${y.label})`}</th></tr></thead>
        <tbody>{points.map((p, i) => <tr key={i}><td>{displayLabel(p)}</td>{chart.type === "scatter" && <td>{x.format(p.x!)}</td>}<td>{y.format(p.value)}</td></tr>)}</tbody></table></div>
    </details>}
  </figure>;
}
