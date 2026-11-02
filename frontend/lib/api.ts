"use client";
// Thin API client. The bearer token lives in localStorage (prototype auth; see docs/limitations.md).

export class ApiError extends Error {
  constructor(public status: number, message: string, public requestId?: string | null) {
    super(message);
  }
}

const TOKEN_KEY = "cliniq.token";

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}
export function setToken(t: string | null) {
  try {
    if (t) localStorage.setItem(TOKEN_KEY, t);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: session-only */
  }
}

export async function api<T = any>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const headers: Record<string, string> = { ...(init.headers as Record<string, string>) };
  const tok = getToken();
  if (tok) headers["Authorization"] = `Bearer ${tok}`;
  let body = init.body;
  if (init.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(init.json);
  }
  const res = await fetch(path, { ...init, headers, body, cache: "no-store" });
  const rid = res.headers.get("X-Request-ID");
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {
      /* non-JSON error */
    }
    throw new ApiError(res.status, msg, rid);
  }
  return res.json();
}

export const fmt = {
  n: (v: unknown) => (v === null || v === undefined ? "—" : Number(v).toLocaleString()),
  pct: (v: unknown, d = 1) => (v === null || v === undefined ? "—" : `${(Number(v) * 100).toFixed(d)}%`),
  num: (v: unknown, d = 3) => (v === null || v === undefined ? "—" : Number(v).toFixed(d)),
  ms: (v: unknown) => (v === null || v === undefined ? "—" : Number(v) >= 1000 ? `${(Number(v) / 1000).toFixed(1)} s` : `${Math.round(Number(v))} ms`),
  dt: (v: unknown) => (v ? new Date(String(v)).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—"),
  short: (s: unknown, n = 8) => (s ? String(s).slice(0, n) : "—"),
};

/** Submit a long-running operation (HTTP 202 + job id) and poll until it finishes. */
export async function runJob<T = any>(path: string, init: RequestInit & { json?: unknown } = {}, onStatus?: (s: string) => void): Promise<T> {
  const job = await api<any>(path, { method: "POST", ...init });
  if (!job?.job_id) return job as T; // endpoint answered synchronously
  for (;;) {
    await new Promise((r) => setTimeout(r, 1000));
    const j = await api<any>(`/api/jobs/${job.job_id}`);
    onStatus?.(j.status);
    if (j.status === "succeeded") return j.result as T;
    if (j.status === "failed") throw new ApiError(500, `background job failed: ${j.error}`, job.job_id);
  }
}
