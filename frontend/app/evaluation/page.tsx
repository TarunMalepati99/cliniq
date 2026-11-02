"use client";
import { useState } from "react";
import { GroupedBar } from "@/components/charts";
import { useRole } from "@/components/Shell";
import { Badge, Card, Empty, ErrorBox, Legend, Loading, PageHeader, Stat, useApi } from "@/components/ui";
import { fmt, runJob } from "@/lib/api";
import { SERIES } from "@/lib/palette";

const SHORT: Record<string, string> = { "Concept F1": "Concept F1", "Criterion F1": "Crit. F1", "Exact match": "Exact", "Exact (hand-written)": "Exact (HW)",
  "Polarity acc.": "Polarity", "Cohort precision": "Cohort P", "Cohort recall": "Cohort R" };

export default function Evaluation() {
  const role = useRole();
  const runs = useApi<any[]>("/api/eval/runs");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const [failRun, setFailRun] = useState<string | null>(null);

  async function runNow() {
    setBusy(true); setErr(null);
    try { await runJob("/api/eval/run", { json: {} }); runs.reload(); } catch (e) { setErr(e); } finally { setBusy(false); }
  }
  if (runs.error) return <ErrorBox error={runs.error} />;
  if (!runs.data) return <Loading />;
  const latest = (suite: string, ver?: string) => runs.data!.find((r) => r.suite === suite && (!ver || r.system_version === ver));
  const v1 = latest("extraction", "rules-v1");
  const v2 = latest("extraction", "rules-v2");
  const llm = runs.data.find((r) => r.suite === "extraction" && r.system_version.startsWith("llm"));
  const ret = latest("retrieval");
  const evd = latest("evidence");

  const cmp = v1 && v2 ? [
    ["Concept F1", v1.metrics.concept.f1, v2.metrics.concept.f1], ["Criterion F1", v1.metrics.criterion.f1, v2.metrics.criterion.f1],
    ["Exact match", v1.metrics.exact_match, v2.metrics.exact_match], ["Exact (hand-written)", v1.metrics.by_subset.handwritten.exact_match, v2.metrics.by_subset.handwritten.exact_match],
    ["Polarity acc.", v1.metrics.polarity_accuracy, v2.metrics.polarity_accuracy], ["Cohort precision", v1.metrics.cohort_precision, v2.metrics.cohort_precision],
    ["Cohort recall", v1.metrics.cohort_recall, v2.metrics.cohort_recall],
  ].map(([m, a, b]) => ({ metric: m, v1: a, v2: b, short: SHORT[m as string] ?? m })) : [];

  return (
    <div className="space-y-5">
      <PageHeader title="Model evaluation" subtitle="Independent benchmark of criteria extraction, cohort correctness, retrieval and citation quality. Every number comes from a recorded benchmark run."
        actions={role === "admin" && <button className="btn-primary" onClick={runNow} disabled={busy}>{busy ? "Running benchmark… (~30 s)" : "Run benchmark now"}</button>} />
      <ErrorBox error={err} />
      {!v2 ? <Card><Empty title="No benchmark runs yet">An admin can run the benchmark above, or use <span className="mono">cliniq eval</span>.</Empty></Card> : (
        <>
          <div className="rounded-md border border-[#f5dc9c] bg-[#fff8e6] px-3 py-2 text-xs text-[#6b4700]">
            <strong>Read with care:</strong> the templated subset ({v2.metrics.by_subset.templated?.n} items) shares vocabulary with the rule lexicon, so its score is optimistic. The hand-written subset ({v2.metrics.by_subset.handwritten?.n} items) is the better estimate. Gold labels are author-reviewed, not independently clinically reviewed.
            {!llm && " The LLM extractor has not been benchmarked in this environment (no API key)."}
          </div>
          <div className="grid grid-cols-2 lg:grid-cols-6 gap-3">
            <Stat label="Benchmark requests" value={v2.n_items} sub={`benchmark ${fmt.short(v2.benchmark_sha256, 10)}`} />
            <Stat label="Exact match · rules-v2" value={fmt.pct(v2.metrics.exact_match)} sub={`hand-written ${fmt.pct(v2.metrics.by_subset.handwritten.exact_match)} · templated ${fmt.pct(v2.metrics.by_subset.templated.exact_match)}`} />
            <Stat label="Structured validity" value={fmt.pct(v2.metrics.structured_validity)} sub={`v1 ${fmt.pct(v1?.metrics.structured_validity)}`} />
            <Stat label="SQL execution success" value={fmt.pct(v2.metrics.sql_success_rate)} sub="compile + guard + execute" />
            <Stat label="Hallucination rate" value={fmt.pct(v2.metrics.hallucination_rate)} sub="criteria with no anchor in request" />
            <Stat label="Latency p50 / p95" value={`${fmt.num(v2.metrics.latency_ms.p50, 1)} / ${fmt.num(v2.metrics.latency_ms.p95, 1)} ms`} sub={`cost $${fmt.num(v2.metrics.cost_usd_total, 2)} (local rules)`} />
          </div>
          <div className="grid xl:grid-cols-2 gap-4">
            <Card title="Baseline vs improved extractor" actions={<Legend items={[{ label: "rules-v1 (baseline)", color: SERIES[1] }, { label: "rules-v2", color: SERIES[0] }]} />}>
              <GroupedBar data={cmp} x="short" series={[{ key: "v1", name: "rules-v1", color: SERIES[1] }, { key: "v2", name: "rules-v2", color: SERIES[0] }]} fmt={(v) => `${Math.round(v * 100)}%`} domain={[0, 1]} height={260} />
              <table className="tbl mt-2"><thead><tr><th>Metric</th><th className="text-right">rules-v1</th><th className="text-right">rules-v2</th></tr></thead>
                <tbody>{cmp.map((c) => <tr key={c.metric as string}><td>{c.metric}</td><td className="text-right tabular-nums">{fmt.pct(c.v1)}</td><td className="text-right tabular-nums">{fmt.pct(c.v2)}</td></tr>)}</tbody></table>
            </Card>
            <Card title="Failure categories (rules-v2, per item)">
              <FailureTable runs={[v1, v2].filter(Boolean)} />
              <button className="link text-sm mt-3" onClick={() => setFailRun(failRun ? null : v2.eval_run_id)}>{failRun ? "Hide" : "Show"} failing items</button>
            </Card>
          </div>
          {failRun && <FailingItems id={failRun} />}
          <div className="grid xl:grid-cols-2 gap-4">
            <Card title="Evidence retrieval (doc-level, answerable queries)" actions={<Legend items={[{ label: "BM25", color: SERIES[1] }, { label: "Dense (LSA)", color: SERIES[2] }, { label: "Hybrid", color: SERIES[0] }]} />}>
              {!ret ? <Empty title="No retrieval run" /> : (
                <>
                  <GroupedBar data={["recall@1", "recall@3", "recall@5", "recall@10", "mrr"].map((k) => ({ metric: k, bm25: ret.metrics.bm25[k], dense: ret.metrics.dense[k], hybrid: ret.metrics.hybrid[k] }))} x="metric"
                    series={[{ key: "bm25", name: "BM25", color: SERIES[1] }, { key: "dense", name: "Dense", color: SERIES[2] }, { key: "hybrid", name: "Hybrid", color: SERIES[0] }]} fmt={(v) => v.toFixed(2)} domain={[0, 1]} />
                  <p className="text-xs text-ink-3 mt-1">{ret.metrics.n_answerable} answerable + {ret.metrics.n_items - ret.metrics.n_answerable} unanswerable queries. Recall@1 is bounded by the number of relevant documents per query. Abstention on unanswerable queries (hybrid): precision {fmt.num(ret.metrics.hybrid.abstention.precision, 2)}, recall {fmt.num(ret.metrics.hybrid.abstention.recall, 2)}.</p>
                </>
              )}
            </Card>
            <Card title="Citations & grounding (evidence engine on gold definitions)">
              {!evd ? <Empty title="No evidence run" /> : (
                <div className="grid grid-cols-2 gap-3">
                  <Stat label="Citation precision" value={fmt.pct(evd.metrics.citation_precision)} sub={`${fmt.n(evd.metrics.citations)} citations`} />
                  <Stat label="Unsupported claim rate" value={fmt.pct(evd.metrics.unsupported_claim_rate)} sub="0 by construction (extractive composer)" />
                  <Stat label="Abstention recall" value={fmt.pct(evd.metrics.insufficient_evidence.abstention_recall)} sub="criteria with no corpus coverage flagged" />
                  <Stat label="Answerable coverage" value={fmt.pct(evd.metrics.insufficient_evidence.answerable_coverage)} sub={`${evd.metrics.insufficient_evidence.flagged_but_answerable} false 'insufficient' flags`} />
                </div>
              )}
            </Card>
          </div>
          <Card title="Run history (version-tracked)">
            <table className="tbl">
              <thead><tr><th>When</th><th>Suite</th><th>System version</th><th>Headline</th><th>Benchmark</th><th>Data version</th><th>Code</th><th className="text-right">Duration</th></tr></thead>
              <tbody>{runs.data.map((r) => (
                <tr key={r.eval_run_id}>
                  <td className="text-xs text-ink-2">{fmt.dt(r.started_at)}</td><td><Badge value="inclusion">{r.suite}</Badge></td>
                  <td className="mono max-w-[220px] truncate" title={r.system_version}>{r.system_version}</td>
                  <td className="tabular-nums">{r.suite === "extraction" ? `exact ${fmt.pct(r.metrics.exact_match)}` : r.suite === "retrieval" ? `R@5 ${fmt.num(r.metrics.hybrid["recall@5"], 2)}` : `cit. prec ${fmt.pct(r.metrics.citation_precision)}`}</td>
                  <td className="mono">{fmt.short(r.benchmark_sha256, 10)}</td><td className="mono">{r.data_version ?? "—"}</td><td className="mono">{r.code_version}</td>
                  <td className="text-right tabular-nums">{fmt.ms(r.duration_ms)}</td>
                </tr>))}</tbody>
            </table>
          </Card>
        </>
      )}
    </div>
  );
}

