"use client";
import Link from "next/link";
import { useParams } from "next/navigation";
import { Fragment, useState } from "react";
import { HBar } from "@/components/charts";
import { LineageLink } from "@/components/Lineage";
import { Badge, Card, Empty, ErrorBox, JsonView, Legend, Loading, Meter, PageHeader, Stat, useApi } from "@/components/ui";
import { api, fmt } from "@/lib/api";
import { CLASS_COLOR, CLASS_LABEL, EVIDENCE_COLOR, EVIDENCE_LABEL, SERIES } from "@/lib/palette";

const EVIDENCE_ORDER = ["positive", "negative", "absent", "unknown"];

export default function RunPage() {
  const { id } = useParams<{ id: string }>();
  const run = useApi<any>(`/api/cohort/runs/${id}`);
  const [repro, setRepro] = useState<any>(null);
  const [reproErr, setReproErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  if (run.error) return <ErrorBox error={run.error} />;
  if (!run.data) return <Loading />;
  const r = run.data;
  const s = r.summary;
  if (r.status !== "succeeded" || !s) return (<><PageHeader title="Cohort run" /><Card><Badge value={r.status} /> <pre className="mono mt-2">{r.error}</pre></Card></>);
  const crit = s.criteria as any[];
  const total = s.total_patients || 1;

  async function reproduce() {
    setBusy(true); setReproErr(null);
    try { setRepro(await api(`/api/cohort/runs/${id}/reproduce`, { method: "POST" })); } catch (e) { setReproErr(e); } finally { setBusy(false); }
  }

  return (
    <div className="space-y-5">
      <PageHeader title={r.definition.name} subtitle={r.definition.nl_request ? `“${r.definition.nl_request}”` : "Manually defined cohort"}
        actions={<>
          <Link className="btn-secondary" href={`/evidence?definition=${r.definition_id}`}>Supporting evidence</Link>
          <button className="btn-secondary" onClick={reproduce} disabled={busy}>{busy ? "Re-executing…" : "Reproduce run"}</button>
        </>} />
      <ErrorBox error={reproErr} />
      {repro && (
        <div className={`rounded-md border px-3 py-2 text-sm ${repro.identical_result ? "border-[#bfe5bf] bg-[#f0faf0]" : "border-[#f5dc9c] bg-[#fff8e6]"}`}>
          <strong>{repro.identical_result ? "✓ Reproduced: identical patient set." : "! Result differs from the original run."}</strong>{" "}
          Same SQL: {String(repro.same_sql)} · same data version: {String(repro.same_data_version)} ·{" "}
          <Link className="link" href={`/cohort/runs/${repro.new_run_id}`}>new run {repro.new_run_id.slice(0, 8)}</Link>
        </div>
      )}

      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
        <Stat label="Eligible" value={fmt.n(s.eligible)} tone={CLASS_COLOR.eligible} sub="all criteria satisfied by positive/negative evidence" />
        <Stat label="Potentially eligible" value={fmt.n(s.potentially_eligible)} tone={CLASS_COLOR.potentially_eligible} sub="nothing disqualifying, ≥1 unknown" />
        <Stat label="Ineligible" value={fmt.n(s.ineligible)} tone={CLASS_COLOR.ineligible} sub={`${fmt.n(s.ineligible_only_by_absent_evidence)} only via weak “absent” evidence`} />
        <Stat label="Patients evaluated" value={fmt.n(s.total_patients)} sub={`as of ${s.as_of_date}`} />
        <Stat label="Query time" value={fmt.ms(s.query_ms)} sub={`${s.compiler_version}`} />
      </div>

      <div className="grid xl:grid-cols-2 gap-4">
        <Card title="Attrition funnel" actions={<Legend items={[{ label: "All criteria met so far", color: SERIES[0] }, { label: "Including unknown (not yet disqualified)", color: SERIES[1] }]} />}>
          <HBar data={s.funnel.map((f: any, i: number) => ({ ...f, label: `${i === 0 ? "" : (f.cid === "exclusions" ? "E " : f.cid + " ")}${f.step.length > 40 ? f.step.slice(0, 40) + "…" : f.step}` }))}
            category="label" series={[{ key: "strict", name: "All criteria met", color: SERIES[0] }, { key: "with_unknown", name: "Including unknown", color: SERIES[1] }]} />
          <p className="text-xs text-ink-3 mt-1">Inclusion criteria applied in order, then exclusions as a block. The gap between the two bars is the population blocked only by missing information.</p>
        </Card>
        <Card title="Missing information blocking potentially eligible patients">
          {!s.missing_information.length ? <Empty title="No patients are blocked by unknown information" /> : (
            <table className="tbl"><thead><tr><th>Criterion</th><th>Patients blocked</th></tr></thead>
              <tbody>{s.missing_information.map((m: any) => (
                <tr key={m.cid}><td><span className="mono text-ink-3 mr-1">{m.cid}</span>{m.label}</td>
                  <td><Meter value={m.patients_blocked / Math.max(1, s.potentially_eligible)} label={fmt.n(m.patients_blocked)} color={SERIES[1]} /></td></tr>))}</tbody></table>
          )}
          <p className="text-xs text-ink-3 mt-2">Unknown is never counted as “not satisfied”. These patients could become eligible after chart review or testing.</p>
        </Card>
      </div>

      <Card title="Criterion-by-criterion evidence" actions={<Legend items={EVIDENCE_ORDER.map((e) => ({ label: EVIDENCE_LABEL[e], color: EVIDENCE_COLOR[e] }))} />}>
        <table className="tbl">
          <thead><tr><th>#</th><th>Criterion</th><th>Role</th><th className="w-[38%]">Evidence across all patients</th><th>Data completeness</th></tr></thead>
          <tbody>{crit.map((c) => (
            <tr key={c.cid}>
              <td className="mono">{c.cid}</td><td>{c.label}</td><td><Badge value={c.role} /></td>
              <td><EvidenceBar counts={c.evidence_counts} total={total} /></td>
              <td><Meter value={c.completeness} /></td>
            </tr>))}</tbody>
        </table>
        <p className="text-xs text-ink-3 mt-2">Positive: matching record. Negative: explicit contradicting evidence (e.g. test negative, other histology, value out of range). Absent: records exist in the domain but none match (weak evidence). Unknown: no data in the domain.</p>
      </Card>

      <Distributions d={s.distributions} />
      <PatientTable runId={id} criteria={crit} />

      <Card title="Reproducibility record">
        <div className="grid md:grid-cols-2 gap-2 text-xs">
          <div><span className="text-ink-3">Run id:</span> <span className="mono">{r.cohort_run_id}</span></div>
          <div><span className="text-ink-3">Definition:</span> <Link className="link mono" href={`/cohort?id=${r.definition_id}`}>{r.definition_id}</Link></div>
          <div><span className="text-ink-3">Approved SQL SHA-256:</span> <span className="mono">{r.sql_sha256}</span></div>
          <div><span className="text-ink-3">Result SHA-256:</span> <span className="mono">{r.result_sha256}</span></div>
          <div><span className="text-ink-3">Data version:</span> <span className="mono">{r.data_version}</span></div>
          <div><span className="text-ink-3">Executed by:</span> {r.executed_by} · {fmt.dt(r.started_at)}</div>
        </div>
      </Card>
    </div>
  );
}

function EvidenceBar({ counts, total }: { counts: Record<string, number>; total: number }) {
  return (
    <div>
      <div className="flex h-3 w-full overflow-hidden rounded-full bg-surface-app gap-[2px]" role="img" aria-label={EVIDENCE_ORDER.map((e) => `${e} ${counts[e] ?? 0}`).join(", ")}>
        {EVIDENCE_ORDER.filter((e) => counts[e]).map((e) => (
          <div key={e} title={`${EVIDENCE_LABEL[e]}: ${counts[e]}`} style={{ width: `${(counts[e] / total) * 100}%`, background: EVIDENCE_COLOR[e] }} />
        ))}
      </div>
      <div className="mt-1 flex flex-wrap gap-x-3 text-[11px] text-ink-2 tabular-nums">
        {EVIDENCE_ORDER.map((e) => <span key={e}>{e} {fmt.n(counts[e] ?? 0)}</span>)}
      </div>
    </div>
  );
}

function Distributions({ d }: { d: any }) {
  if (!d || !d.population) return <Card title="Cohort composition"><Empty title="No eligible or potentially eligible patients">Distributions are computed over the eligible + potentially eligible population.</Empty></Card>;
  const one = (rows: any[], key = "n", labelKey = "key") => rows.map((r) => ({ ...r, label: r.label ? `${r.key} · ${String(r.label).slice(0, 30)}` : r[labelKey] }));
  return (
    <div className="space-y-2">
      <div className="text-sm font-semibold">Cohort composition <span className="font-normal text-ink-3">— eligible + potentially eligible ({fmt.n(d.population)} patients)</span></div>
      <div className="grid xl:grid-cols-3 gap-4">
        <Card title="Biomarker results" actions={<Legend items={[{ label: "Detected", color: SERIES[0] }, { label: "Not detected", color: SERIES[1] }, { label: "Indeterminate", color: SERIES[2] }]} />}>
          {!d.biomarkers.length ? <Empty title="No biomarker testing in this population" /> :
            <HBar data={one(d.biomarkers)} category="label" stacked series={[{ key: "detected", name: "Detected", color: SERIES[0] }, { key: "not_detected", name: "Not detected", color: SERIES[1] }, { key: "indeterminate", name: "Indeterminate", color: SERIES[2] }]} />}
        </Card>
        <Card title="Diagnoses (patients)"><HBar data={one(d.diagnoses)} category="label" series={[{ key: "n", name: "Patients", color: SERIES[0] }]} /></Card>
        <Card title="Treatment history (patients, by drug class)">
          {!d.treatments.length ? <Empty title="No medication records in this population" /> : <HBar data={one(d.treatments)} category="label" series={[{ key: "n", name: "Patients", color: SERIES[0] }]} />}
        </Card>
        <Card title="Geography (state of residence)"><HBar data={d.geography.map((g: any) => ({ ...g, label: g.key }))} category="label" series={[{ key: "n", name: "Patients", color: SERIES[0] }]} /></Card>
        <Card title="Age bands"><HBar data={d.age_bands.map((g: any) => ({ ...g, label: g.key }))} category="label" series={[{ key: "n", name: "Patients", color: SERIES[0] }]} /></Card>
        <Card title="Sex"><HBar data={d.sex.map((g: any) => ({ ...g, label: g.key }))} category="label" series={[{ key: "n", name: "Patients", color: SERIES[0] }]} /></Card>
      </div>
    </div>
  );
}

function PatientTable({ runId, criteria }: { runId: string; criteria: any[] }) {
  const [cls, setCls] = useState<"eligible" | "potentially_eligible" | "ineligible">("eligible");
  const [page, setPage] = useState(0);
  const [open, setOpen] = useState<string | null>(null);
  const rows = useApi<any[]>(`/api/cohort/runs/${runId}/patients?classification=${cls}&limit=50&offset=${page * 50}`);
  const label = Object.fromEntries(criteria.map((c) => [c.cid, c.label]));
  return (
    <Card title="Why each patient was included or excluded"
      actions={<div className="flex gap-1">{(["eligible", "potentially_eligible", "ineligible"] as const).map((c) => (
        <button key={c} className={c === cls ? "btn-primary" : "btn-secondary"} onClick={() => { setCls(c); setPage(0); setOpen(null); }}>{CLASS_LABEL[c]}</button>))}</div>}>
      <ErrorBox error={rows.error} />
      {!rows.data ? <Loading /> : !rows.data.length ? <Empty title={`No ${CLASS_LABEL[cls].toLowerCase()} patients`} /> : (
        <table className="tbl">
          <thead><tr><th>Patient</th>{criteria.map((c) => <th key={c.cid} title={c.label}>{c.cid}</th>)}<th /></tr></thead>
          <tbody>{rows.data.map((p) => (
            <Fragment key={p.person_id}>
              <tr>
                <td className="mono">{p.person_id}</td>
                {p.criteria.map((c: any) => <td key={c.cid}><Badge value={c.evidence} /></td>)}
                <td><button className="link text-xs" onClick={() => setOpen(open === p.person_id ? null : p.person_id)}>{open === p.person_id ? "Hide" : "Explain"}</button></td>
              </tr>
              {open === p.person_id && (
                <tr><td colSpan={criteria.length + 2} className="bg-surface-app/50">
                  <div className="grid md:grid-cols-2 gap-3">{p.criteria.map((c: any) => (
                    <div key={c.cid} className="rounded-md border border-surface-line bg-white p-2 text-xs">
                      <div className="flex items-center gap-2"><span className="mono text-ink-3">{c.cid}</span><span className="font-medium">{label[c.cid]}</span><Badge value={c.status} /><Badge value={c.evidence} /></div>
                      <EvidenceDetail detail={c.detail} />
                    </div>))}</div>
                </td></tr>
              )}
            </Fragment>))}</tbody>
        </table>
      )}
      <div className="mt-3 flex justify-between">
        <button className="btn-secondary" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button>
        <button className="btn-secondary" disabled={!rows.data || rows.data.length < 50} onClick={() => setPage(page + 1)}>Next</button>
      </div>
    </Card>
  );
}

function EvidenceDetail({ detail }: { detail: any }) {
  if (!detail) return null;
  const refs: any[] = detail.matching_conditions || detail.results || detail.matching_exposures || detail.matching_procedures || [];
  return (
    <div className="mt-1 space-y-1">
      {refs.length > 0 && (
        <ul className="space-y-0.5">{refs.map((r: any, i: number) => (
          <li key={i} className="text-ink-2">{Object.entries(r).filter(([k]) => k !== "source_record_id").map(([k, v]) => `${k}: ${v ?? "—"}`).join(" · ")} · <LineageLink rawId={r.source_record_id} /></li>))}</ul>
      )}
      {detail.source_record_id !== undefined && detail.value !== undefined && (
        <div className="text-ink-2">{detail.value === null ? "No value in lookback window" : `value ${detail.value} on ${detail.observed} (${detail.provenance})`} {detail.source_record_id != null && <LineageLink rawId={detail.source_record_id} />}</div>
      )}
      {!refs.length && detail.value === undefined && <JsonView value={detail} maxH="max-h-24" />}
    </div>
  );
}
