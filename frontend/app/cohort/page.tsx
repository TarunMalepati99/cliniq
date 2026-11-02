"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { Badge, Card, Empty, ErrorBox, Legend, PageHeader, useApi } from "@/components/ui";
import { api, fmt } from "@/lib/api";

const EXAMPLES = [
  "Find patients aged 40–70 diagnosed with non-small-cell lung cancer, with an EGFR exon 19 deletion, and a documented history of prior treatment.",
  "KRAS G12C NSCLC previously treated with platinum-based chemotherapy, ECOG 0-1, hemoglobin at least 90 g/L, excluding brain metastases.",
  "Women over 50 with HER2-positive breast cancer who have not received trastuzumab, excluding anyone with heart failure.",
  "Treatment-naive colorectal cancer with MSI-H status in Indiana or Ohio, life expectancy > 3 months.",
];

export default function CohortBuilderPage() {
  return <Suspense><CohortBuilder /></Suspense>;
}

function CohortBuilder() {
  const router = useRouter();
  const params = useSearchParams();
  const vocab = useApi<any>("/api/vocabulary");
  const defs = useApi<any[]>("/api/cohort/definitions");
  const [text, setText] = useState(EXAMPLES[0]);
  const [extractor, setExtractor] = useState("rules-v2");
  const [ext, setExt] = useState<any>(null);
  const [json, setJson] = useState("");
  const [jsonErr, setJsonErr] = useState<string | null>(null);
  const [draft, setDraft] = useState<any>(null);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<unknown>(null);

  useEffect(() => {
    const id = params.get("id");
    if (id) api(`/api/cohort/definitions/${id}`).then((d) => { setDraft(d); setText(d.nl_request ?? ""); setExt(d.extraction); }).catch(setErr);
  }, [params]);

  async function run<T>(label: string, fn: () => Promise<T>): Promise<T | undefined> {
    setBusy(label); setErr(null);
    try { return await fn(); } catch (e) { setErr(e); } finally { setBusy(null); }
  }
  async function extract() {
    setDraft(null);
    const r = await run("extract", () => api("/api/cohort/extract", { method: "POST", json: { text, extractor } }));
    if (r) { setExt(r); setJson(JSON.stringify(r.definition ?? { name: "Untitled cohort", inclusion: [], exclusion: [] }, null, 2)); setJsonErr(null); }
  }
  async function createDraft() {
    let definition;
    try { definition = JSON.parse(json); setJsonErr(null); } catch (e: any) { setJsonErr(`Invalid JSON: ${e.message}`); return; }
    const d = await run("create", () => api("/api/cohort/definitions", { method: "POST", json: { definition, nl_request: text, extraction: ext, extractor_version: ext?.version } }));
    if (d) { setDraft(d); defs.reload(); }
  }
  async function review(approve: boolean) {
    const d = await run(approve ? "approve" : "reject", () => api(`/api/cohort/definitions/${draft.definition_id}/review`, { method: "POST", json: { sql_sha256: draft.sql_sha256, approve, comment: comment || null } }));
    if (d) { setDraft(d); defs.reload(); }
  }
  async function execute() {
    const r: any = await run("execute", () => api(`/api/cohort/definitions/${draft.definition_id}/execute`, { method: "POST" }));
    if (r) router.push(`/cohort/runs/${r.cohort_run_id}`);
  }

  return (
    <div className="space-y-5">
      <PageHeader title="Cohort builder" subtitle="Natural-language request → structured, allowlisted criteria → deterministic SQL → human approval → read-only execution. The language model (if enabled) never writes or runs SQL." />
      <ErrorBox error={err} />

      <Card title={<span><Step n={1} /> Research request</span>}>
        <textarea className="input min-h-[84px]" value={text} onChange={(e) => setText(e.target.value)} />
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <select className="input max-w-[220px]" value={extractor} onChange={(e) => setExtractor(e.target.value)}>
            {(vocab.data?.extractors ?? ["rules-v2", "rules-v1"]).map((x: string) => <option key={x} value={x}>{x === "rules-v2" ? "rules-v2 (default)" : x === "rules-v1" ? "rules-v1 (baseline)" : x === "gemini" ? "Gemini" : "LLM (Anthropic)"}</option>)}
          </select>
          <button className="btn-primary" onClick={extract} disabled={!!busy || text.trim().length < 3}>{busy === "extract" ? "Extracting…" : "Extract criteria"}</button>
          <span className="text-xs text-ink-3 ml-2">Examples:</span>
          {EXAMPLES.map((e, i) => <button key={i} className="link text-xs" onClick={() => setText(e)}>#{i + 1}</button>)}
        </div>
      </Card>

      {ext && !draft && (
        <div className="grid xl:grid-cols-2 gap-4">
          <Card title={<span><Step n={2} /> Extracted concepts</span>} actions={<span className="text-xs text-ink-3">{ext.version} · {fmt.ms(ext.latency_ms)}</span>}>
            <Legend items={[{ label: "Inclusion", color: "#256abf" }, { label: "Exclusion", color: "#c2552a" }]} />
            <p className="mt-2 text-sm leading-7"><Highlighted text={ext.normalized_text} mentions={ext.mentions || []} /></p>
            {ext.unsupported?.length > 0 && (
              <div className="mt-3 rounded-md border border-[#f5dc9c] bg-[#fff8e6] p-3">
                <div className="text-sm font-medium text-[#6b4700]">! Not representable — needs manual review ({ext.unsupported.length})</div>
                <ul className="mt-1 text-xs text-[#6b4700] space-y-0.5">{ext.unsupported.map((u: any, i: number) => <li key={i}><span className="mono">“{u.text}”</span> — {u.reason}</li>)}</ul>
              </div>
            )}
            {!ext.valid && <div className="mt-3 text-sm text-[#a12d2d]">Extraction did not produce a valid definition: {ext.errors?.join("; ")}</div>}
            <div className="mt-3"><div className="label">Required data fields</div>
              <div className="mt-1 flex flex-wrap gap-1">{(ext.required_fields || []).map((f: string) => <span key={f} className="mono rounded bg-surface-app px-1.5 py-0.5">{f}</span>)}</div></div>
          </Card>
          <Card title={<span><Step n={3} /> Structured representation (editable)</span>}
            actions={<button className="btn-primary" onClick={createDraft} disabled={!!busy}>{busy === "create" ? "Compiling…" : "Validate & compile"}</button>}>
            <p className="text-xs text-ink-3 mb-2">Edit if needed. Only allowlisted keys validate: conditions, biomarkers, labs, drug classes, procedures and US states listed in the vocabulary.</p>
            <textarea className="input mono min-h-[340px]" value={json} onChange={(e) => setJson(e.target.value)} spellCheck={false} />
            {jsonErr && <div className="mt-1 text-xs text-[#a12d2d]">{jsonErr}</div>}
          </Card>
        </div>
      )}

      {draft && (
        <div className="grid xl:grid-cols-5 gap-4">
          <Card title={<span><Step n={4} /> Review compiled query</span>} className="xl:col-span-3" actions={<Badge value={draft.status} />}>
            <div className="text-sm font-medium">{draft.name}</div>
            <div className="mt-3 grid md:grid-cols-2 gap-3">
              {(["inclusion", "exclusion"] as const).map((role) => (
                <div key={role}>
                  <div className="label mb-1">{role === "inclusion" ? "Inclusion criteria" : "Exclusion criteria"}</div>
                  <ul className="space-y-1">{draft.criteria.filter((c: any) => c.role === role).map((c: any) => (
                    <li key={c.cid} className="text-sm flex gap-2"><span className="mono text-ink-3 w-6">{c.cid}</span>{c.label}</li>))}
                    {!draft.criteria.some((c: any) => c.role === role) && <li className="text-xs text-ink-3">None</li>}</ul>
                </div>
              ))}
            </div>
            <div className="mt-4"><div className="label mb-1">Validation checks</div>
              <div className="flex flex-wrap gap-1.5">{draft.validation.checks.map((c: any) => <Badge key={c.check} value={c.passed ? "succeeded" : "failed"}>{c.check.replaceAll("_", " ")}</Badge>)}</div></div>
            <details className="mt-4"><summary className="cursor-pointer text-sm text-brand">Compiled SQL ({draft.compiler_version}) · parameters are bound separately</summary>
              <pre className="mono mt-2 max-h-96 overflow-auto rounded-md bg-[#f6f6f4] border border-surface-line p-3">{draft.compiled_sql}</pre>
              <pre className="mono mt-2 max-h-40 overflow-auto rounded-md bg-[#f6f6f4] border border-surface-line p-3">{JSON.stringify(draft.compiled_params, null, 1)}</pre></details>
            <div className="mt-3 text-xs text-ink-3">SQL SHA-256 <span className="mono">{draft.sql_sha256}</span> · definition SHA-256 <span className="mono">{fmt.short(draft.definition_sha256, 16)}</span> · author {draft.created_by}</div>
          </Card>
          <Card title={<span><Step n={5} /> Approve &amp; execute</span>} className="xl:col-span-2">
            {draft.status === "draft" && (
              <>
                <p className="text-sm text-ink-2">Approval binds to the exact SQL hash shown. Execution runs as a read-only, SELECT-only database role with a statement timeout.</p>
                <textarea className="input mt-3" placeholder="Review comment (optional)" value={comment} onChange={(e) => setComment(e.target.value)} />
                <div className="mt-3 flex gap-2">
                  <button className="btn-primary" disabled={!!busy || !draft.validation.ok} onClick={() => review(true)}>{busy === "approve" ? "Approving…" : "Approve query"}</button>
                  <button className="btn-danger" disabled={!!busy} onClick={() => review(false)}>Reject</button>
                </div>
              </>
            )}
            {draft.status === "approved" && (
              <>
                <p className="text-sm text-ink-2">Approved by {draft.approvals.at(-1)?.decided_by} · {fmt.dt(draft.approvals.at(-1)?.decided_at)}</p>
                <button className="btn-primary mt-3" disabled={!!busy} onClick={execute}>{busy === "execute" ? "Executing…" : "Execute query"}</button>
              </>
            )}
            {draft.status === "rejected" && <p className="text-sm text-ink-2">Rejected. Create a new definition to try again.</p>}
            <div className="mt-4"><div className="label mb-1">Approval history</div>
              {!draft.approvals.length ? <div className="text-xs text-ink-3">No decisions yet.</div> : draft.approvals.map((a: any) => (
                <div key={a.approval_id} className="text-xs text-ink-2"><Badge value={a.decision} /> {a.decided_by} · {fmt.dt(a.decided_at)} {a.comment && `— “${a.comment}”`}</div>))}
            </div>
            <button className="link text-xs mt-4" onClick={() => { setDraft(null); router.replace("/cohort"); }}>Start a new request</button>
          </Card>
        </div>
      )}

      <Card title="Saved cohort definitions">
        {!defs.data?.length ? <Empty title="No definitions yet" /> : (
          <table className="tbl"><thead><tr><th>Name</th><th>Status</th><th>Author</th><th>Created</th><th>Extractor</th><th className="text-right">Runs</th></tr></thead>
            <tbody>{defs.data.slice(0, 25).map((d) => (
              <tr key={d.definition_id}><td className="max-w-[360px] truncate"><Link className="link" href={`/cohort?id=${d.definition_id}`}>{d.name}</Link></td>
                <td><Badge value={d.status} /></td><td className="text-xs">{d.created_by}</td><td className="text-xs text-ink-2">{fmt.dt(d.created_at)}</td>
                <td className="mono">{d.extractor_version ?? "manual"}</td><td className="text-right">{d.runs}</td></tr>))}</tbody></table>
        )}
      </Card>
    </div>
  );
}

function Step({ n }: { n: number }) {
  return <span className="mr-2 inline-grid h-5 w-5 place-items-center rounded-full bg-brand text-white text-[11px]">{n}</span>;
}

function Highlighted({ text, mentions }: { text: string; mentions: any[] }) {
  const out: React.ReactNode[] = [];
  let pos = 0;
  [...mentions].sort((a, b) => a.start - b.start).forEach((m, i) => {
    if (m.start < pos) return;
    out.push(text.slice(pos, m.start));
    const excl = m.polarity === "exclusion";
    out.push(
      <mark key={i} title={`${m.polarity}: ${JSON.stringify(m.criterion)}`}
        className={`rounded px-0.5 text-ink border-b-2 ${excl ? "bg-[#fdeee6] border-[#c2552a]" : "bg-brand-soft border-brand"}`}>
        {text.slice(m.start, m.end)}<sup className="ml-0.5 text-[9px] text-ink-3">{excl ? "excl" : m.kind}</sup>
      </mark>);
    pos = m.end;
  });
  out.push(text.slice(pos));
  return <>{out}</>;
}
