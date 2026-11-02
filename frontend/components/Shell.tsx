"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ReactNode, useEffect, useState } from "react";
import { api, getToken, setToken } from "@/lib/api";

const NAV = [
  { href: "/", label: "Overview" },
  { href: "/data", label: "Clinical data" },
  { href: "/pipeline", label: "Pipeline" },
  { href: "/cohort", label: "Cohort builder" },
  { href: "/cohort/runs", label: "Cohort results" },
  { href: "/evidence", label: "Evidence" },
  { href: "/evaluation", label: "Evaluation" },
  { href: "/quality", label: "Quality & lineage" },
];

const DEV_USERS = [
  { token: "dev-admin-token", label: "Admin", desc: "Ingest data, run pipelines and benchmarks" },
  { token: "dev-researcher-token", label: "Researcher", desc: "Build, review and execute cohorts" },
  { token: "dev-reviewer-token", label: "Reviewer", desc: "Second researcher, for four-eyes approval" },
  { token: "dev-viewer-token", label: "Viewer", desc: "Read-only access" },
];

type Me = { user_id: string; role: string; disclaimer: string };

export function Shell({ children }: { children: ReactNode }) {
  const path = usePathname();
  const [me, setMe] = useState<Me | null>(null);
  const [checked, setChecked] = useState(false);
  const [custom, setCustom] = useState("");
  const [err, setErr] = useState<string | null>(null);

  async function login(tok: string | null) {
    setToken(tok);
    setErr(null);
    if (!tok) {
      setMe(null);
      setChecked(true);
      return;
    }
    try {
      setMe(await api<Me>("/api/me"));
    } catch (e: any) {
      setToken(null);
      setMe(null);
      setErr(e.message);
    }
    setChecked(true);
  }
  useEffect(() => {
    login(getToken());
  }, []);

  if (!checked) return null;
  if (!me)
    return (
      <div className="min-h-screen grid place-items-center p-6">
        <div className="card w-full max-w-md p-6">
          <div className="text-lg font-semibold">CLINIQ</div>
          <div className="text-sm text-ink-2">Clinical Intelligence &amp; Cohort Feasibility Platform</div>
          <p className="mt-4 rounded-md bg-[#fff6e0] border border-[#f5dc9c] px-3 py-2 text-xs text-[#6b4700]">
            Research and engineering prototype on synthetic data. Not a clinically validated product; not for medical decision-making.
          </p>
          <div className="mt-5 label">Sign in with a development role</div>
          <div className="mt-2 grid gap-2">
            {DEV_USERS.map((u) => (
              <button key={u.token} className="btn-secondary justify-between text-left" onClick={() => login(u.token)}>
                <span className="font-medium">{u.label}</span>
                <span className="text-xs text-ink-3">{u.desc}</span>
              </button>
            ))}
          </div>
          <div className="mt-4 label">Or paste an API token</div>
          <form className="mt-1 flex gap-2" onSubmit={(e) => { e.preventDefault(); login(custom.trim()); }}>
            <input className="input" type="password" value={custom} onChange={(e) => setCustom(e.target.value)} placeholder="Bearer token" />
            <button className="btn-primary">Sign in</button>
          </form>
          {err && <div className="mt-2 text-xs text-[#a12d2d]">{err}</div>}
        </div>
      </div>
    );

  return (
    <div className="min-h-screen flex">
      <aside className="w-56 shrink-0 border-r border-surface-line bg-surface flex flex-col">
        <div className="px-4 py-4 border-b border-surface-line">
          <div className="font-semibold tracking-tight">CLINIQ</div>
          <div className="text-[11px] text-ink-3 leading-tight">Clinical intelligence &amp; cohort feasibility</div>
        </div>
        <nav className="flex-1 p-2 space-y-0.5">
          {NAV.map((n) => {
            const active = n.href === "/" ? path === "/" : path === n.href || (path.startsWith(n.href + "/") && !(n.href === "/cohort" && path.startsWith("/cohort/runs")));
            return (
              <Link key={n.href} href={n.href}
                className={`block rounded-md px-3 py-1.5 text-sm ${active ? "bg-brand-soft text-brand font-medium" : "text-ink-2 hover:bg-surface-app hover:text-ink"}`}>
                {n.label}
              </Link>
            );
          })}
        </nav>
        <div className="p-3 border-t border-surface-line text-xs">
          <div className="truncate text-ink-2">{me.user_id}</div>
          <div className="flex items-center justify-between mt-1">
            <span className="rounded bg-surface-app px-1.5 py-0.5 font-medium text-ink-2">{me.role}</span>
            <button className="link text-xs" onClick={() => login(null)}>Sign out</button>
          </div>
        </div>
      </aside>
      <div className="flex-1 min-w-0">
        <div className="border-b border-[#f5dc9c] bg-[#fff8e6] px-6 py-1.5 text-xs text-[#6b4700]">
          <strong>Research prototype · synthetic data only.</strong> Not clinically validated; not for medical decision-making or patient care.
        </div>
        <main className="px-6 py-6 max-w-[1400px]">{children}</main>
      </div>
    </div>
  );
}

export function useRole(): string | null {
  const [role, setRole] = useState<string | null>(null);
  useEffect(() => {
    api<Me>("/api/me").then((m) => setRole(m.role)).catch(() => setRole(null));
  }, []);
  return role;
}
