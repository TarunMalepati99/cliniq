"use client";
// Chart primitives following the data-viz spec: thin marks, 4px rounded data-ends, recessive grid,
// hover tooltips on every mark, legends for >=2 series, text in ink tokens (never series color).
import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { INK } from "@/lib/palette";

type Row = Record<string, any>;

function Tip({ active, payload, label, fmt }: any) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-md border border-surface-line bg-white px-3 py-2 text-xs shadow-sm">
      <div className="font-medium text-ink mb-1">{label ?? payload[0]?.payload?.label}</div>
      {payload.map((p: any) => (
        <div key={p.dataKey} className="flex items-center gap-2 text-ink-2">
          <span className="inline-block h-2 w-2 rounded-sm" style={{ background: p.color || p.payload?.fill }} />
          <span>{p.name}</span>
          <span className="ml-auto pl-3 tabular-nums text-ink">{fmt ? fmt(p.value) : Number(p.value).toLocaleString()}</span>
        </div>
      ))}
    </div>
  );
}

/** Horizontal bars; one or more series. `series` = [{key, name, color}]. */
export function HBar({ data, category, series, height, stacked = false, fmt, domain }: {
  data: Row[]; category: string; series: { key: string; name: string; color: string }[]; height?: number; stacked?: boolean;
  fmt?: (v: number) => string; domain?: [number, number];
}) {
  const h = height ?? Math.max(120, data.length * (series.length > 1 && !stacked ? 34 : 26) + 40);
  return (
    <ResponsiveContainer width="100%" height={h}>
      <BarChart data={data} layout="vertical" margin={{ left: 8, right: 24, top: 4, bottom: 4 }} barGap={2} barCategoryGap="22%">
        <CartesianGrid horizontal={false} stroke={INK.grid} />
        <XAxis type="number" tick={{ fill: INK.muted, fontSize: 11 }} axisLine={false} tickLine={false} tickFormatter={fmt} domain={domain} />
        <YAxis type="category" dataKey={category} width={210} tick={{ fill: INK.secondary, fontSize: 11 }} axisLine={false} tickLine={false} />
        <Tooltip content={<Tip fmt={fmt} />} cursor={{ fill: "rgba(37,106,191,0.06)" }} />
        {series.map((s, i) => (
          <Bar key={s.key} dataKey={s.key} name={s.name} fill={s.color} stackId={stacked ? "s" : undefined}
            radius={stacked ? (i === series.length - 1 ? [0, 4, 4, 0] : [0, 0, 0, 0]) : [0, 4, 4, 0]}
            stroke={stacked ? INK.surface : undefined} strokeWidth={stacked ? 2 : 0} maxBarSize={18} isAnimationActive={false} />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Vertical bars for a single series, optionally per-bar colors. */
export function VBar({ data, x, y, name, color, colors, height = 220, fmt }: {
  data: Row[]; x: string; y: string; name: string; color?: string; colors?: string[]; height?: number; fmt?: (v: number) => string;
}) {
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={data} margin={{ left: 0, right: 8, top: 8, bottom: 4 }}>
        <CartesianGrid vertical={false} stroke={INK.grid} />
        <XAxis dataKey={x} tick={{ fill: INK.secondary, fontSize: 11 }} axisLine={{ stroke: INK.grid }} tickLine={false} interval={0} />
        <YAxis tick={{ fill: INK.muted, fontSize: 11 }} axisLine={false} tickLine={false} tickFormatter={fmt} width={44} />
        <Tooltip content={<Tip fmt={fmt} />} cursor={{ fill: "rgba(37,106,191,0.06)" }} />
        <Bar dataKey={y} name={name} fill={color ?? "#2a78d6"} radius={[4, 4, 0, 0]} maxBarSize={36} isAnimationActive={false}>
          {colors && data.map((_, i) => <Cell key={i} fill={colors[i % colors.length]} />)}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Grouped vertical bars for comparing versions (e.g. baseline vs improved). */
export function GroupedBar({ data, x, series, height = 240, fmt, domain }: {
  data: Row[]; x: string; series: { key: string; name: string; color: string }[]; height?: number; fmt?: (v: number) => string; domain?: [number, number];
}) {
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={data} margin={{ left: 0, right: 8, top: 8, bottom: 4 }} barGap={2} barCategoryGap="28%">
        <CartesianGrid vertical={false} stroke={INK.grid} />
        <XAxis dataKey={x} tick={{ fill: INK.secondary, fontSize: 11 }} axisLine={{ stroke: INK.grid }} tickLine={false} interval={0} />
        <YAxis tick={{ fill: INK.muted, fontSize: 11 }} axisLine={false} tickLine={false} tickFormatter={fmt} domain={domain} width={44} />
        <Tooltip content={<Tip fmt={fmt} />} cursor={{ fill: "rgba(37,106,191,0.06)" }} />
        {series.map((s) => (
          <Bar key={s.key} dataKey={s.key} name={s.name} fill={s.color} radius={[4, 4, 0, 0]} maxBarSize={28} isAnimationActive={false} />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}
