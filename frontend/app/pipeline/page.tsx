"use client";
import { useState } from "react";
import { useRole } from "@/components/Shell";
import { Badge, Card, Empty, ErrorBox, JsonView, Loading, PageHeader, Stat, useApi } from "@/components/ui";
import { api, fmt, runJob } from "@/lib/api";

export default function Pipeline() {
  const role = useRole();
  const runs = useApi<any[]>("/api/pipeline/runs?limit=100");
  const [sel, setSel] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [result, setResult] = useState<any>(null);
  const [err, setErr] = useState<unknown>(null);
  const [files, setFiles] = useState<FileList | null>(null);
  const admin = role === "admin";

  async function act(label: string, fn: () => Promise<any>) {
    setBusy(label); setErr(null); setResult(null);
    try {
      const r = await fn();
      setResult({ label, ...r });
      if (r?.run_id) setSel(r.run_id);
      runs.reload();
    } catch (e) { setErr(e); } finally { setBusy(null); }
  }
  async function upload() {
    if (!files?.length) return;
    const fd = new FormData();
    Array.from(files).forEach((f) => fd.append("files", f));
    await act("Upload & ingest", () => runJob("/api/ingest/upload", { body: fd }));
  }

  return (
    <div className="space-y-5">
      <PageHeader title="Data pipeline" subtitle="Raw data → validation → standardization → analytical database → dbt marts → evidence index. Every run is recorded with its metrics, events and rejected records." />
      <Card title="Run the pipeline" actions={!admin && <span className="text-xs text-ink-3">Admin role required to trigger runs</span>}>
        <div className="grid md:grid-cols-2 gap-4">
          <div>
            <div className="label mb-1">Upload FHIR R4 bundles (.json / .ndjson), lab or genomic CSV feeds, or notes (.jsonl)</div>
            <div className="flex gap-2">
              <input type="file" multiple accept=".json,.ndjson,.csv,.jsonl" onChange={(e) => setFiles(e.target.files)} className="input" disabled={!admin} />
              <button className="btn-primary" onClick={upload} disabled={!admin || !files?.length || !!busy}>{busy === "Upload & ingest" ? "Ingesting…" : "Upload & ingest"}</button>
            </div>
            <p className="text-xs text-ink-3 mt-1">Max 50 MB per file. Re-uploading identical bytes is a no-op (idempotent).</p>
          </div>
          <div className="flex flex-wrap gap-2 items-start">
            <button className="btn-secondary" disabled={!admin || !!busy} onClick={() => act("Ingest synthetic sample", () => runJob("/api/ingest/sample"))}>
              {busy === "Ingest synthetic sample" ? "Ingesting…" : "Ingest synthetic sample"}</button>
            <button className="btn-secondary" disabled={!admin || !!busy} onClick={() => act("dbt build", () => runJob("/api/pipeline/dbt"))}>
              {busy === "dbt build" ? "Running dbt…" : "Run dbt build"}</button>
            <button className="btn-secondary" disabled={!admin || !!busy} onClick={() => act("Rebuild evidence index", () => runJob("/api/evidence/reindex"))}>
              {busy === "Rebuild evidence index" ? "Indexing…" : "Rebuild evidence index"}</button>
            <button className="btn-secondary" disabled={!admin || !!busy} onClick={() => act("Retry dead letters", () => runJob("/api/dead-letter/retry", { json: {} }))}>
              {busy === "Retry dead letters" ? "Retrying…" : "Retry open dead letters"}</button>
          </div>
        </div>
        <div className="mt-3 space-y-2">
          <ErrorBox error={err} />
          {result && (
            <div className="rounded-md border border-surface-line bg-surface-app/60 p-3 text-sm">
              <div className="flex items-center gap-2"><strong>{result.label}</strong> <Badge value={result.status} /> {result.duration_ms != null && <span className="text-ink-3">{fmt.ms(result.duration_ms)}</span>}
                {result.resolved != null && <span className="text-ink-2">resolved {result.resolved}, still failing {result.still_failing}</span>}</div>
              {result.metrics && <RunMetrics m={result.metrics} />}
            </div>
          )}
        </div>
      </Card>

      <div className="grid xl:grid-cols-5 gap-4">
        <Card title="Pipeline runs" className="xl:col-span-2">
          <ErrorBox error={runs.error} />
          {!runs.data ? <Loading /> : !runs.data.length ? <Empty title="No runs yet" /> : (
            <div className="max-h-[620px] overflow-auto">
              <table className="tbl">
                <thead><tr><th>Pipeline</th><th>Status</th><th>Started</th><th className="text-right">Loaded</th><th className="text-right">Rejected</th></tr></thead>
                <tbody>{runs.data.map((r) => (
                  <tr key={r.run_id} className={`cursor-pointer ${sel === r.run_id ? "bg-brand-soft" : ""}`} onClick={() => setSel(r.run_id)}>
                    <td>{r.pipeline}</td><td><Badge value={r.status} /></td><td className="text-xs text-ink-2">{fmt.dt(r.started_at)}</td>
                    <td className="text-right tabular-nums">{r.metrics?.records_loaded_new != null ? fmt.n(r.metrics.records_loaded_new) : r.metrics?.documents_total != null ? `${r.metrics.documents_total} docs` : "—"}</td>
                    <td className="text-right tabular-nums">{r.metrics?.records_rejected != null ? fmt.n(r.metrics.records_rejected) : "—"}</td>
                  </tr>))}</tbody>
              </table>
            </div>
          )}
        </Card>
        <div className="xl:col-span-3">{sel ? <RunDetail id={sel} /> : <Card><Empty title="Select a run">Metrics, ordered event log, files and dead-letter records for the run appear here.</Empty></Card>}</div>
      </div>
    </div>
  );
}

