"use client";
import { useState } from "react";
import { api, fmt } from "@/lib/api";
import { ErrorBox, JsonView } from "./ui";

/** Click-to-expand provenance for any clinical row: raw record → source file → pipeline run. */
export function LineageLink({ rawId }: { rawId: number | string | null | undefined }) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState<unknown>(null);
  if (rawId == null) return <span className="text-ink-3">—</span>;
  async function toggle() {
    setOpen(!open);
    if (!data && !open) {
      try {
        setData(await api(`/api/data/lineage/${rawId}`));
      } catch (e) {
        setErr(e);
      }
    }
  }
  return (
    <span>
      <button className="link mono" onClick={toggle} title="Show source lineage">raw#{rawId}</button>
      {open && (
        <div className="mt-2 rounded-md border border-surface-line bg-white p-3 text-xs space-y-1 min-w-[360px]">
          <ErrorBox error={err} />
          {data && (
            <>
              <div><span className="text-ink-3">Source file:</span> <span className="mono">{data.file_name}</span> ({data.source_format}, {data.source_system})</div>
              <div><span className="text-ink-3">Record:</span> <span className="mono">{data.record_locator}</span> · {data.record_type} · status {data.process_status}</div>
              <div><span className="text-ink-3">Content SHA-256:</span> <span className="mono">{fmt.short(data.content_sha256, 16)}…</span></div>
              <div><span className="text-ink-3">Pipeline run:</span> <span className="mono">{fmt.short(data.run_id)}</span> · {data.pipeline} · {fmt.dt(data.run_started_at)} · {data.transform_version ?? "—"}</div>
              <details><summary className="cursor-pointer text-brand">Source payload</summary><JsonView value={data.payload} maxH="max-h-56" /></details>
            </>
          )}
        </div>
      )}
    </span>
  );
}
