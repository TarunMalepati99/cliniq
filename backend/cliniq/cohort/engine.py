"""Cohort workflow: draft → validate → human approval → read-only execution → explainable results.

Safety invariants enforced here (and tested):
- Only definitions in status 'approved' can execute.
- Approval binds to the exact SQL hash the reviewer saw; any change requires re-approval.
- At execution the definition is recompiled; if the SQL hash differs from the approved one
  (e.g. compiler upgraded), execution is refused.
- SQL runs on a read-only transaction as a SELECT-only database role with a timeout.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import Counter, defaultdict
from typing import Any

from psycopg.types.json import Jsonb

from .. import vocabulary as V
from ..config import get_settings
from ..db import connect, fetch_one, read_only
from .compiler import COMPILER_VERSION, CompiledQuery, classify, compile_definition, evidence_to_status
from .models import CohortDefinition
from .sqlguard import explain, validate_sql


class WorkflowError(Exception):
    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def data_version() -> str:
    """Fingerprint of the analytical dataset: latest successful ingest + table row counts."""
    row = fetch_one("""
        SELECT (SELECT coalesce(max(finished_at)::text, '') FROM ops.pipeline_runs
                 WHERE pipeline IN ('ingest','dlq_retry') AND status IN ('succeeded','partial')) AS last_ingest,
               (SELECT count(*) FROM clinical.person) AS person, (SELECT count(*) FROM clinical.condition_occurrence) AS cond,
               (SELECT count(*) FROM clinical.measurement) AS meas, (SELECT count(*) FROM clinical.drug_exposure) AS drug,
               (SELECT count(*) FROM clinical.genomic_variant) AS gen, (SELECT count(*) FROM clinical.procedure_occurrence) AS proc,
               (SELECT count(*) FROM clinical.note_fact) AS nf""")
    return "dv-" + hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _compile_and_validate(defn: CohortDefinition) -> tuple[CompiledQuery, dict[str, Any]]:
    q = compile_definition(defn, get_settings().as_of_date)
    rep = validate_sql(q.sql)
    with read_only() as conn:
        ok, detail = explain(q.sql, q.params, conn)
    rep.add("explain_as_readonly_role", ok, detail)
    return q, {"ok": rep.ok, "checks": rep.checks}


def create_definition(defn: CohortDefinition, user: str, nl_request: str | None = None,
                      extraction: dict[str, Any] | None = None, extractor_version: str | None = None) -> dict[str, Any]:
    q, validation = _compile_and_validate(defn)
    did = str(uuid.uuid4())
    with connect() as conn:
        conn.execute("""INSERT INTO cohort.definitions (definition_id, name, nl_request, definition, definition_sha256, extraction,
                        extractor_version, compiler_version, compiled_sql, compiled_params, sql_sha256, validation, status, created_by)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'draft',%s)""",
                     (did, defn.name, nl_request, Jsonb(defn.model_dump(mode="json")), defn.sha256(), Jsonb(extraction) if extraction else None,
                      extractor_version, q.compiler_version, q.sql, Jsonb({k: _jsonable(v) for k, v in q.params.items()}),
                      q.sha256, Jsonb(validation), user))
    return get_definition(did)


def _jsonable(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def get_definition(definition_id: str) -> dict[str, Any]:
    d = fetch_one("SELECT * FROM cohort.definitions WHERE definition_id=%s", (definition_id,))
    if d is None:
        raise WorkflowError("definition not found", 404)
    defn = CohortDefinition.model_validate(d["definition"])
    q = compile_definition(defn, get_settings().as_of_date)
    d["criteria"] = [m.__dict__ for m in q.criteria]
    d["approvals"] = list(_approvals(definition_id))
    return d


def _approvals(definition_id: str) -> list[dict[str, Any]]:
    with connect() as c:
        return list(c.execute("SELECT * FROM cohort.approvals WHERE definition_id=%s ORDER BY decided_at", (definition_id,)))


def decide(definition_id: str, user: str, reviewed_sql_sha256: str, approve: bool, comment: str | None = None) -> dict[str, Any]:
    d = get_definition(definition_id)
    if d["status"] != "draft":
        raise WorkflowError(f"definition is {d['status']}; only drafts can be reviewed")
    if reviewed_sql_sha256 != d["sql_sha256"]:
        raise WorkflowError("reviewed SQL hash does not match the current compiled SQL; reload and review again")
    if approve and not d["validation"]["ok"]:
        raise WorkflowError("SQL validation failed; this definition cannot be approved", 422)
    if approve and get_settings().require_distinct_approver and user == d["created_by"]:
        raise WorkflowError("four-eyes rule: the author cannot approve their own query", 403)
    decision = "approved" if approve else "rejected"
    with connect() as conn:
        conn.execute("INSERT INTO cohort.approvals (definition_id, sql_sha256, decision, decided_by, comment) VALUES (%s,%s,%s,%s,%s)",
                     (definition_id, d["sql_sha256"], decision, user, comment))
        conn.execute("UPDATE cohort.definitions SET status=%s WHERE definition_id=%s", (decision, definition_id))
    return get_definition(definition_id)


def execute(definition_id: str, user: str) -> dict[str, Any]:
    d = get_definition(definition_id)
    if d["status"] != "approved":
        raise WorkflowError("query has not been approved by a human reviewer", 403)
    approved = [a for a in d["approvals"] if a["decision"] == "approved"]
    if not approved or approved[-1]["sql_sha256"] != d["sql_sha256"]:
        raise WorkflowError("approval does not cover the current SQL", 403)
    defn = CohortDefinition.model_validate(d["definition"])
    q = compile_definition(defn, get_settings().as_of_date)
    if q.sha256 != d["sql_sha256"]:
        raise WorkflowError(f"recompiled SQL differs from approved SQL (compiler {COMPILER_VERSION} vs {d['compiler_version']});"
                            " create a new definition and re-approve", 409)
    run_id = str(uuid.uuid4())
    dv = data_version()
    with connect() as conn:
        conn.execute("INSERT INTO cohort.runs (cohort_run_id, definition_id, sql_sha256, data_version, as_of_date, executed_by, status)"
                     " VALUES (%s,%s,%s,%s,%s,%s,'running')", (run_id, definition_id, q.sha256, dv, q.as_of_date, user))
    t0 = time.monotonic()
    try:
        with read_only() as conn:
            rows = conn.execute(q.sql, q.params).fetchall()  # type: ignore[arg-type]
        query_ms = int((time.monotonic() - t0) * 1000)
        patients, summary = summarize(rows, q)
        summary["query_ms"] = query_ms
        summary["distributions"] = distributions([p["person_id"] for p in patients if p["classification"] != "ineligible"])
        result_sha = hashlib.sha256(json.dumps(sorted((p["person_id"], p["classification"]) for p in patients)).encode()).hexdigest()
        with connect() as conn:
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO cohort.run_patients (cohort_run_id, person_id, classification, criteria) VALUES (%s,%s,%s,%s)",
                                [(run_id, p["person_id"], p["classification"], Jsonb(p["criteria"])) for p in patients])
            conn.execute("UPDATE cohort.runs SET status='succeeded', duration_ms=%s, summary=%s, result_sha256=%s WHERE cohort_run_id=%s",
                         (int((time.monotonic() - t0) * 1000), Jsonb(summary), result_sha, run_id))
    except Exception as exc:
        with connect() as conn:
            conn.execute("UPDATE cohort.runs SET status='failed', duration_ms=%s, error=%s WHERE cohort_run_id=%s",
                         (int((time.monotonic() - t0) * 1000), f"{type(exc).__name__}: {exc}"[:2000], run_id))
        raise
    return get_run(run_id)


def summarize(rows: list[dict[str, Any]], q: CompiledQuery) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    patients = []
    per_crit: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        crit = []
        for m in q.criteria:
            ev = r[f"{m.cid}_evidence"]
            per_crit[m.cid][ev] += 1
            crit.append({"cid": m.cid, "status": evidence_to_status(ev), "evidence": ev, "detail": r[f"{m.cid}_detail"]})
        patients.append({"person_id": r["person_id"], "classification": classify(r, q.criteria), "criteria": crit})
    counts = Counter(p["classification"] for p in patients)

    # Attrition funnel over inclusion criteria (in order), then exclusions as a block.
    funnel = [{"step": "All patients in dataset", "cid": None, "strict": len(rows), "with_unknown": len(rows)}]
    strict = set(r["person_id"] for r in rows)
    lenient = set(strict)
    by_id = {r["person_id"]: r for r in rows}
    for m in q.criteria:
        if m.role != "inclusion":
            continue
        strict = {pid for pid in strict if evidence_to_status(by_id[pid][f"{m.cid}_evidence"]) == "met"}
        lenient = {pid for pid in lenient if evidence_to_status(by_id[pid][f"{m.cid}_evidence"]) != "not_met"}
        funnel.append({"step": m.label, "cid": m.cid, "strict": len(strict), "with_unknown": len(lenient)})
    excl = [m for m in q.criteria if m.role == "exclusion"]
    if excl:
        strict = {pid for pid in strict if all(evidence_to_status(by_id[pid][f"{m.cid}_evidence"]) == "not_met" for m in excl)}
        lenient = {pid for pid in lenient if all(evidence_to_status(by_id[pid][f"{m.cid}_evidence"]) != "met" for m in excl)}
        funnel.append({"step": "After exclusions", "cid": "exclusions", "strict": len(strict), "with_unknown": len(lenient)})

    # Missing information among patients not otherwise disqualified.
    pot = [p for p in patients if p["classification"] == "potentially_eligible"]
    missing = Counter(c["cid"] for p in pot for c in p["criteria"] if c["status"] == "unknown")
    weak = sum(1 for p in patients if p["classification"] == "ineligible"
               and all(c["evidence"] != "negative" for c in p["criteria"]
                       if (c["status"] == "not_met" and _role(q, c["cid"]) == "inclusion"))
               and not any(c["status"] == "met" for c in p["criteria"] if _role(q, c["cid"]) == "exclusion"))
    criteria_stats = []
    for m in q.criteria:
        ev = per_crit[m.cid]
        n = len(rows) or 1
        criteria_stats.append({**m.__dict__, "evidence_counts": dict(ev), "completeness": round(1 - ev.get("unknown", 0) / n, 4)})
    return patients, {
        "total_patients": len(rows), "eligible": counts.get("eligible", 0),
        "potentially_eligible": counts.get("potentially_eligible", 0), "ineligible": counts.get("ineligible", 0),
        "ineligible_only_by_absent_evidence": weak,
        "funnel": funnel, "criteria": criteria_stats,
        "missing_information": [{"cid": cid, "label": _label(q, cid), "patients_blocked": n} for cid, n in missing.most_common()],
        "as_of_date": q.as_of_date.isoformat(), "compiler_version": q.compiler_version,
    }


def _role(q: CompiledQuery, cid: str) -> str:
    return next(m.role for m in q.criteria if m.cid == cid)


def _label(q: CompiledQuery, cid: str) -> str:
    return next(m.label for m in q.criteria if m.cid == cid)


def distributions(person_ids: list[str]) -> dict[str, Any]:
    """Fixed, non-generated SQL over the eligible + potentially eligible set."""
    if not person_ids:
        return {"geography": [], "biomarkers": [], "diagnoses": [], "treatments": [], "sex": [], "age_bands": []}
    with read_only() as c:
        def q(sql: str) -> list[dict[str, Any]]:
            return list(c.execute(sql, {"ids": person_ids}).fetchall())  # type: ignore[arg-type]
        geography = q("SELECT coalesce(state,'unknown') AS key, count(*) AS n FROM clinical.person WHERE person_id = ANY(%(ids)s) GROUP BY 1 ORDER BY 2 DESC")
        sex = q("SELECT coalesce(sex,'unknown') AS key, count(*) AS n FROM clinical.person WHERE person_id = ANY(%(ids)s) GROUP BY 1 ORDER BY 2 DESC")
        ages = q("""SELECT CASE WHEN birth_date IS NULL THEN 'unknown' ELSE (floor(date_part('year', age(%(asof)s::date, birth_date))/10)*10)::int::text || 's' END AS key,
                   count(*) AS n FROM clinical.person WHERE person_id = ANY(%(ids)s) GROUP BY 1 ORDER BY 1""".replace("%(asof)s", f"'{get_settings().as_of_date.isoformat()}'"))
        bms = q("""SELECT biomarker_key AS key, count(*) FILTER (WHERE result='detected') AS detected,
                   count(*) FILTER (WHERE result='not_detected') AS not_detected, count(*) FILTER (WHERE result='indeterminate') AS indeterminate
                   FROM (SELECT DISTINCT ON (person_id, biomarker_key) * FROM clinical.genomic_variant WHERE person_id = ANY(%(ids)s)
                         ORDER BY person_id, biomarker_key, reported_date DESC) g GROUP BY 1 ORDER BY 2 DESC, 1""")
        dx = q("""SELECT code AS key, count(DISTINCT person_id) AS n FROM clinical.condition_occurrence WHERE person_id = ANY(%(ids)s)
                  GROUP BY 1 ORDER BY 2 DESC LIMIT 12""")
        tx = q("""SELECT coalesce(drug_class,'unmapped') AS key, count(DISTINCT person_id) AS n FROM clinical.drug_exposure
                  WHERE person_id = ANY(%(ids)s) GROUP BY 1 ORDER BY 2 DESC""")
    names = {c: n for c, n in V.ICD10_CODES.items()}
    for r in dx:
        r["label"] = names.get(r["key"], "Unmapped code")
    for r in tx:
        r["label"] = V.DRUG_CLASSES.get(r["key"], (r["key"],))[0]
    for r in bms:
        r["label"] = V.BIOMARKERS[r["key"]].label if r["key"] in V.BIOMARKERS else r["key"]
    return {"geography": geography, "sex": sex, "age_bands": ages, "biomarkers": bms, "diagnoses": dx, "treatments": tx,
            "population": len(person_ids)}


def get_run(run_id: str) -> dict[str, Any]:
    r = fetch_one("SELECT * FROM cohort.runs WHERE cohort_run_id=%s", (run_id,))
    if r is None:
        raise WorkflowError("run not found", 404)
    return r


def run_patients(run_id: str, classification: str | None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    with connect() as c:
        sql = "SELECT person_id, classification, criteria FROM cohort.run_patients WHERE cohort_run_id=%s"
        params: list[Any] = [run_id]
        if classification:
            sql += " AND classification=%s"
            params.append(classification)
        sql += " ORDER BY person_id LIMIT %s OFFSET %s"
        params += [limit, offset]
        return list(c.execute(sql, params))


def reproduce(run_id: str, user: str) -> dict[str, Any]:
    """Re-execute a saved run's definition and compare results."""
    orig = get_run(run_id)
    new = execute(str(orig["definition_id"]), user)
    return {"original_run_id": run_id, "new_run_id": str(new["cohort_run_id"]),
            "same_sql": orig["sql_sha256"] == new["sql_sha256"], "same_data_version": orig["data_version"] == new["data_version"],
            "identical_result": orig["result_sha256"] == new["result_sha256"],
            "original_result_sha256": orig["result_sha256"], "new_result_sha256": new["result_sha256"]}