function RunMetrics({ m }: { m: any }) {
  if (m.records_received == null) return <JsonView value={m} maxH="max-h-40" />;
  return (
    <div className="mt-3 grid grid-cols-3 md:grid-cols-6 gap-2">
      <Stat label="Received" value={fmt.n(m.records_received)} />
      <Stat label="Loaded (new)" value={fmt.n(m.records_loaded_new)} />
      <Stat label="Updated" value={fmt.n(m.records_updated)} />
      <Stat label="Duplicates skipped" value={fmt.n(m.duplicates_skipped)} />
      <Stat label="Rejected → DLQ" value={fmt.n(m.records_rejected)} />
      <Stat label="Files skipped (already seen)" value={fmt.n(m.files_skipped_already_ingested)} />
    </div>
  );
}

function RunDetail({ id }: { id: string }) {
  const { data, error } = useApi<any>(`/api/pipeline/runs/${id}`);
  if (error) return <ErrorBox error={error} />;
  if (!data) return <Card><Loading /></Card>;
  const m = data.metrics || {};
  return (
    <div className="space-y-4">
      <Card title={<span>{data.pipeline} run <span className="mono text-ink-3">{id.slice(0, 8)}</span></span>} actions={<Badge value={data.status} />}>
        <div className="text-xs text-ink-2">Triggered by {data.triggered_by} · {fmt.dt(data.started_at)} · {fmt.ms(data.duration_ms)}</div>
        {data.error && <pre className="mono mt-2 text-[#a12d2d] whitespace-pre-wrap">{data.error}</pre>}
        <RunMetrics m={m} />
        {m.rejections_by_code && Object.keys(m.rejections_by_code).length > 0 && (
          <div className="mt-3"><div className="label mb-1">Rejections by stage:code</div>
            <div className="flex flex-wrap gap-1.5">{Object.entries(m.rejections_by_code).map(([k, v]) => <span key={k} className="rounded border border-surface-line px-2 py-0.5 text-xs mono">{k} · {String(v)}</span>)}</div></div>
        )}
        {m.warnings && Object.keys(m.warnings).length > 0 && (
          <div className="mt-3"><div className="label mb-1">Warnings (loaded with data-quality flags)</div>
            <div className="flex flex-wrap gap-1.5">{Object.entries(m.warnings).map(([k, v]) => <span key={k} className="rounded border border-surface-line px-2 py-0.5 text-xs mono">{k} · {String(v)}</span>)}</div></div>
        )}
      </Card>
      <Card title={`Event log (${data.events.length})`}>
        <div className="max-h-64 overflow-auto">
          <table className="tbl"><thead><tr><th>Time</th><th>Level</th><th>Stage</th><th>Message</th><th>Details</th></tr></thead>
            <tbody>{data.events.map((e: any, i: number) => (
              <tr key={i}><td className="text-xs whitespace-nowrap">{new Date(e.ts).toLocaleTimeString()}</td><td className="text-xs">{e.level}</td><td className="text-xs">{e.stage}</td><td>{e.message}</td>
                <td className="mono text-ink-3 max-w-[260px] truncate" title={JSON.stringify(e.details)}>{JSON.stringify(e.details)}</td></tr>))}</tbody></table>
        </div>
      </Card>
      <Card title={`Dead-letter records from this run (${data.dead_letter.length})`}>
        {!data.dead_letter.length ? <Empty title="No rejected records" /> : (
          <div className="max-h-72 overflow-auto">
            <table className="tbl"><thead><tr><th>Locator</th><th>Stage</th><th>Error</th><th>Detail</th><th>Status</th></tr></thead>
              <tbody>{data.dead_letter.map((d: any) => (
                <tr key={d.dlq_id}><td className="mono">{d.record_locator}</td><td className="text-xs">{d.stage}</td><td className="mono">{d.error_code}</td>
                  <td className="mono text-ink-3 max-w-[260px] truncate" title={JSON.stringify(d.error_detail)}>{JSON.stringify(d.error_detail)}</td><td><Badge value={d.status} /></td></tr>))}</tbody></table>
          </div>
        )}
      </Card>
    </div>
  );
}