function FailureTable({ runs }: { runs: any[] }) {
  const cats = Array.from(new Set(runs.flatMap((r) => Object.keys(r.metrics.failure_categories)))).sort();
  if (!cats.length) return <Empty title="No failures" />;
  return (
    <table className="tbl"><thead><tr><th>Category</th>{runs.map((r) => <th key={r.eval_run_id} className="text-right">{r.system_version}</th>)}</tr></thead>
      <tbody>{cats.map((c) => <tr key={c}><td className="mono">{c}</td>{runs.map((r) => <td key={r.eval_run_id} className="text-right tabular-nums">{r.metrics.failure_categories[c] ?? 0}</td>)}</tr>)}</tbody></table>
  );
}

function FailingItems({ id }: { id: string }) {
  const d = useApi<any>(`/api/eval/runs/${id}?only_failures=true`);
  if (!d.data) return <Card><Loading /></Card>;
  return (
    <Card title={`Failing items · ${d.data.system_version} (${d.data.items.length})`}>
      <table className="tbl"><thead><tr><th>Item</th><th>Category</th><th>Request</th><th>Missing (gold, not predicted)</th><th>Extra (predicted, not gold)</th></tr></thead>
        <tbody>{d.data.items.map((it: any) => (
          <tr key={it.item_id}><td className="mono">{it.item_id}</td><td className="mono">{it.failure_category}</td><td className="max-w-[320px]">{it.details.request}</td>
            <td className="mono text-[11px] max-w-[260px] break-all">{it.details.missing?.join("\n") || "—"}</td><td className="mono text-[11px] max-w-[260px] break-all">{it.details.extra?.join("\n") || "—"}</td></tr>))}</tbody></table>
    </Card>
  );
}
