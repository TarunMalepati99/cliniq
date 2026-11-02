"use client";
import Link from "next/link";
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader, useApi } from "@/components/ui";
import { fmt } from "@/lib/api";

export default function Runs() {
  const runs = useApi<any[]>("/api/cohort/runs");
  return (
    <div className="space-y-5">
      <PageHeader title="Cohort results" subtitle="Every execution of an approved cohort definition, with its data version and result hash for reproducibility." />
      <ErrorBox error={runs.error} />
      <Card>
        {!runs.data ? <Loading /> : !runs.data.length ? <Empty title="No cohort queries executed yet"><Link className="link" href="/cohort">Build a cohort →</Link></Empty> : (
          <table className="tbl">
            <thead><tr><th>Cohort</th><th>Status</th><th className="text-right">Eligible</th><th className="text-right">Potentially eligible</th><th>Executed</th><th className="text-right">Duration</th><th>Data version</th><th>Result hash</th></tr></thead>
            <tbody>{runs.data.map((r) => (
              <tr key={r.cohort_run_id}>
                <td className="max-w-[340px] truncate"><Link className="link" href={`/cohort/runs/${r.cohort_run_id}`}>{r.name}</Link></td>
                <td><Badge value={r.status} /></td>
                <td className="text-right tabular-nums">{fmt.n(r.eligible)}</td><td className="text-right tabular-nums">{fmt.n(r.potentially_eligible)}</td>
                <td className="text-xs text-ink-2">{r.executed_by} · {fmt.dt(r.started_at)}</td><td className="text-right tabular-nums">{fmt.ms(r.duration_ms)}</td>
                <td className="mono">{r.data_version}</td><td className="mono">{fmt.short(r.result_sha256, 12)}</td>
              </tr>))}</tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
