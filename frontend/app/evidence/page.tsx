"use client";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader, useApi } from "@/components/ui";
import { api, fmt } from "@/lib/api";

export default function EvidencePage() {
  return <Suspense><Evidence /></Suspense>;
}

function Evidence() {
  const params = useSearchParams();
  const defs = useApi<any[]>("/api/cohort/definitions");
  const [q, setQ] = useState("trials requiring an EGFR exon 19 deletion after a prior EGFR TKI");
  const [mode, setMode] = useState("hybrid");
  const [res, setRes] = useState<any>(null);
  const [defId, setDefId] = useState<string>(params.get("definition") ?? "");
  const [defRes, setDefRes] = useState<any>(null);
  const [doc, setDoc] = useState<{ id: string; start: number; end: number } | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function search() {
    setBusy(true); setErr(null);
    try { setRes(await api(`/api/evidence/search?${new URLSearchParams({ q, mode, k: "6" })}`)); } catch (e) { setErr(e); } finally { setBusy(false); }
  }
  async function forDefinition(id: string) {
    setDefId(id); setDefRes(null); if (!id) return;
    try { setDefRes(await api("/api/evidence/definition", { method: "POST", json: { definition_id: id } })); } catch (e) { setErr(e); }
  }
  useEffect(() => { if (defId) forDefinition(defId); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);

  return (
    <div className="space-y-5">
      <PageHeader title="AI evidence explorer" subtitle="Hybrid retrieval (BM25 + local LSA embeddings, reciprocal-rank fusion) over trial eligibility documents and reference notes. Retrieved passages are shown verbatim; system statements are labeled as inferred." />
      <div className="rounded-md border border-surface-line bg-white px-3 py-2 text-xs text-ink-2">
        Corpus: <strong>synthetic</strong> trial eligibility documents (generated, not real trials) and <strong>authored</strong> reference notes (not peer-reviewed). Real public trials can be added with <span className="mono">scripts/fetch_ctgov.py</span>.
      </div>
      <ErrorBox error={err} />

      <Card title="Search">
        <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); search(); }}>
          <input className="input" value={q} onChange={(e) => setQ(e.target.value)} />
          <select className="input max-w-[140px]" value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="hybrid">Hybrid</option><option value="bm25">BM25 only</option><option value="dense">Dense only</option>
          </select>
          <button className="btn-primary" disabled={busy}>{busy ? "Searching…" : "Search"}</button>
        </form>
        {res && (
          <div className="mt-4 space-y-3">
            {res.insufficient_evidence && (
              <div className="rounded-md border border-[#f5dc9c] bg-[#fff8e6] px-3 py-2 text-sm text-[#6b4700]">
                <strong>! Insufficient evidence.</strong> {res.reason}. The passages below are the closest matches and should not be treated as support.
              </div>
            )}
            <div className="text-xs text-ink-3">{res.results.length} passages · {fmt.ms(res.latency_ms)} · index {res.index_version}</div>
            {res.results.map((h: any) => <Passage key={h.chunk_id} h={h} onOpen={() => setDoc({ id: h.doc_id, start: h.char_start, end: h.char_end })} dim={res.insufficient_evidence} />)}
          </div>
        )}
      </Card>

      <Card title="Evidence for a cohort definition">
        <select className="input max-w-xl" value={defId} onChange={(e) => forDefinition(e.target.value)}>
          <option value="">Select a saved definition…</option>
          {(defs.data || []).map((d) => <option key={d.definition_id} value={d.definition_id}>{d.name}</option>)}
        </select>
        {defId && !defRes && <Loading />}
        {defRes && (
          <div className="mt-4 space-y-3">
            <div className="text-xs text-ink-3">{defRes.items.length} criteria · {defRes.insufficient_count} with insufficient evidence · {fmt.ms(defRes.latency_ms)} · {defRes.version}</div>
            {defRes.items.map((it: any) => (
              <div key={it.cid} className="rounded-md border border-surface-line p-3">
                <div className="flex items-center gap-2"><span className="mono text-ink-3">{it.cid}</span><span className="font-medium text-sm">{it.label}</span><Badge value={it.role} /><Badge value={it.status} /></div>
                {it.status === "insufficient_evidence" ? (
                  <p className="mt-1 text-xs text-ink-2">None of the {it.candidates_considered} retrieved passages mention this concept. No supporting evidence is claimed.</p>
                ) : (
                  <>
                    {it.statements.map((s: any, i: number) => (
                      <p key={i} className="mt-2 text-sm"><span className="mr-2 rounded bg-surface-app px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-ink-2">Inferred</span>{s.text}
                        <span className="ml-1 text-xs text-ink-3">[{s.citations.length} citation{s.citations.length === 1 ? "" : "s"}]</span></p>))}
                    <div className="mt-2 space-y-2">{it.citations.slice(0, 3).map((h: any) => <Passage key={h.chunk_id} h={h} onOpen={() => setDoc({ id: h.doc_id, start: h.char_start, end: h.char_end })} anchors={h.anchors} />)}</div>
                  </>
                )}
              </div>
            ))}
          </div>
        )}
      </Card>
      {doc && <DocViewer {...doc} onClose={() => setDoc(null)} />}
    </div>
  );
}

function Passage({ h, onOpen, dim, anchors }: { h: any; onOpen: () => void; dim?: boolean; anchors?: string[] }) {
  return (
    <div className={`rounded-md border border-surface-line bg-white p-3 ${dim ? "opacity-70" : ""}`}>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="rounded bg-[#eef6ee] px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-[#0a6f0a]">Retrieved</span>
        <button className="link font-medium" onClick={onOpen}>{h.doc_id}</button>
        <span className="text-ink-2 truncate max-w-[520px]">{h.title}</span>
        <span className="text-ink-3">§ {h.section} · chars {h.char_start}–{h.char_end}</span>
        <span className="ml-auto text-ink-3 tabular-nums">rrf {fmt.num(h.score, 4)} · bm25 {fmt.num(h.bm25, 2)} · cos {fmt.num(h.dense, 2)}{h.term_coverage != null && ` · coverage ${fmt.pct(h.term_coverage, 0)}`}</span>
      </div>
      <p className="mt-2 text-sm whitespace-pre-line leading-relaxed">{h.passage}</p>
      {anchors && <div className="mt-1 text-[11px] text-ink-3">Anchor check passed for: {anchors.join(", ")}</div>}
    </div>
  );
}

function DocViewer({ id, start, end, onClose }: { id: string; start: number; end: number; onClose: () => void }) {
  const d = useApi<any>(`/api/evidence/documents/${id}`);
  return (
    <div className="fixed inset-0 z-20 bg-black/30 flex justify-end" onClick={onClose}>
      <div className="h-full w-full max-w-2xl bg-white shadow-xl overflow-auto p-5" onClick={(e) => e.stopPropagation()}>
        <div className="flex justify-between items-start gap-2"><div className="font-semibold">{d.data?.title ?? id}</div><button className="btn-secondary" onClick={onClose}>Close</button></div>
        {d.data && (
          <>
            <div className="mt-1 text-xs text-ink-3">{d.data.doc_type} · {d.data.source} · license {d.data.license}</div>
            <pre className="mt-4 whitespace-pre-wrap text-sm font-sans leading-relaxed">
              {d.data.body.slice(0, start)}<mark className="bg-[#fff1b8] rounded">{d.data.body.slice(start, end)}</mark>{d.data.body.slice(end)}
            </pre>
          </>
        )}
        {!d.data && <Empty title="Loading document…" />}
      </div>
    </div>
  );
}
