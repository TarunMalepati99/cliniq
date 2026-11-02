"use client";
import { ReactNode, useEffect, useState, useCallback } from "react";
import { api, ApiError } from "@/lib/api";

export function Card({ title, actions, children, className = "" }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <div className="card-h">
          <h2>{title}</h2>
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </div>
      )}
      <div className={title || actions ? "card-b" : "p-4"}>{children}</div>
    </section>
  );
}

export function Stat({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: string }) {
  return (
    <div className="card p-4">
      <div className="label">{label}</div>
      <div className="mt-1 text-2xl font-semibold tracking-tight flex items-center gap-2">
        {tone && <span aria-hidden className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: tone }} />}
        {value}
      </div>
      {sub && <div className="mt-1 text-xs text-ink-3">{sub}</div>}
    </div>
  );
}

const BADGE: Record<string, string> = {
  succeeded: "bg-[#e8f6e8] text-[#0a6f0a] border-[#bfe5bf]", approved: "bg-[#e8f6e8] text-[#0a6f0a] border-[#bfe5bf]",
  supported: "bg-[#e8f6e8] text-[#0a6f0a] border-[#bfe5bf]", eligible: "bg-[#e8f6e8] text-[#0a6f0a] border-[#bfe5bf]",
  met: "bg-[#e8f6e8] text-[#0a6f0a] border-[#bfe5bf]", positive: "bg-[#e8f6e8] text-[#0a6f0a] border-[#bfe5bf]",
  partial: "bg-[#fff6e0] text-[#8a5a00] border-[#f5dc9c]", draft: "bg-[#fff6e0] text-[#8a5a00] border-[#f5dc9c]",
  potentially_eligible: "bg-[#fff6e0] text-[#8a5a00] border-[#f5dc9c]", unknown: "bg-[#f1f1ef] text-[#52514e] border-[#dcdbd6]",
  insufficient_evidence: "bg-[#fff6e0] text-[#8a5a00] border-[#f5dc9c]", absent: "bg-[#fdeee6] text-[#9a4320] border-[#f2c9b6]",
  failed: "bg-[#fdf0f0] text-[#a12d2d] border-[#efc4c4]", rejected: "bg-[#fdf0f0] text-[#a12d2d] border-[#efc4c4]",
  not_met: "bg-[#fdf0f0] text-[#a12d2d] border-[#efc4c4]", negative: "bg-[#fdf0f0] text-[#a12d2d] border-[#efc4c4]",
  open: "bg-[#fdf0f0] text-[#a12d2d] border-[#efc4c4]", retry_failed: "bg-[#fdf0f0] text-[#a12d2d] border-[#efc4c4]",
  resolved: "bg-[#e8f6e8] text-[#0a6f0a] border-[#bfe5bf]", running: "bg-brand-soft text-brand border-[#c4dbf6]",
  ineligible: "bg-[#f1f1ef] text-[#52514e] border-[#dcdbd6]", inclusion: "bg-brand-soft text-brand border-[#c4dbf6]",
  exclusion: "bg-[#fdeee6] text-[#9a4320] border-[#f2c9b6]",
};
const ICON: Record<string, string> = { succeeded: "✓", approved: "✓", supported: "✓", eligible: "✓", met: "✓", positive: "✓", resolved: "✓",
  failed: "✕", rejected: "✕", not_met: "✕", negative: "✕", ineligible: "✕", partial: "!", draft: "•", unknown: "?", absent: "∅",
  insufficient_evidence: "!", potentially_eligible: "?", open: "!", retry_failed: "✕" };

export function Badge({ value, children }: { value: string; children?: ReactNode }) {
  return (
    <span className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] font-medium whitespace-nowrap ${BADGE[value] ?? "bg-surface-app text-ink-2 border-surface-line"}`}>
      {ICON[value] && <span aria-hidden>{ICON[value]}</span>}
      {children ?? value.replaceAll("_", " ")}
    </span>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-md border border-dashed border-surface-line px-4 py-8 text-center">
      <div className="text-sm font-medium text-ink-2">{title}</div>
      {children && <div className="mt-1 text-xs text-ink-3">{children}</div>}
    </div>
  );
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  const e = error as ApiError;
  return (
    <div role="alert" className="rounded-md border border-[#efc4c4] bg-[#fdf0f0] px-3 py-2 text-sm text-[#7d2222]">
      <strong>Request failed{e.status ? ` (${e.status})` : ""}:</strong> {e.message}
      {e.requestId && <span className="ml-2 mono text-[#a12d2d]">request {e.requestId.slice(0, 8)}</span>}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return <div className="py-6 text-center text-sm text-ink-3">{label}</div>;
}

/** Fetch helper hook: returns data, error, loading and a reload function. */
export function useApi<T = any>(path: string | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const load = useCallback(async () => {
    if (!path) return;
    setLoading(true);
    setError(null);
    try {
      setData(await api<T>(path));
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, ...deps]);
  useEffect(() => {
    load();
  }, [load]);
  return { data, error, loading, reload: load, setData };
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="mt-0.5 text-sm text-ink-2 max-w-3xl">{subtitle}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Legend({ items }: { items: { label: string; color: string }[] }) {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-2">
      {items.map((i) => (
        <span key={i.label} className="inline-flex items-center gap-1.5">
          <span aria-hidden className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: i.color }} />
          {i.label}
        </span>
      ))}
    </div>
  );
}

export function JsonView({ value, maxH = "max-h-72" }: { value: unknown; maxH?: string }) {
  return <pre className={`mono overflow-auto rounded-md bg-[#f6f6f4] border border-surface-line p-3 ${maxH}`}>{JSON.stringify(value, null, 2)}</pre>;
}

/** Proportional horizontal bar (with text value) used inside tables. */
export function Meter({ value, color = "#2a78d6", label }: { value: number; color?: string; label?: string }) {
  const v = Math.max(0, Math.min(1, value || 0));
  return (
    <div className="flex items-center gap-2 min-w-[140px]">
      <div className="h-2 flex-1 rounded-full bg-surface-app overflow-hidden">
        <div className="h-full rounded-full" style={{ width: `${v * 100}%`, background: color }} />
      </div>
      <span className="text-xs tabular-nums text-ink-2 w-12 text-right">{label ?? `${(v * 100).toFixed(1)}%`}</span>
    </div>
  );
}
