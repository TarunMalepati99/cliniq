"use client";
import Link from "next/link";
import { Badge, Card, Empty, ErrorBox, Loading, Meter, PageHeader, Stat, useApi } from "@/components/ui";
import { fmt } from "@/lib/api";
import { STATUS } from "@/lib/palette";

export default function Overview() {
  const mon = useApi<any>("/api/monitoring/overview");
  const runs = useApi<any[]>("/api/cohort/runs");
  const m = mon.data;
  if (mon.error) return <ErrorBox error={mon.error} />;
  if (!m) return <Loading />;
  const rc = m.row_counts || {};
  const clinicalRows = Object.entries(rc).filter(([k]) => k !== "person").reduce((a, [, v]) => a + Number(v), 0);
  const openDlq = (m.dead_letter || []).filter((d: any) => d.status === "open").reduce((a: number, d: any) => a + d.n, 0);
  const lastIngest = m.ingestion_health?.last_run;
  const evals = (m.eval_trend || []) as any[];
  const latest = (suite: string, ver?: string) => [...evals].reverse().find((e) => e.suite === suite && (!ver || e.system_version === ver));
  const v2 = latest("extraction", "rules-v2");
  const v1 = latest("extraction", "rules-v1");
  const ret = latest("retrieval");
  const evd = latest("evidence");

  return (
    <div className="space-y-5">
      <PageHeader title="Overview" subtitle="Platform health and the latest measured outputs. Every value on this page is read from the system's operational tables." />
      {rc.person === 0 ? (
        <Card><Empty title="No clinical data ingested yet">Go to <Link className="link" href="/pipeline">Pipeline</Link> and upload a FHIR bundle or load the synthetic sample.</Empty></Card>
      ) : null}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <Stat label="Synthetic patients" value={fmt.n(rc.person)} sub={`${fmt.n(clinicalRows)} normalized clinical rows`} />
        <Stat label="Last ingestion" value={lastIngest ? <Badge value={lastIngest.status} /> : "—"}
          sub={lastIngest ? `${fmt.dt(lastIngest.finished_at)} · ${fmt.n(lastIngest.metrics?.records_loaded_new)} loaded, ${fmt.n(lastIngest.metrics?.records_rejected)} rejected` : "no runs"} />
        <Stat label="Open dead-letter records" value={fmt.n(openDlq)} tone={openDlq ? STATUS.critical : STATUS.good}
          sub={<Link className="link" href="/quality">Inspect failures →</Link>} />
        <Stat label="Cohort queries executed" value={fmt.n(runs.data?.length ?? 0)}
          sub={m.query_performance?.cohort_runs?.p50_ms != null ? `p50 ${fmt.ms(m.query_performance.cohort_runs.p50_ms)} · p95 ${fmt.ms(m.query_performance.cohort_runs.p95_ms)}` : "no executions yet"} />
      </div>

      <div className="grid lg:grid-cols-3 gap-4">
        <Card title="Data domain coverage" className="lg:col-span-1">
          <p className="text-xs text-ink-3 mb-3">Share of patients with at least one record in each domain. Gaps here become “unknown” evidence in cohorts.</p>
          <div className="space-y-2">
            {(m.domain_coverage || []).map((d: any) => (
              <div key={d.domain} className="flex items-center gap-3 text-sm">
                <span className="w-44 text-ink-2">{d.domain}</span>
                <Meter value={d.coverage} />
              </div>
            ))}
          </div>
        </Card>
        <Card title="Model & retrieval quality (latest benchmark)" className="lg:col-span-2"
          actions={<Link className="link text-xs" href="/evaluation">Details →</Link>}>
          {!v2 ? <Empty title="No benchmark runs yet">Run the benchmark from the Evaluation page.</Empty> : (
            <div className="grid sm:grid-cols-3 gap-3 text-sm">
              <div className="rounded-md border border-surface-line p-3">
                <div className="label">Criteria extraction · exact match</div>
                <div className="text-2xl font-semibold mt-1">{fmt.pct(v2.metrics.exact_match)}</div>
                <div className="text-xs text-ink-3 mt-1">rules-v2 · hand-written subset {fmt.pct(v2.metrics.by_subset?.handwritten?.exact_match)}
                  {v1 && <> · baseline v1 {fmt.pct(v1.metrics.exact_match)}</>}</div>
              </div>
              <div className="rounded-md border border-surface-line p-3">
                <div className="label">Cohort precision / recall vs gold</div>
                <div className="text-2xl font-semibold mt-1">{fmt.num(v2.metrics.cohort_precision, 2)} / {fmt.num(v2.metrics.cohort_recall, 2)}</div>
                <div className="text-xs text-ink-3 mt-1">{v2.n_items} requests · SQL success {fmt.pct(v2.metrics.sql_success_rate)}</div>
              </div>
              <div className="rounded-md border border-surface-line p-3">
                <div className="label">Evidence retrieval</div>
                <div className="text-2xl font-semibold mt-1">{ret ? fmt.num(ret.metrics.hybrid?.["recall@5"], 2) : "—"}</div>
                <div className="text-xs text-ink-3 mt-1">hybrid recall@5 · citation precision {evd ? fmt.pct(evd.metrics.citation_precision) : "—"}</div>
              </div>
            </div>
          )}
        </Card>
      </div>

      <div className="grid lg:grid-cols-2 gap-4">
        <Card title="Recent pipeline runs" actions={<Link className="link text-xs" href="/pipeline">All runs →</Link>}>
          <table className="tbl">
            <thead><tr><th>Pipeline</th><th>Status</th><th>Started</th><th className="text-right">Duration</th></tr></thead>
            <tbody>
              {(m.pipeline_runs || []).slice(0, 8).map((r: any) => (
                <tr key={r.run_id}><td>{r.pipeline}</td><td><Badge value={r.status} /></td><td className="text-ink-2">{fmt.dt(r.started_at)}</td><td className="text-right tabular-nums">{fmt.ms(r.duration_ms)}</td></tr>
              ))}
            </tbody>
          </table>
        </Card>
        <Card title="Recent cohort results" actions={<Link className="link text-xs" href="/cohort">New cohort →</Link>}>
          {!runs.data?.length ? <Empty title="No cohort queries executed yet">Build one in the Cohort builder.</Empty> : (
            <table className="tbl">
              <thead><tr><th>Cohort</th><th className="text-right">Eligible</th><th className="text-right">Potentially</th><th>Run</th></tr></thead>
              <tbody>
                {runs.data.slice(0, 8).map((r: any) => (
                  <tr key={r.cohort_run_id}>
                    <td className="max-w-[280px] truncate"><Link className="link" href={`/cohort/runs/${r.cohort_run_id}`}>{r.name}</Link></td>
                    <td className="text-right tabular-nums">{fmt.n(r.eligible)}</td>
                    <td className="text-right tabular-nums">{fmt.n(r.potentially_eligible)}</td>
                    <td className="text-ink-2">{fmt.dt(r.started_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>
    </div>
  );
}
