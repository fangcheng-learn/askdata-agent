"use client";

import React from "react";
import { Bar, BarChart, Cell, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

type Chart = { type: "bar" | "pie"; title: string; unit?: string; currency?: string; precision?: number;
  source: string; points: { label: string; value: number }[] };

/** Decode the server projection of an Agent-selected, source-bound chart. */
export function decodeReportChart(encoded: string): Chart | null {
  try {
    if (encoded.length > 100_000) return null;
    const value = JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(encoded), c => c.charCodeAt(0)))) as Chart;
    if (!["bar", "pie"].includes(value.type) || typeof value.title !== "string" || typeof value.source !== "string"
      || !Array.isArray(value.points) || !value.points.length || value.points.length > 30
      || !value.points.every(p => typeof p.label === "string" && typeof p.value === "number" && Number.isFinite(p.value))) return null;
    return value;
  } catch { return null; }
}

export function ReportChart({ encoded }: { encoded: string }) {
  const chart = decodeReportChart(encoded);
  if (!chart) return null;
  // Display conversion follows the source unit, never a field-name guess.
  const cents = /^([A-Z]{3}) cents(\/.*)?$/u.exec(chart.unit ?? "");
  const ratio = chart.unit === "ratio";
  const scale = ratio ? 100 : cents ? .01 : 1;
  const unit = ratio ? "%" : cents ? `${cents[1]}${cents[2] ?? ""}` : chart.unit ?? "";
  const points = chart.points.map(p => ({ ...p, value: p.value * scale }));
  const format = (value: number) => new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: ratio || cents ? 2 : Math.max(0, Math.min(chart.precision ?? 2, 6)),
  }).format(value);
  const colors = ["#343b46", "#737d8c", "#a7afba", "#ced3da", "#576474", "#8896a7"];
  return <figure className="my-6 border-y border-border py-5" aria-label={chart.title}>
    <figcaption className="mb-4 flex items-baseline justify-between gap-3">
      <span className="text-sm font-medium">{chart.title}</span>
      <span className="shrink-0 text-xs text-muted">{unit}</span>
    </figcaption>
    <div className="w-full" style={{ height: chart.type === "pie" ? 240 : Math.max(160, points.length * 38 + 40) }}>
      <ResponsiveContainer width="100%" height="100%">
        {chart.type === "pie" ? <PieChart><Pie data={points} dataKey="value" nameKey="label" outerRadius={85} label>
          {points.map((p, i) => <Cell key={`${p.label}-${i}`} fill={colors[i % colors.length]} />)}
        </Pie><Tooltip formatter={(v) => `${format(Number(v))} ${unit}`} /></PieChart>
          : <BarChart data={points} layout="vertical" margin={{ left: 8, right: 20, top: 0, bottom: 0 }}>
            <XAxis type="number" tickFormatter={format} axisLine={false} tickLine={false} tick={{ fontSize: 11, fill: "#737d8c" }} />
            <YAxis type="category" dataKey="label" width={105} axisLine={false} tickLine={false} tick={{ fontSize: 12, fill: "#576474" }} />
            <Tooltip cursor={{ fill: "rgba(127,127,127,.06)" }} formatter={(v) => [`${format(Number(v))} ${unit}`, "数值"]} />
            <Bar dataKey="value" maxBarSize={22} radius={[0, 3, 3, 0]} isAnimationActive={false}>
              {points.map((p, i) => <Cell key={`${p.label}-${i}`} fill={p.value < 0 ? "#b54848" : colors[0]} />)}
            </Bar>
          </BarChart>}
      </ResponsiveContainer>
    </div>
    <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted">
      {points.length <= 6 && points.map((p, i) => <span key={`${p.label}-${i}`}>{p.label} <strong className="font-medium tabular-nums text-foreground">{format(p.value)}{ratio ? "%" : ""}</strong></span>)}
    </div>
  </figure>;
}
