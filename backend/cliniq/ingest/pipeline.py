"""Ingestion pipeline: land → validate → standardize → load, with DLQ, lineage and metrics.

Guarantees
- Idempotent: a file whose bytes were already received is skipped; a record whose content
  hash was already landed is counted as a duplicate and not re-processed. Re-running the
  pipeline on the same inputs leaves the database unchanged.
- Incremental: only raw records with process_status='pending' are processed.
- No silent drops: every record ends as loaded, duplicate, or rejected (with a DLQ row).
- Lineage: every clinical row carries source_record_id → raw.records → ops.source_files.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import vocabulary as V
from ..db import connect, retry
from ..observability.logging import run_id_var
from .transform import MAPPERS, TRANSFORM_VERSION, Mapped, RecordRejected

log = logging.getLogger(__name__)

TYPE_PRIORITY = {"Patient": 0}  # persons first so references resolve within a run
TABLE_PK = {
    "person": "person_id", "condition_occurrence": "condition_id", "measurement": "measurement_id",
    "drug_exposure": "drug_exposure_id", "procedure_occurrence": "procedure_id", "genomic_variant": "variant_result_id",
    "note": "note_id",
}
FORMATS = {"fhir_bundle", "fhir_ndjson", "lab_csv", "genomic_csv", "notes_jsonl"}


@dataclass
class RawRecord:
    locator: str
    record_type: str
    source_key: str | None
    payload: dict[str, Any]

    @property
    def sha(self) -> str:
        return hashlib.sha256(json.dumps(self.payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass
class RunMetrics:
    files_received: int = 0
    files_skipped_already_ingested: int = 0
    files_rejected: int = 0
    records_received: int = 0
    records_landed: int = 0
    duplicates_skipped: int = 0
    records_loaded_new: int = 0
    records_updated: int = 0
    records_rejected: int = 0
    transformation_failures: int = 0
    warnings: Counter = field(default_factory=Counter)
    rejections_by_code: Counter = field(default_factory=Counter)
    loaded_by_table: Counter = field(default_factory=Counter)
    stage_ms: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if not isinstance(v, (Counter, dict))}
        d.update(warnings=dict(self.warnings), rejections_by_code=dict(self.rejections_by_code),
                 loaded_by_table=dict(self.loaded_by_table), stage_ms=self.stage_ms)
        return d


def detect_format(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".ndjson"):
        return "fhir_ndjson"
    if name.endswith(".json"):
        return "fhir_bundle"
    if name.endswith(".jsonl"):
        return "notes_jsonl"
    if name.endswith(".csv"):
        with path.open() as f:
            header = f.readline().lower()
        if "loinc" in header:
            return "lab_csv"
        if "biomarker" in header:
            return "genomic_csv"
    raise ValueError(f"cannot determine source format for {path.name}")


def parse_file(path: Path, fmt: str) -> list[RawRecord]:
    """Parse a file into raw records. Raises ValueError for file-level corruption."""
    text = path.read_text(encoding="utf-8")
    if fmt == "fhir_bundle":
        doc = json.loads(text)
        if doc.get("resourceType") != "Bundle" or not isinstance(doc.get("entry"), list):
            raise ValueError("not a FHIR Bundle with an entry array")
        out = []
        for i, e in enumerate(doc["entry"]):
            r = e.get("resource") if isinstance(e, dict) else None
            if not isinstance(r, dict):
                out.append(RawRecord(f"{path.name}#entry[{i}]", "Unknown", None, {"_invalid_entry": e}))
                continue
            out.append(RawRecord(f"{path.name}#entry[{i}]", str(r.get("resourceType", "Unknown")), r.get("id"), r))
        return out
    if fmt == "fhir_ndjson":
        out = []
        for i, line in enumerate(text.splitlines()):
            if line.strip():
                r = json.loads(line)
                out.append(RawRecord(f"{path.name}#line {i + 1}", str(r.get("resourceType", "Unknown")), r.get("id"), r))
        return out
    if fmt in ("lab_csv", "genomic_csv"):
        rtype = "LabRow" if fmt == "lab_csv" else "GenomicRow"
        reader = csv.DictReader(io.StringIO(text))
        return [RawRecord(f"{path.name}#line {i + 2}", rtype, None, dict(row)) for i, row in enumerate(reader)]
    if fmt == "notes_jsonl":
        out = []
        for i, line in enumerate(text.splitlines()):
            if line.strip():
                r = json.loads(line)
                out.append(RawRecord(f"{path.name}#line {i + 1}", "Note", r.get("note_id"), r))
        return out
    raise ValueError(f"unsupported format {fmt}")


class IngestionRun:
    def __init__(self, conn: psycopg.Connection[Any], triggered_by: str, params: dict[str, Any] | None = None,
                 pipeline: str = "ingest") -> None:
        self.conn = conn
        self.run_id = str(uuid.uuid4())
        self.metrics = RunMetrics()
        self.t0 = time.monotonic()
        self.pipeline = pipeline
        conn.execute("INSERT INTO ops.pipeline_runs (run_id, pipeline, status, triggered_by, params) VALUES (%s,%s,'running',%s,%s)",
                     (self.run_id, pipeline, triggered_by, Jsonb(params or {})))
        conn.commit()
        run_id_var.set(self.run_id)
        self.event("info", "run", "pipeline run started", params or {})

    def event(self, level: str, stage: str, message: str, details: dict[str, Any] | None = None) -> None:
        self.conn.execute("INSERT INTO ops.ingestion_events (run_id, level, stage, message, details) VALUES (%s,%s,%s,%s,%s)",
                          (self.run_id, level, stage, message, Jsonb(details or {})))
        getattr(log, "warning" if level == "warning" else level)(message, extra={"stage": stage, **(details or {})})

    def dead_letter(self, locator: str, rtype: str | None, stage: str, code: str, detail: Any, payload: Any,
                    source_file_id: int | None) -> None:
        self.conn.execute(
            "INSERT INTO ops.dead_letter (run_id, source_file_id, record_locator, resource_type, stage, error_code, error_detail, payload)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (self.run_id, source_file_id, locator, rtype, stage, code, Jsonb(detail if isinstance(detail, dict) else {"detail": detail}),
             Jsonb(payload)))
        self.metrics.records_rejected += 1
        self.metrics.rejections_by_code[f"{stage}:{code}"] += 1

    def finish(self, error: str | None = None) -> dict[str, Any]:
        m = self.metrics
        status = "failed" if error else ("partial" if m.records_rejected or m.files_rejected else "succeeded")
        dur = int((time.monotonic() - self.t0) * 1000)
        self.conn.execute("UPDATE ops.pipeline_runs SET status=%s, finished_at=now(), duration_ms=%s, metrics=%s, error=%s WHERE run_id=%s",
                          (status, dur, Jsonb(m.as_dict()), error, self.run_id))
        self.event("error" if error else "info", "run", f"pipeline run {status}", {"duration_ms": dur})
        self.conn.commit()
        return {"run_id": self.run_id, "status": status, "duration_ms": dur, "metrics": m.as_dict()}


# ---------------------------------------------------------------- landing

def land_file(run: IngestionRun, path: Path, source_system: str = "synthetic") -> None:
    conn, m = run.conn, run.metrics
    m.files_received += 1
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if conn.execute("SELECT 1 FROM ops.source_files WHERE file_sha256=%s", (sha,)).fetchone():
        m.files_skipped_already_ingested += 1
        run.event("info", "land", "file already ingested; skipped", {"file": path.name})
        return
    try:
        fmt = detect_format(path)
        records = parse_file(path, fmt)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        if conn.execute("SELECT 1 FROM ops.dead_letter WHERE stage='parse' AND payload->>'file_sha256'=%s", (sha,)).fetchone():
            m.files_skipped_already_ingested += 1
            run.event("warning", "parse", "file previously rejected as unparseable; skipped", {"file": path.name})
            return
        m.files_rejected += 1
        run.dead_letter(path.name, None, "parse", "file_unparseable", {"error": str(exc)[:500]},
                        {"file_name": path.name, "file_sha256": sha, "size_bytes": len(data)}, None)
        run.event("error", "parse", "file rejected: unparseable", {"file": path.name, "error": str(exc)[:200]})
        conn.commit()
        return
    sf = conn.execute(
        "INSERT INTO ops.source_files (file_name, file_sha256, source_format, source_system, size_bytes, first_run_id)"
        " VALUES (%s,%s,%s,%s,%s,%s) RETURNING source_file_id", (path.name, sha, fmt, source_system, len(data), run.run_id)).fetchone()
    sfid = sf["source_file_id"]  # type: ignore[index]
    m.records_received += len(records)
    landed = 0
    with conn.cursor() as cur:
        for rec in records:
            cur.execute(
                "INSERT INTO raw.records (source_file_id, run_id, record_locator, record_type, source_key, content_sha256, payload)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (record_type, content_sha256) DO NOTHING RETURNING raw_id",
                (sfid, run.run_id, rec.locator, rec.record_type, rec.source_key, rec.sha, Jsonb(rec.payload)))
            if cur.fetchone():
                landed += 1
            else:
                m.duplicates_skipped += 1
    m.records_landed += landed
    run.event("info", "land", "file landed", {"file": path.name, "format": fmt, "records": len(records), "landed": landed,
                                               "duplicates": len(records) - landed})
    conn.commit()


# ---------------------------------------------------------------- processing

def _upsert(conn: psycopg.Connection[Any], mapped: Mapped) -> bool:
    """Insert or update a normalized row. Returns True if newly inserted."""
    if mapped.table == "note_fact":
        cols = list(mapped.row)
        conn.execute(f"INSERT INTO clinical.note_fact ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})"
                     " ON CONFLICT (note_id, fact_key, span_start) DO NOTHING", list(mapped.row.values()))
        return True
    pk = TABLE_PK[mapped.table]
    cols = list(mapped.row)
    updates = ",".join(f"{c}=EXCLUDED.{c}" for c in cols if c != pk)
    sql = (f"INSERT INTO clinical.{mapped.table} ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})"
           f" ON CONFLICT ({pk}) DO UPDATE SET {updates} RETURNING (xmax = 0) AS inserted")
    row = conn.execute(sql, list(mapped.row.values())).fetchone()  # type: ignore[arg-type]
    return bool(row["inserted"])  # type: ignore[index]


def process_pending(run: IngestionRun, raw_ids: list[int] | None = None) -> None:
    conn, m = run.conn, run.metrics
    t0 = time.monotonic()
    where = "process_status='pending'" if raw_ids is None else "raw_id = ANY(%s)"
    rows = conn.execute(
        f"SELECT raw_id, source_file_id, record_locator, record_type, payload FROM raw.records WHERE {where}"
        " ORDER BY CASE WHEN record_type='Patient' THEN 0 ELSE 1 END, raw_id", (raw_ids,) if raw_ids is not None else None).fetchall()
    known = {r["person_id"] for r in conn.execute("SELECT person_id FROM clinical.person")}
    for r in rows:
        rtype = r["record_type"]
        mapper = MAPPERS["fhir"] if rtype not in MAPPERS else MAPPERS[rtype]
        try:
            mapped = mapper(r["payload"], r["raw_id"], run.run_id)
            if mapped.table != "person" and mapped.person_id not in known:
                raise RecordRejected("reference", "unresolved_patient_reference", {"person_id": mapped.person_id})
            with conn.transaction():  # savepoint: isolates DB failures to this record
                inserted = _upsert(conn, mapped)
                for child in mapped.children:
                    _upsert(conn, child)
                conn.execute("UPDATE raw.records SET process_status='loaded' WHERE raw_id=%s", (r["raw_id"],))
            if mapped.table == "person":
                known.add(mapped.person_id)
            m.loaded_by_table[mapped.table] += 1
            m.loaded_by_table["note_fact"] += len(mapped.children) if mapped.children else 0
            if inserted:
                m.records_loaded_new += 1
            else:
                m.records_updated += 1
            for code, details in mapped.warnings:
                m.warnings[code] += 1
                if code not in ("unit_converted", "missing_birth_date"):
                    conn.execute("INSERT INTO ops.dq_issues (run_id, check_name, severity, entity, entity_id, details) VALUES (%s,%s,'warning',%s,%s,%s)",
                                 (run.run_id, code, mapped.table, mapped.key, Jsonb(details)))
        except RecordRejected as rej:
            conn.execute("UPDATE raw.records SET process_status='rejected' WHERE raw_id=%s", (r["raw_id"],))
            run.dead_letter(r["record_locator"], rtype, rej.stage, rej.code, rej.detail, r["payload"], r["source_file_id"])
        except psycopg.Error as exc:  # unexpected DB-level failure for this record
            m.transformation_failures += 1
            conn.execute("UPDATE raw.records SET process_status='rejected' WHERE raw_id=%s", (r["raw_id"],))
            run.dead_letter(r["record_locator"], rtype, "load", "database_error", {"error": str(exc)[:500]}, r["payload"], r["source_file_id"])
    conn.commit()
    m.stage_ms["process"] = m.stage_ms.get("process", 0) + int((time.monotonic() - t0) * 1000)
    run.event("info", "process", "records processed", {"n": len(rows)})


def post_load(run: IngestionRun) -> None:
    """Derived fields and dataset-level data-quality checks."""
    conn = run.conn
    conn.execute("""
        UPDATE clinical.person p SET smoking_status = f.fact_value
        FROM (SELECT DISTINCT ON (nf.person_id) nf.person_id, nf.fact_value
              FROM clinical.note_fact nf JOIN clinical.note n USING (note_id)
              WHERE nf.fact_key='smoking_status' ORDER BY nf.person_id, n.note_date DESC) f
        WHERE p.person_id = f.person_id AND p.smoking_status IS DISTINCT FROM f.fact_value""")
    dups = conn.execute("""SELECT match_key, array_agg(person_id ORDER BY person_id) AS ids FROM clinical.person
                           WHERE match_key IS NOT NULL GROUP BY match_key HAVING count(*) > 1""").fetchall()
    already = {r["entity_id"] for r in conn.execute("SELECT entity_id FROM ops.dq_issues WHERE check_name='probable_duplicate_person'")}
    for d in dups:
        key = ",".join(d["ids"])
        if key not in already:
            conn.execute("INSERT INTO ops.dq_issues (run_id, check_name, severity, entity, entity_id, details) VALUES (%s,'probable_duplicate_person','warning','person',%s,%s)",
                         (run.run_id, key, Jsonb({"person_ids": d["ids"], "rule": "same name+birth date+sex (hashed)"})))
    run.metrics.warnings["probable_duplicate_person_groups"] = len(dups)
    conn.commit()


def seed_concepts(conn: psycopg.Connection[Any]) -> int:
    rows = V.concept_rows()
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO clinical.concept (vocabulary, code, name, domain, concept_class) VALUES (%s,%s,%s,%s,%s)"
                        " ON CONFLICT (vocabulary, code) DO UPDATE SET name=EXCLUDED.name, domain=EXCLUDED.domain, concept_class=EXCLUDED.concept_class", rows)
    conn.commit()
    return len(rows)


def ingest_paths(paths: list[Path], triggered_by: str = "cli", source_system: str = "synthetic") -> dict[str, Any]:
    """Ingest files (or directories). Returns the run summary."""
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files += sorted(x for x in p.rglob("*") if x.is_file() and x.suffix in (".json", ".ndjson", ".csv", ".jsonl")
                            and x.name not in ("manifest.json", "truth.json"))
        else:
            files.append(p)
    with connect() as conn:
        run = IngestionRun(conn, triggered_by, {"files": [f.name for f in files], "transform_version": TRANSFORM_VERSION})
        try:
            seed_concepts(conn)
            t0 = time.monotonic()
            for f in files:
                retry(lambda f=f: land_file(run, f, source_system), what=f"land {f.name}")
            run.metrics.stage_ms["land"] = int((time.monotonic() - t0) * 1000)
            process_pending(run)
            post_load(run)
            return run.finish()
        except Exception as exc:
            conn.rollback()
            log.exception("ingestion run failed")
            return run.finish(error=f"{type(exc).__name__}: {exc}")


def retry_dead_letters(dlq_ids: list[int] | None, triggered_by: str) -> dict[str, Any]:
    """Re-process open DLQ items whose raw record exists (e.g. after the referenced patient arrived)."""
    with connect() as conn:
        run = IngestionRun(conn, triggered_by, {"dlq_ids": dlq_ids}, pipeline="dlq_retry")
        q = """SELECT d.dlq_id, r.raw_id FROM ops.dead_letter d
               JOIN raw.records r ON r.source_file_id = d.source_file_id AND r.record_locator = d.record_locator
               WHERE d.status = 'open'""" + (" AND d.dlq_id = ANY(%s)" if dlq_ids else "")
        items = conn.execute(q, (dlq_ids,) if dlq_ids else None).fetchall()
        before = run.metrics.records_rejected
        resolved = failed = 0
        for it in items:
            run.metrics.records_rejected = before
            conn.execute("UPDATE raw.records SET process_status='pending' WHERE raw_id=%s", (it["raw_id"],))
            process_pending(run, [it["raw_id"]])
            status = conn.execute("SELECT process_status FROM raw.records WHERE raw_id=%s", (it["raw_id"],)).fetchone()["process_status"]  # type: ignore[index]
            if status == "loaded":
                resolved += 1
                conn.execute("UPDATE ops.dead_letter SET status='resolved', resolved_at=now(), retry_count=retry_count+1 WHERE dlq_id=%s", (it["dlq_id"],))
            else:
                failed += 1
                conn.execute("UPDATE ops.dead_letter SET status='retry_failed', retry_count=retry_count+1 WHERE dlq_id=%s", (it["dlq_id"],))
                # the re-run produced a fresh DLQ row with the latest error; keep only the original
                conn.execute("DELETE FROM ops.dead_letter WHERE run_id=%s AND record_locator=(SELECT record_locator FROM raw.records WHERE raw_id=%s)",
                             (run.run_id, it["raw_id"]))
        conn.commit()
        run.metrics.records_rejected = failed
        out = run.finish()
        out["resolved"], out["still_failing"] = resolved, failed
        return out
