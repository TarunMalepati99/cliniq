"use client";
import { useState } from "react";
import { HBar } from "@/components/charts";
import { Badge, Card, Empty, ErrorBox, JsonView, Loading, Meter, PageHeader, Stat, useApi } from "@/components/ui";
import { fmt } from "@/lib/api";
import { SERIES, STATUS } from "@/lib/palette";

export default function Quality() {
  const mon = useApi<any>("/api/monitoring/overview");
  const lin = useApi<any>("/api/monitoring/lineage");
  const [dlqStatus, setDlqStatus] = useState("open");
  const dlq = useApi<any[]>(`/api/dead-letter?status=${dlqStatus}&limit=200`);
  const [sel, setSel] = useState<any>(null);
  if (mon.error) return <ErrorBox error={mon.error} />;
  if (!mon.data) return <Loading />;
  const m = mon.data;
  const f = m.freshness || {};
  const byCode: Record<string, number> = {};
  (m.dead_letter || []).forEach((d: any) => { byCode[`${d.stage}:${d.error_code}`] = (byCode[`${d.stage}:${d.error_code}`] || 0) + d.n; });
  const raw = m.raw_record_status || {};
  const rawTotal = Object.values(raw).reduce((a: number, b: any) => a + Number(b), 0) as number;
  const rq = m.retrieval || {};
  const cq = m.query_performance?.cohort_runs || {};

  return (
    <div className="space-y-5">
      <PageHeader title="Data quality & lineage" subtitle="Failures are surfaced, not hidden: rejected records, schema violations, duplicates, missingness and freshness, plus dataset-level lineage from source files to dbt marts." />
      <div className="grid grid-cols-2 lg:grid-cols-6 gap-3">
        <Stat label="Hours since last ingest" value={f.hours_since_ingest != null ? Number(f.hours_since_ingest).toFixed(1) : "—"} sub={fmt.dt(f.last_ingest)} />
        <Stat label="Latest lab date in data" value={f.latest_measurement_date ?? "—"} sub={`genomics ${f.latest_genomic_date ?? "—"}`} />
        <Stat label="Last dbt build" value={lin.data?.last_dbt_run ? <Badge value={lin.data.last_dbt_run.status} /> : "—"} sub={fmt.dt(f.last_dbt)} />
        <Stat label="Raw records loaded" value={fmt.pct(rawTotal ? (raw.loaded || 0) / rawTotal : null)} sub={`${fmt.n(raw.rejected || 0)} rejected · ${fmt.n(raw.pending || 0)} pending`} tone={raw.pending ? STATUS.warning : STATUS.good} />
        <Stat label="Cohort query p95" value={fmt.ms(cq.p95_ms)} sub={`${fmt.n(cq.runs)} runs · ${fmt.n(cq.failed)} failed`} />
        <Stat label="Evidence: insufficient rate" value={rq.queries ? fmt.pct(rq.insufficient / rq.queries) : "—"} sub={`${fmt.n(rq.queries)} queries logged`} />
      </div>

      <div className="grid xl:grid-cols-2 gap-4">
        <Card title="Rejected records by stage and error (all runs)">
          {!Object.keys(byCode).length ? <Empty title="No rejected records" /> :
            <HBar data={Object.entries(byCode).sort((a, b) => b[1] - a[1]).map(([k, n]) => ({ label: k, n }))} category="label" series={[{ key: "n", name: "Records", color: SERIES[0] }]} />}
        </Card>
        <Card title="Data-quality issues (loaded with warnings)">
          {!m.dq_issues.length ? <Empty title="No data-quality issues" /> : (
            <table className="tbl"><thead><tr><th>Check</th><th>Severity</th><th className="text-right">Count</th></tr></thead>
              <tbody>{m.dq_issues.map((d: any) => <tr key={d.check_name}><td className="mono">{d.check_name}</td><td><Badge value={d.severity === "warning" ? "partial" : "unknown"}>{d.severity}</Badge></td><td className="text-right tabular-nums">{fmt.n(d.n)}</td></tr>)}</tbody></table>
          )}
          <p className="text-xs text-ink-3 mt-2">Unit conversions and missing birth dates are counted in run metrics (Pipeline page) rather than as issues.</p>
        </Card>
      </div>

      <div className="grid xl:grid-cols-2 gap-4">
        <Card title="Missingness (null rate by field)">
          <table className="tbl"><thead><tr><th>Field</th><th>Null rate</th></tr></thead>
            <tbody>{m.missingness.map((x: any) => <tr key={x.field}><td className="mono">{x.field}</td><td><Meter value={x.null_rate} color={SERIES[1]} /></td></tr>)}</tbody></table>
        </Card>
        <Card title="Domain coverage (patients with ≥1 record)">
          <table className="tbl"><thead><tr><th>Domain</th><th>Coverage</th></tr></thead>
            <tbody>{m.domain_coverage.map((x: any) => <tr key={x.domain}><td>{x.domain}</td><td><Meter value={x.coverage} /></td></tr>)}</tbody></table>
        </Card>
      </div>

      <Card title="Dead-letter queue" actions={
        <select className="input max-w-[160px]" value={dlqStatus} onChange={(e) => setDlqStatus(e.target.value)}>
          <option value="open">Open</option><option value="retry_failed">Retry failed</option><option value="resolved">Resolved</option><option value="">All</option>
        </select>}>
        <ErrorBox error={dlq.error} />
        {!dlq.data ? <Loading /> : !dlq.data.length ? <Empty title="No records in this state" /> : (
          <div className="space-y-4">
            <div className="max-h-96 overflow-auto">
              <table className="tbl"><thead><tr><th>ID</th><th>Record locator</th><th>Stage</th><th>Error</th><th>Status</th><th className="text-right">Retries</th><th>Received</th></tr></thead>
                <tbody>{dlq.data.map((d) => (
                  <tr key={d.dlq_id} className={`cursor-pointer ${sel?.dlq_id === d.dlq_id ? "bg-brand-soft" : ""}`} onClick={() => setSel(d)}>
                    <td className="mono">{d.dlq_id}</td><td className="mono text-[11px]">{d.record_locator}</td>
                    <td className="text-xs">{d.stage}</td><td className="mono text-[11px]">{d.error_code}</td><td><Badge value={d.status} /></td><td className="text-right">{d.retry_count}</td>
                    <td className="text-xs text-ink-2">{fmt.dt(d.created_at)}</td></tr>))}</tbody></table>
            </div>
            <div>{sel ? (<div className="grid md:grid-cols-2 gap-4"><div><div className="label mb-1">Error detail · record {sel.dlq_id}</div><JsonView value={sel.error_detail} maxH="max-h-64" /></div><div><div className="label mb-1">Rejected payload (as received)</div><JsonView value={sel.payload} maxH="max-h-64" /></div></div>)
              : <Empty title="Select a record to inspect the error and payload" />}</div>
          </div>
        )}
      </Card>

      <Card title="Lineage: source format → normalized table (rows)">
        {!lin.data ? <Loading /> : (
          <div className="grid xl:grid-cols-3 gap-4">
            <div className="xl:col-span-2">
              <table className="tbl"><thead><tr><th>Source format</th><th>→</th><th>Clinical table</th><th className="text-right">Rows</th></tr></thead>
                <tbody>{lin.data.flows.map((x: any) => <tr key={x.source_format + x.target}><td className="mono">{x.source_format}</td><td className="text-ink-3">→</td><td className="mono">clinical.{x.target}</td><td className="text-right tabular-nums">{fmt.n(x.n)}</td></tr>)}</tbody></table>
            </div>
            <div>
              <div className="label mb-1">dbt models (built from clinical.*)</div>
              <ul className="space-y-0.5">{lin.data.dbt_models.map((d: any) => <li key={d.table_name} className="mono">{d.table_schema}.{d.table_name}</li>)}</ul>
              {lin.data.last_dbt_run?.metrics?.statuses && <div className="mt-3 text-xs text-ink-2">Last build: {Object.entries(lin.data.last_dbt_run.metrics.statuses).map(([k, v]) => `${v} ${k}`).join(" · ")}</div>}
            </div>
          </div>
        )}
      </Card>

      <Card title="API performance (last 7 days)">
        {!m.query_performance.api.length ? <Empty title="No API traffic recorded" /> : (
          <table className="tbl"><thead><tr><th>Route</th><th className="text-right">Requests</th><th className="text-right">p50</th><th className="text-right">p95</th><th className="text-right">4xx</th><th className="text-right">5xx</th></tr></thead>
            <tbody>{m.query_performance.api.map((a: any) => <tr key={a.route}><td className="mono">{a.route}</td><td className="text-right tabular-nums">{fmt.n(a.n)}</td><td className="text-right tabular-nums">{fmt.ms(a.p50_ms)}</td><td className="text-right tabular-nums">{fmt.ms(a.p95_ms)}</td><td className="text-right tabular-nums">{a.errors_4xx}</td><td className="text-right tabular-nums">{a.errors_5xx}</td></tr>)}</tbody></table>
        )}
      </Card>
    </div>
  );
}
