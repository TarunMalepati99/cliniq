"use client";
import { useState } from "react";
import { HBar } from "@/components/charts";
import { LineageLink } from "@/components/Lineage";
import { Badge, Card, Empty, ErrorBox, Legend, Loading, PageHeader, Stat, useApi } from "@/components/ui";
import { fmt } from "@/lib/api";
import { SERIES } from "@/lib/palette";

const CANCERS: [string, string][] = [["", "All patients"], ["C34", "Lung (C34)"], ["C50", "Breast (C50)"], ["C18", "Colon (C18)"], ["C20", "Rectum (C20)"],
  ["C25", "Pancreas (C25)"], ["C43", "Melanoma (C43)"], ["C61", "Prostate (C61)"]];

export default function DataExplorer() {
  const summary = useApi<any>("/api/data/summary");
  const [q, setQ] = useState("");
  const [cancer, setCancer] = useState("");
  const [page, setPage] = useState(0);
  const [sel, setSel] = useState<string | null>(null);
  const qs = new URLSearchParams({ limit: "25", offset: String(page * 25), ...(q ? { q } : {}), ...(cancer ? { cancer } : {}) });
  const list = useApi<any>(`/api/data/patients?${qs}`);
  const s = summary.data;

  return (
    <div className="space-y-5">
      <PageHeader title="Clinical data explorer" subtitle="The normalized, OMOP-inspired analytical model after validation and standardization. Every row links back to its raw source record." />
      <ErrorBox error={summary.error} />
      {!s ? <Loading /> : (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-8 gap-3">
            {Object.entries(s.row_counts).map(([k, v]) => <Stat key={k} label={k.replaceAll("_", " ")} value={fmt.n(v)} />)}
          </div>
          <div className="grid lg:grid-cols-2 gap-4">
            <Card title="Most frequent diagnoses (patients)">
              <HBar data={s.top_diagnoses.map((d: any) => ({ ...d, label: `${d.code} · ${d.name.length > 34 ? d.name.slice(0, 34) + "…" : d.name}` }))}
                category="label" series={[{ key: "persons", name: "Patients", color: SERIES[0] }]} />
            </Card>
            <Card title="Biomarker results (patients, latest per test)" actions={<Legend items={[{ label: "Detected", color: SERIES[0] }, { label: "Not detected", color: SERIES[1] }, { label: "Indeterminate", color: SERIES[2] }]} />}>
              <HBar data={s.biomarkers} category="biomarker_key" stacked
                series={[{ key: "detected", name: "Detected", color: SERIES[0] }, { key: "not_detected", name: "Not detected", color: SERIES[1] }, { key: "indeterminate", name: "Indeterminate", color: SERIES[2] }]} />
              <p className="mt-2 text-xs text-ink-3">Patients never tested for a marker do not appear here — in cohorts they are treated as <em>unknown</em>, not negative.</p>
            </Card>
          </div>
        </>
      )}

      <div className="grid xl:grid-cols-5 gap-4">
        <Card title={`Patients ${list.data ? `(${fmt.n(list.data.total)})` : ""}`} className="xl:col-span-2">
          <div className="flex gap-2 mb-3">
            <input className="input" placeholder="Search patient id…" value={q} onChange={(e) => { setQ(e.target.value); setPage(0); }} />
            <select className="input max-w-[160px]" value={cancer} onChange={(e) => { setCancer(e.target.value); setPage(0); }}>
              {CANCERS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </div>
          <ErrorBox error={list.error} />
          <table className="tbl">
            <thead><tr><th>Patient</th><th>Sex</th><th>State</th><th>Conditions</th><th className="text-right">Biomarkers</th></tr></thead>
            <tbody>
              {(list.data?.items || []).map((p: any) => (
                <tr key={p.person_id} onClick={() => setSel(p.person_id)} className={`cursor-pointer ${sel === p.person_id ? "bg-brand-soft" : ""}`}>
                  <td className="mono">{p.person_id}</td><td>{p.sex}</td><td>{p.state ?? <span className="text-ink-3">—</span>}</td>
                  <td className="text-xs text-ink-2 max-w-[180px] truncate">{p.conditions ?? "—"}</td>
                  <td className="text-right tabular-nums">{p.biomarker_results}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-3 flex justify-between text-sm">
            <button className="btn-secondary" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button>
            <span className="text-ink-3 text-xs self-center">Page {page + 1}</span>
            <button className="btn-secondary" disabled={!list.data || (page + 1) * 25 >= list.data.total} onClick={() => setPage(page + 1)}>Next</button>
          </div>
        </Card>
        <div className="xl:col-span-3">{sel ? <PatientDetail id={sel} /> : <Card><Empty title="Select a patient">Records, extracted note facts and per-row source lineage appear here.</Empty></Card>}</div>
      </div>
    </div>
  );
}

function PatientDetail({ id }: { id: string }) {
  const { data, error } = useApi<any>(`/api/data/patients/${id}`);
  if (error) return <ErrorBox error={error} />;
  if (!data) return <Card><Loading /></Card>;
  const p = data.person;
  return (
    <div className="space-y-4">
      <Card title={<span className="mono">{p.person_id}</span>} actions={<LineageLink rawId={p.source_record_id} />}>
        <div className="grid grid-cols-4 gap-3 text-sm">
          <div><div className="label">Birth date</div>{p.birth_date ?? <Badge value="unknown" />}</div>
          <div><div className="label">Sex</div>{p.sex}</div>
          <div><div className="label">State</div>{p.state ?? <Badge value="unknown" />}</div>
          <div><div className="label">Smoking (from notes)</div>{p.smoking_status ?? <Badge value="unknown" />}</div>
        </div>
      </Card>
      <Card title="Conditions">
        {!data.conditions.length ? <Empty title="No conditions on record" /> : (
          <table className="tbl"><thead><tr><th>Code</th><th>Name</th><th>Histology</th><th>Onset</th><th>Source</th></tr></thead>
            <tbody>{data.conditions.map((c: any) => (
              <tr key={c.condition_id}><td className="mono">{c.code}</td><td>{c.name}</td><td className="mono">{c.histology_code ?? "—"}</td><td>{c.onset_date ?? "—"}</td><td><LineageLink rawId={c.source_record_id} /></td></tr>
            ))}</tbody></table>
        )}
      </Card>
      <Card title="Biomarker results">
        {!data.biomarkers.length ? <Empty title="No biomarker testing on record">Biomarker criteria will evaluate as unknown for this patient.</Empty> : (
          <table className="tbl"><thead><tr><th>Biomarker</th><th>HGVS</th><th>Result</th><th>Reported</th><th>Source</th></tr></thead>
            <tbody>{data.biomarkers.map((b: any) => (
              <tr key={b.variant_result_id}><td>{b.biomarker_key}</td><td className="mono">{b.hgvs_p ?? "—"}</td>
                <td><Badge value={b.result === "detected" ? "positive" : b.result === "not_detected" ? "negative" : "unknown"}>{b.result.replace("_", " ")}</Badge></td>
                <td>{b.reported_date}</td><td><LineageLink rawId={b.source_record_id} /></td></tr>
            ))}</tbody></table>
        )}
      </Card>
      <div className="grid md:grid-cols-2 gap-4">
        <Card title="Medications">
          {!data.drugs.length ? <Empty title="No medication records">Treatment history criteria evaluate as unknown.</Empty> : (
            <table className="tbl"><thead><tr><th>Drug</th><th>Class</th><th>Start</th><th>Source</th></tr></thead>
              <tbody>{data.drugs.map((d: any) => (
                <tr key={d.drug_exposure_id}><td>{d.drug_name}</td><td className="text-xs text-ink-2">{d.drug_class ?? "unmapped"}</td><td>{d.start_date ?? "—"}</td><td><LineageLink rawId={d.source_record_id} /></td></tr>
              ))}</tbody></table>
          )}
        </Card>
        <Card title={`Measurements (${data.measurements.length})`}>
          {!data.measurements.length ? <Empty title="No measurements" /> : (
            <div className="max-h-80 overflow-auto">
              <table className="tbl"><thead><tr><th>Lab</th><th className="text-right">Value</th><th>Source value</th><th>Date</th><th>Source</th></tr></thead>
                <tbody>{data.measurements.map((m: any) => (
                  <tr key={m.measurement_id}><td>{m.lab_key}</td><td className="text-right tabular-nums">{Number(m.value_numeric).toFixed(2)} <span className="text-ink-3 text-xs">{m.unit}</span></td>
                    <td className="text-xs text-ink-3">{m.source_unit !== m.unit ? `${m.source_value} ${m.source_unit}` : ""}</td><td>{m.measured_at}</td><td><LineageLink rawId={m.source_record_id} /></td></tr>
                ))}</tbody></table>
            </div>
          )}
        </Card>
      </div>
      <Card title="Notes and extracted facts">
        {!data.notes.length ? <Empty title="No notes" /> : data.notes.map((n: any) => (
          <div key={n.note_id} className="mb-3 last:mb-0">
            <div className="text-xs text-ink-3 mb-1">{n.note_date} · {n.note_type} · <LineageLink rawId={n.source_record_id} /></div>
            <p className="text-sm leading-relaxed"><HighlightedNote text={n.note_text} facts={n.facts || []} /></p>
          </div>
        ))}
        <p className="text-xs text-ink-3 mt-2">Highlighted spans were extracted by a deterministic pattern extractor (provenance: note text).</p>
      </Card>
    </div>
  );
}

function HighlightedNote({ text, facts }: { text: string; facts: { key: string; value: string; start: number; end: number }[] }) {
  const parts: React.ReactNode[] = [];
  let pos = 0;
  [...facts].sort((a, b) => a.start - b.start).forEach((f, i) => {
    if (f.start < pos) return;
    parts.push(text.slice(pos, f.start));
    parts.push(<mark key={i} title={`${f.key} = ${f.value}`} className="bg-brand-soft text-ink rounded px-0.5 border-b-2 border-brand">{text.slice(f.start, f.end)}</mark>);
    pos = f.end;
  });
  parts.push(text.slice(pos));
  return <>{parts}</>;
}
