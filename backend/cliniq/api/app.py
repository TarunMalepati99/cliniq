"""CLINIQ HTTP API (FastAPI).

Roles: viewer < researcher < admin.
  viewer      read dashboards, data explorer, cohort results, evidence, evaluation reports
  researcher  + extract criteria, create/review/execute cohort queries
  admin       + ingest data, run dbt / re-index / retry dead letters / run benchmarks

Auth is a bearer token mapped (by SHA-256) to a user and role via CLINIQ_API_TOKENS; in
CLINIQ_ENV=dev, documented non-secret dev tokens are also accepted. This is a prototype
auth layer — see docs/limitations.md for what a production deployment would need.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import vocabulary as V
from ..cohort import engine
from ..cohort.models import CohortDefinition
from ..config import REPO_ROOT, get_settings, hash_token
from ..db import connect, fetch_all, fetch_one
from ..evaluation.run import run_suites
from ..extraction.llm import get_extractor
from ..ingest.dbt_runner import run_dbt
from ..ingest.pipeline import ingest_paths, retry_dead_letters
from ..observability.logging import configure_logging, request_id_var, user_var
from ..observability.monitoring import overview, pipeline_run
from ..retrieval import evidence as ev
from ..retrieval.index import get_index, index_corpus
from . import jobs

log = logging.getLogger("cliniq.api")
DISCLAIMER = ("CLINIQ is a research and engineering prototype operating on synthetic data. It is not a clinically "
              "validated product and must not be used for medical decision-making or patient care.")
ROLE_RANK = {"viewer": 0, "researcher": 1, "admin": 2}
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
ALLOWED_UPLOAD = re.compile(r"^[A-Za-z0-9._\-]{1,120}\.(json|ndjson|csv|jsonl)$")

app = FastAPI(title="CLINIQ API", version="0.1.0", description=DISCLAIMER,
              openapi_tags=[{"name": t} for t in ["system", "ingestion", "data", "cohort", "evidence", "evaluation", "monitoring"]])
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"], allow_methods=["*"],
                   allow_headers=["*"], expose_headers=["X-Request-ID"])


@app.on_event("startup")
def _startup() -> None:
    configure_logging()


class User(BaseModel):
    user_id: str
    role: str


def current_user(authorization: str | None = Header(default=None)) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    entry = get_settings().api_tokens.get(hash_token(authorization.split(" ", 1)[1].strip()))
    if entry is None:
        raise HTTPException(401, "invalid token")
    user_var.set(entry[0])
    return User(user_id=entry[0], role=entry[1])


def require(role: str) -> Any:
    def dep(user: User = Depends(current_user)) -> User:
        if ROLE_RANK[user.role] < ROLE_RANK[role]:
            raise HTTPException(403, f"requires role '{role}' (you are '{user.role}')")
        return user
    return dep


@app.middleware("http")
async def request_context(request: Request, call_next: Any) -> Any:
    rid = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    try:
        rid = str(uuid.UUID(rid))
    except ValueError:
        rid = str(uuid.uuid4())
    request_id_var.set(rid)
    t0 = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception:
        log.exception("unhandled error", extra={"path": request.url.path})
        response = JSONResponse({"detail": "internal error", "request_id": rid}, status_code=500)
    dur = int((time.perf_counter() - t0) * 1000)
    response.headers["X-Request-ID"] = rid
    if request.url.path.startswith("/api/"):
        try:
            with connect() as c:
                c.execute("INSERT INTO ops.api_requests (request_id, method, path, status_code, duration_ms, user_id) VALUES (%s,%s,%s,%s,%s,%s)",
                          (rid, request.method, request.url.path[:300], status, dur, user_var.get()))
        except Exception:  # noqa: BLE001 - telemetry must not break requests
            log.warning("could not record api request")
        log.info("request", extra={"method": request.method, "path": request.url.path, "status": status, "duration_ms": dur})
    return response


@app.exception_handler(engine.WorkflowError)
async def _workflow_error(request: Request, exc: engine.WorkflowError) -> JSONResponse:
    return JSONResponse({"detail": str(exc), "request_id": request_id_var.get()}, status_code=exc.status_code)


# ---------------------------------------------------------------- system
@app.get("/api/health", tags=["system"])
def health() -> dict[str, Any]:
    try:
        fetch_one("SELECT 1 AS ok")
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "database": db_ok, "disclaimer": DISCLAIMER}


@app.get("/api/me", tags=["system"])
def me(user: User = Depends(current_user)) -> dict[str, Any]:
    return {**user.model_dump(), "disclaimer": DISCLAIMER}


@app.get("/api/vocabulary", tags=["system"])
def vocabulary(_: User = Depends(require("viewer"))) -> dict[str, Any]:
    return {
        "conditions": {k: v.label for k, v in V.CONDITION_SETS.items()}, "biomarkers": {k: v.label for k, v in V.BIOMARKERS.items()},
        "labs": {k: {"label": v.label, "unit": v.unit} for k, v in V.LABS.items()},
        "drug_classes": {**{k: v[0] for k, v in V.DRUG_CLASSES.items()}, **{k: v[0] for k, v in V.VIRTUAL_DRUG_CLASSES.items()}},
        "drugs": sorted(V.DRUG_TO_CLASS), "procedure_classes": {k: v[0] for k, v in V.PROCEDURE_CLASSES.items()}, "states": V.US_STATES,
        "extractors": ["rules-v2", "rules-v1"] + (["llm"] if get_settings().anthropic_api_key else [])
                      + (["gemini"] if os.environ.get("GEMINI_API_KEY") else []),
    }


def _dispatch(kind: str, user: User, wait: bool, fn: Any) -> Any:
    """Long operations run as background jobs (poll GET /api/jobs/{id}); wait=true runs inline."""
    if wait:
        return fn()
    return JSONResponse(jobs.submit(kind, user.user_id, fn), status_code=202)


@app.get("/api/jobs", tags=["system"])
def list_jobs(_: User = Depends(current_user)) -> list[dict[str, Any]]:
    return jobs.recent()


@app.get("/api/jobs/{job_id}", tags=["system"])
def job_status(job_id: uuid.UUID, _: User = Depends(current_user)) -> dict[str, Any]:
    j = jobs.get(str(job_id))
    if j is None:
        raise HTTPException(404, "job not found (jobs are kept in memory and reset on restart)")
    return {k: v for k, v in j.items() if k != "trace"}


# ---------------------------------------------------------------- ingestion (admin)
@app.post("/api/ingest/upload", tags=["ingestion"])
async def upload(files: list[UploadFile] = File(...), wait: bool = False, user: User = Depends(require("admin"))) -> Any:
    dest = get_settings().data_dir / "uploads" / time.strftime("%Y%m%dT%H%M%S") / uuid.uuid4().hex[:8]
    dest.mkdir(parents=True, exist_ok=True)
    paths = []
    for f in files:
        name = Path(f.filename or "").name
        if not ALLOWED_UPLOAD.match(name):
            raise HTTPException(400, f"unsupported file name/type: {name!r} (allowed: .json .ndjson .csv .jsonl)")
        target = dest / name
        size = 0
        with target.open("wb") as out:
            while chunk := await f.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    shutil.rmtree(dest, ignore_errors=True)
                    raise HTTPException(413, f"{name} exceeds {MAX_UPLOAD_BYTES // 1024 // 1024} MB")
                out.write(chunk)
        paths.append(target)
    return _dispatch("ingest_upload", user, wait, lambda: ingest_paths(paths, triggered_by=user.user_id, source_system="upload"))


@app.post("/api/ingest/sample", tags=["ingestion"])
def ingest_sample(wait: bool = False, user: User = Depends(require("admin"))) -> Any:
    sample = get_settings().data_dir / "synthetic"
    if not sample.exists():
        raise HTTPException(404, "sample dataset not found; run `cliniq generate` first")
    return _dispatch("ingest_sample", user, wait, lambda: ingest_paths([sample], triggered_by=user.user_id))


@app.post("/api/pipeline/dbt", tags=["ingestion"])
def dbt_build(wait: bool = False, user: User = Depends(require("admin"))) -> Any:
    return _dispatch("dbt_build", user, wait, lambda: run_dbt(triggered_by=user.user_id))


@app.post("/api/evidence/reindex", tags=["evidence"])
def reindex(wait: bool = False, user: User = Depends(require("admin"))) -> Any:
    return _dispatch("rag_index", user, wait, lambda: index_corpus(get_settings().corpus_dir, triggered_by=user.user_id))


@app.get("/api/pipeline/runs", tags=["ingestion"])
def pipeline_runs(_: User = Depends(require("viewer")), limit: int = Query(50, le=500)) -> list[dict[str, Any]]:
    return fetch_all("SELECT run_id, pipeline, status, triggered_by, started_at, finished_at, duration_ms, metrics, error"
                     " FROM ops.pipeline_runs ORDER BY started_at DESC LIMIT %s", (limit,))


@app.get("/api/pipeline/runs/{run_id}", tags=["ingestion"])
def pipeline_run_detail(run_id: uuid.UUID, _: User = Depends(require("viewer"))) -> dict[str, Any]:
    r = pipeline_run(str(run_id))
    if r is None:
        raise HTTPException(404, "run not found")
    return r


@app.get("/api/dead-letter", tags=["ingestion"])
def dead_letter(status: str | None = None, limit: int = Query(200, le=1000), _: User = Depends(require("viewer"))) -> list[dict[str, Any]]:
    sql = """SELECT d.dlq_id, d.run_id, f.file_name, d.record_locator, d.resource_type, d.stage, d.error_code, d.error_detail,
                    d.status, d.retry_count, d.created_at, d.payload FROM ops.dead_letter d LEFT JOIN ops.source_files f USING (source_file_id)"""
    params: list[Any] = []
    if status:
        sql += " WHERE d.status = %s"
        params.append(status)
    return fetch_all(sql + " ORDER BY d.dlq_id DESC LIMIT %s", (*params, limit))


class RetryRequest(BaseModel):
    dlq_ids: list[int] | None = None


@app.post("/api/dead-letter/retry", tags=["ingestion"])
def dead_letter_retry(body: RetryRequest, wait: bool = False, user: User = Depends(require("admin"))) -> Any:
    return _dispatch("dlq_retry", user, wait, lambda: retry_dead_letters(body.dlq_ids, user.user_id))


# ---------------------------------------------------------------- data explorer
@app.get("/api/data/summary", tags=["data"])
def data_summary(_: User = Depends(require("viewer"))) -> dict[str, Any]:
    o = overview()
    with connect() as c:
        dx = list(c.execute("""SELECT c.code, coalesce(k.name,'Unmapped code') AS name, count(DISTINCT c.person_id) AS persons
                               FROM clinical.condition_occurrence c LEFT JOIN clinical.concept k ON k.vocabulary='ICD10CM' AND k.code=c.code
                               GROUP BY 1,2 ORDER BY 3 DESC LIMIT 15"""))
        bm = list(c.execute("""SELECT biomarker_key, count(DISTINCT person_id) FILTER (WHERE result='detected') AS detected,
                               count(DISTINCT person_id) FILTER (WHERE result='not_detected') AS not_detected,
                               count(DISTINCT person_id) FILTER (WHERE result='indeterminate') AS indeterminate
                               FROM clinical.genomic_variant GROUP BY 1 ORDER BY 2 DESC"""))
        sources = list(c.execute("SELECT source_format, count(*) AS files, sum(size_bytes) AS bytes FROM ops.source_files GROUP BY 1"))
    return {"row_counts": o["row_counts"], "domain_coverage": o["domain_coverage"], "missingness": o["missingness"],
            "top_diagnoses": dx, "biomarkers": bm, "sources": sources, "freshness": o["freshness"]}


@app.get("/api/data/patients", tags=["data"])
def patients(q: str | None = None, cancer: str | None = None, limit: int = Query(50, le=200), offset: int = 0,
             _: User = Depends(require("viewer"))) -> dict[str, Any]:
    where, params = [], []
    if q:
        where.append("p.person_id ILIKE %s")
        params.append(f"%{q}%")
    if cancer:
        where.append("EXISTS (SELECT 1 FROM clinical.condition_occurrence c WHERE c.person_id=p.person_id AND c.code LIKE %s)")
        params.append(cancer.replace("%", "") + "%")
    w = ("WHERE " + " AND ".join(where)) if where else ""
    rows = fetch_all(f"""SELECT p.person_id, p.birth_date, p.sex, p.state, p.smoking_status,
            (SELECT string_agg(DISTINCT code, ', ') FROM clinical.condition_occurrence c WHERE c.person_id=p.person_id) AS conditions,
            (SELECT count(*) FROM clinical.genomic_variant g WHERE g.person_id=p.person_id) AS biomarker_results,
            (SELECT count(*) FROM clinical.drug_exposure d WHERE d.person_id=p.person_id) AS drug_exposures,
            (SELECT count(*) FROM clinical.measurement m WHERE m.person_id=p.person_id) AS measurements
            FROM clinical.person p {w} ORDER BY p.person_id LIMIT %s OFFSET %s""", (*params, limit, offset))
    total = fetch_one(f"SELECT count(*) AS n FROM clinical.person p {w}", params or None)
    return {"total": total["n"] if total else 0, "items": rows}


@app.get("/api/data/patients/{person_id}", tags=["data"])
def patient_detail(person_id: str, _: User = Depends(require("viewer"))) -> dict[str, Any]:
    p = fetch_one("SELECT person_id, birth_date, sex, state, smoking_status, source_record_id, run_id FROM clinical.person WHERE person_id=%s", (person_id,))
    if p is None:
        raise HTTPException(404, "patient not found")
    with connect() as c:
        def q(sql: str) -> list[dict[str, Any]]:
            return list(c.execute(sql, (person_id,)))
        return {
            "person": p,
            "conditions": q("""SELECT c.condition_id, c.code, coalesce(k.name,'Unmapped code') AS name, c.histology_code, c.onset_date, c.source_record_id
                               FROM clinical.condition_occurrence c LEFT JOIN clinical.concept k ON k.vocabulary=c.vocabulary AND k.code=c.code
                               WHERE c.person_id=%s ORDER BY c.onset_date"""),
            "biomarkers": q("SELECT variant_result_id, gene, biomarker_key, hgvs_p, result, assay, reported_date, source_record_id FROM clinical.genomic_variant WHERE person_id=%s ORDER BY reported_date, biomarker_key"),
            "measurements": q("SELECT measurement_id, lab_key, value_numeric, unit, source_value, source_unit, measured_at, source_record_id FROM clinical.measurement WHERE person_id=%s ORDER BY measured_at DESC, lab_key LIMIT 200"),
            "drugs": q("SELECT drug_exposure_id, drug_name, drug_class, start_date, end_date, status, source_record_id FROM clinical.drug_exposure WHERE person_id=%s ORDER BY start_date"),
            "procedures": q("SELECT procedure_id, code, procedure_class, performed_date, source_record_id FROM clinical.procedure_occurrence WHERE person_id=%s"),
            "notes": q("""SELECT n.note_id, n.note_date, n.note_type, n.note_text, n.source_record_id,
                          (SELECT jsonb_agg(jsonb_build_object('key', f.fact_key, 'value', f.fact_value, 'start', f.span_start, 'end', f.span_end))
                           FROM clinical.note_fact f WHERE f.note_id=n.note_id) AS facts FROM clinical.note n WHERE n.person_id=%s"""),
        }


@app.get("/api/data/lineage/{raw_id}", tags=["data"])
def lineage(raw_id: int, _: User = Depends(require("viewer"))) -> dict[str, Any]:
    r = fetch_one("""SELECT r.raw_id, r.record_locator, r.record_type, r.source_key, r.content_sha256, r.process_status, r.landed_at, r.payload,
                            f.file_name, f.file_sha256, f.source_format, f.source_system, f.received_at,
                            p.run_id, p.pipeline, p.status AS run_status, p.started_at AS run_started_at, p.params->>'transform_version' AS transform_version
                     FROM raw.records r JOIN ops.source_files f USING (source_file_id) JOIN ops.pipeline_runs p ON p.run_id = r.run_id
                     WHERE r.raw_id=%s""", (raw_id,))
    if r is None:
        raise HTTPException(404, "raw record not found")
    return r


# ---------------------------------------------------------------- cohort
class ExtractRequest(BaseModel):
    text: str = Field(min_length=3, max_length=4000)
    extractor: Literal["rules-v2", "rules-v1", "llm", "gemini"] = "rules-v2"


@app.post("/api/cohort/extract", tags=["cohort"])
def extract(body: ExtractRequest, _: User = Depends(require("researcher"))) -> dict[str, Any]:
    if body.extractor == "gemini" and not os.environ.get("GEMINI_API_KEY"):
        raise HTTPException(503, "Gemini is not enabled; start the API with `cliniq serve --gemini` to enter a key")
    try:
        ex = _gemini() if body.extractor == "gemini" else get_extractor(body.extractor)
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from None
    r = ex.extract(body.text)
    return r.__dict__


_gemini_cache: dict[str, Any] = {}


def _gemini() -> Any:
    """One validated Gemini client per process (model check happens once, non-interactively)."""
    if "x" not in _gemini_cache:
        from ..extraction.gemini import GeminiExtractor
        _gemini_cache["x"] = GeminiExtractor(interactive=False)
    return _gemini_cache["x"]


class CreateDefinition(BaseModel):
    definition: CohortDefinition
    nl_request: str | None = None
    extraction: dict[str, Any] | None = None
    extractor_version: str | None = None


@app.post("/api/cohort/definitions", tags=["cohort"])
def create_definition(body: CreateDefinition, user: User = Depends(require("researcher"))) -> dict[str, Any]:
    return engine.create_definition(body.definition, user.user_id, body.nl_request, body.extraction, body.extractor_version)


@app.get("/api/cohort/definitions", tags=["cohort"])
def list_definitions(_: User = Depends(require("viewer"))) -> list[dict[str, Any]]:
    return fetch_all("""SELECT d.definition_id, d.name, d.status, d.created_by, d.created_at, d.extractor_version, d.sql_sha256,
                        (SELECT count(*) FROM cohort.runs r WHERE r.definition_id=d.definition_id) AS runs
                        FROM cohort.definitions d ORDER BY d.created_at DESC LIMIT 200""")


@app.get("/api/cohort/definitions/{definition_id}", tags=["cohort"])
def get_definition(definition_id: uuid.UUID, _: User = Depends(require("viewer"))) -> dict[str, Any]:
    return engine.get_definition(str(definition_id))


class Review(BaseModel):
    sql_sha256: str = Field(min_length=64, max_length=64)
    approve: bool
    comment: str | None = Field(default=None, max_length=2000)


@app.post("/api/cohort/definitions/{definition_id}/review", tags=["cohort"])
def review(definition_id: uuid.UUID, body: Review, user: User = Depends(require("researcher"))) -> dict[str, Any]:
    return engine.decide(str(definition_id), user.user_id, body.sql_sha256, body.approve, body.comment)


@app.post("/api/cohort/definitions/{definition_id}/execute", tags=["cohort"])
def execute(definition_id: uuid.UUID, user: User = Depends(require("researcher"))) -> dict[str, Any]:
    return engine.execute(str(definition_id), user.user_id)


@app.get("/api/cohort/runs", tags=["cohort"])
def list_runs(_: User = Depends(require("viewer"))) -> list[dict[str, Any]]:
    return fetch_all("""SELECT r.cohort_run_id, r.definition_id, d.name, r.status, r.executed_by, r.started_at, r.duration_ms, r.data_version,
                        r.result_sha256, r.summary->'eligible' AS eligible, r.summary->'potentially_eligible' AS potentially_eligible
                        FROM cohort.runs r JOIN cohort.definitions d USING (definition_id) ORDER BY r.started_at DESC LIMIT 200""")


@app.get("/api/cohort/runs/{run_id}", tags=["cohort"])
def get_run(run_id: uuid.UUID, _: User = Depends(require("viewer"))) -> dict[str, Any]:
    r = engine.get_run(str(run_id))
    r["definition"] = engine.get_definition(str(r["definition_id"]))
    return r


@app.get("/api/cohort/runs/{run_id}/patients", tags=["cohort"])
def run_patients(run_id: uuid.UUID, classification: Literal["eligible", "potentially_eligible", "ineligible"] | None = None,
                 limit: int = Query(100, le=500), offset: int = 0, _: User = Depends(require("viewer"))) -> list[dict[str, Any]]:
    return engine.run_patients(str(run_id), classification, limit, offset)


@app.post("/api/cohort/runs/{run_id}/reproduce", tags=["cohort"])
def reproduce(run_id: uuid.UUID, user: User = Depends(require("researcher"))) -> dict[str, Any]:
    return engine.reproduce(str(run_id), user.user_id)


# ---------------------------------------------------------------- evidence
@app.get("/api/evidence/search", tags=["evidence"])
def evidence_search(q: str = Query(min_length=2, max_length=500), k: int = Query(5, ge=1, le=20),
                    mode: Literal["hybrid", "bm25", "dense"] = "hybrid", user: User = Depends(require("viewer"))) -> dict[str, Any]:
    if get_index() is None:
        raise HTTPException(409, "evidence index is empty; run POST /api/evidence/reindex")
    return ev.search(q, k=k, mode=mode, user=user.user_id)


class EvidenceForDefinition(BaseModel):
    definition: CohortDefinition | None = None
    definition_id: uuid.UUID | None = None


@app.post("/api/evidence/definition", tags=["evidence"])
def evidence_definition(body: EvidenceForDefinition, user: User = Depends(require("viewer"))) -> dict[str, Any]:
    if get_index() is None:
        raise HTTPException(409, "evidence index is empty; run POST /api/evidence/reindex")
    defn = body.definition
    if defn is None and body.definition_id:
        defn = CohortDefinition.model_validate(engine.get_definition(str(body.definition_id))["definition"])
    if defn is None:
        raise HTTPException(422, "provide definition or definition_id")
    return ev.evidence_for_definition(defn, user=user.user_id)


@app.get("/api/evidence/documents/{doc_id}", tags=["evidence"])
def evidence_document(doc_id: str, _: User = Depends(require("viewer"))) -> dict[str, Any]:
    d = fetch_one("SELECT doc_id, title, doc_type, source, license, body, indexed_at FROM rag.documents WHERE doc_id=%s", (doc_id,))
    if d is None:
        raise HTTPException(404, "document not found")
    d["chunks"] = fetch_all("SELECT chunk_id, ordinal, section, char_start, char_end FROM rag.chunks WHERE doc_id=%s ORDER BY ordinal", (doc_id,))
    return d


# ---------------------------------------------------------------- evaluation
@app.get("/api/eval/runs", tags=["evaluation"])
def eval_runs(_: User = Depends(require("viewer"))) -> list[dict[str, Any]]:
    return fetch_all("""SELECT eval_run_id, suite, system_version, prompt_sha256, benchmark_sha256, data_version, code_version, started_at,
                        duration_ms, metrics, n_items, report_path FROM ops.eval_runs ORDER BY started_at DESC""")


@app.get("/api/eval/runs/{eval_run_id}", tags=["evaluation"])
def eval_run(eval_run_id: uuid.UUID, only_failures: bool = False, _: User = Depends(require("viewer"))) -> dict[str, Any]:
    r = fetch_one("SELECT * FROM ops.eval_runs WHERE eval_run_id=%s", (str(eval_run_id),))
    if r is None:
        raise HTTPException(404, "eval run not found")
    sql = "SELECT item_id, passed, failure_category, details FROM ops.eval_item_results WHERE eval_run_id=%s"
    r["items"] = fetch_all(sql + (" AND NOT passed" if only_failures else "") + " ORDER BY item_id", (str(eval_run_id),))
    return r


class EvalRequest(BaseModel):
    suites: list[Literal["extraction", "retrieval", "evidence"]] = ["extraction", "retrieval", "evidence"]
    extractors: list[Literal["rules-v1", "rules-v2", "llm"]] = ["rules-v1", "rules-v2"]


@app.post("/api/eval/run", tags=["evaluation"])
def eval_run_now(body: EvalRequest, wait: bool = False, user: User = Depends(require("admin"))) -> Any:
    if "llm" in body.extractors and not get_settings().anthropic_api_key:
        raise HTTPException(503, "ANTHROPIC_API_KEY is not configured; cannot benchmark the LLM extractor")
    return _dispatch("benchmark", user, wait, lambda: run_suites(list(body.suites), list(body.extractors)))


# ---------------------------------------------------------------- monitoring
@app.get("/api/monitoring/overview", tags=["monitoring"])
def monitoring(_: User = Depends(require("viewer"))) -> dict[str, Any]:
    return overview()


@app.get("/api/monitoring/lineage", tags=["monitoring"])
def lineage_graph(_: User = Depends(require("viewer"))) -> dict[str, Any]:
    """Dataset-level lineage: source files → raw records → clinical tables → dbt models."""
    with connect() as c:
        files = list(c.execute("SELECT source_format, count(*) AS files FROM ops.source_files GROUP BY 1"))
        raw = list(c.execute("SELECT record_type, process_status, count(*) AS n FROM raw.records GROUP BY 1,2 ORDER BY 1,2"))
        flows = list(c.execute("""
            SELECT f.source_format, t.target, count(*) AS n FROM (
                SELECT source_record_id, 'person' AS target FROM clinical.person UNION ALL
                SELECT source_record_id, 'condition_occurrence' FROM clinical.condition_occurrence UNION ALL
                SELECT source_record_id, 'measurement' FROM clinical.measurement UNION ALL
                SELECT source_record_id, 'drug_exposure' FROM clinical.drug_exposure UNION ALL
                SELECT source_record_id, 'procedure_occurrence' FROM clinical.procedure_occurrence UNION ALL
                SELECT source_record_id, 'genomic_variant' FROM clinical.genomic_variant UNION ALL
                SELECT source_record_id, 'note' FROM clinical.note) t
            JOIN raw.records r ON r.raw_id = t.source_record_id JOIN ops.source_files f USING (source_file_id)
            GROUP BY 1,2 ORDER BY 1,2"""))
        dbt_models = list(c.execute("""SELECT table_schema, table_name FROM information_schema.tables
                                       WHERE table_schema IN ('dbt_staging','dbt_marts') ORDER BY 1,2"""))
        last_dbt = c.execute("SELECT run_id, status, finished_at, metrics FROM ops.pipeline_runs WHERE pipeline='dbt' ORDER BY started_at DESC LIMIT 1").fetchone()
    return {"source_formats": files, "raw_records": raw, "flows": flows, "dbt_models": dbt_models, "last_dbt_run": last_dbt}


def openapi_to_file(path: Path = REPO_ROOT / "docs" / "openapi.json") -> Path:
    import json
    path.write_text(json.dumps(app.openapi(), indent=1), encoding="utf-8")
    return path
