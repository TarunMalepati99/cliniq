"""Monitoring queries: ingestion health, freshness, missingness, violations, performance, trends.

Everything returned is computed from operational tables; nothing is estimated. When a
signal is unavailable (e.g. no runs yet) the field is null and `empty` explains why.
"""
from __future__ import annotations

from typing import Any

from ..db import connect


def _q(c: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    return list(c.execute(sql, params).fetchall())


def overview() -> dict[str, Any]:
    with connect() as c:
        runs = _q(c, """SELECT run_id, pipeline, status, triggered_by, started_at, finished_at, duration_ms, metrics, error
                       FROM ops.pipeline_runs ORDER BY started_at DESC LIMIT 25""")
        counts = _q(c, """SELECT 'person' AS t, count(*) n FROM clinical.person UNION ALL SELECT 'condition_occurrence', count(*) FROM clinical.condition_occurrence
                          UNION ALL SELECT 'measurement', count(*) FROM clinical.measurement UNION ALL SELECT 'drug_exposure', count(*) FROM clinical.drug_exposure
                          UNION ALL SELECT 'procedure_occurrence', count(*) FROM clinical.procedure_occurrence UNION ALL SELECT 'genomic_variant', count(*) FROM clinical.genomic_variant
                          UNION ALL SELECT 'note', count(*) FROM clinical.note UNION ALL SELECT 'note_fact', count(*) FROM clinical.note_fact""")
        freshness = c.execute("""SELECT (SELECT max(finished_at) FROM ops.pipeline_runs WHERE pipeline='ingest' AND status IN ('succeeded','partial')) AS last_ingest,
                                        (SELECT max(finished_at) FROM ops.pipeline_runs WHERE pipeline='dbt' AND status='succeeded') AS last_dbt,
                                        (SELECT max(finished_at) FROM ops.pipeline_runs WHERE pipeline='rag_index' AND status='succeeded') AS last_rag_index,
                                        (SELECT max(measured_at) FROM clinical.measurement) AS latest_measurement_date,
                                        (SELECT max(reported_date) FROM clinical.genomic_variant) AS latest_genomic_date,
                                        extract(epoch FROM now() - (SELECT max(finished_at) FROM ops.pipeline_runs WHERE pipeline='ingest' AND status IN ('succeeded','partial')))/3600 AS hours_since_ingest""").fetchone()
        dlq = _q(c, "SELECT stage, error_code, status, count(*) AS n FROM ops.dead_letter GROUP BY 1,2,3 ORDER BY n DESC")
        dq = _q(c, "SELECT check_name, severity, count(*) AS n FROM ops.dq_issues GROUP BY 1,2 ORDER BY n DESC")
        raw = _q(c, "SELECT process_status, count(*) AS n FROM raw.records GROUP BY 1")
        missing = _q(c, """SELECT 'person.birth_date' AS field, avg((birth_date IS NULL)::int)::float AS null_rate FROM clinical.person
                           UNION ALL SELECT 'person.state', avg((state IS NULL)::int)::float FROM clinical.person
                           UNION ALL SELECT 'person.smoking_status', avg((smoking_status IS NULL)::int)::float FROM clinical.person
                           UNION ALL SELECT 'condition.histology_code (lung)', avg((histology_code IS NULL)::int)::float FROM clinical.condition_occurrence WHERE code LIKE 'C34%%'
                           UNION ALL SELECT 'condition.onset_date', avg((onset_date IS NULL)::int)::float FROM clinical.condition_occurrence
                           UNION ALL SELECT 'drug_exposure.end_date', avg((end_date IS NULL)::int)::float FROM clinical.drug_exposure
                           UNION ALL SELECT 'drug_exposure.drug_class', avg((drug_class IS NULL)::int)::float FROM clinical.drug_exposure""")
        coverage = _q(c, """WITH n AS (SELECT count(*)::float AS t FROM clinical.person)
            SELECT 'conditions' AS domain, count(DISTINCT person_id)/nullif((SELECT t FROM n),0) AS coverage FROM clinical.condition_occurrence
            UNION ALL SELECT 'measurements', count(DISTINCT person_id)/nullif((SELECT t FROM n),0) FROM clinical.measurement
            UNION ALL SELECT 'medications', count(DISTINCT person_id)/nullif((SELECT t FROM n),0) FROM clinical.drug_exposure
            UNION ALL SELECT 'genomics', count(DISTINCT person_id)/nullif((SELECT t FROM n),0) FROM clinical.genomic_variant
            UNION ALL SELECT 'notes', count(DISTINCT person_id)/nullif((SELECT t FROM n),0) FROM clinical.note
            UNION ALL SELECT 'ECOG (structured or note)', count(DISTINCT person_id)/nullif((SELECT t FROM n),0) FROM
               (SELECT person_id FROM clinical.measurement WHERE lab_key='ecog' UNION SELECT person_id FROM clinical.note_fact WHERE fact_key='ecog') e""")
        cohort_perf = c.execute("""SELECT count(*) AS runs, count(*) FILTER (WHERE status='failed') AS failed,
                                          percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50_ms,
                                          percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_ms
                                   FROM cohort.runs""").fetchone()
        api_perf = _q(c, """SELECT regexp_replace(regexp_replace(regexp_replace(path, '[0-9a-f]{8}-[0-9a-f-]{27}', ':id', 'g'), '/[0-9]+(/|$)', '/:n\\1', 'g'), '/patients/[^/]+$', '/patients/:id') AS route, count(*) AS n,
                               percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50_ms,
                               percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_ms,
                               count(*) FILTER (WHERE status_code >= 500) AS errors_5xx, count(*) FILTER (WHERE status_code BETWEEN 400 AND 499) AS errors_4xx
                            FROM ops.api_requests WHERE ts > now() - interval '7 days' GROUP BY 1 ORDER BY n DESC LIMIT 15""")
        rag = c.execute("""SELECT count(*) AS queries, count(*) FILTER (WHERE insufficient) AS insufficient,
                                  percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_ms FROM rag.queries""").fetchone()
        evals = _q(c, """SELECT eval_run_id, suite, system_version, started_at, metrics, n_items, data_version, code_version
                         FROM ops.eval_runs ORDER BY started_at""")
    ingest_runs = [r for r in runs if r["pipeline"] in ("ingest", "dlq_retry")]
    failures = [r for r in runs if r["status"] in ("failed", "partial")]
    return {
        "row_counts": {r["t"]: r["n"] for r in counts},
        "pipeline_runs": runs,
        "ingestion_health": {
            "last_run": ingest_runs[0] if ingest_runs else None,
            "runs_total": len(ingest_runs), "runs_with_rejections_or_failures": len([r for r in ingest_runs if r["status"] != "succeeded"]),
        },
        "freshness": freshness,
        "raw_record_status": {r["process_status"]: r["n"] for r in raw},
        "dead_letter": dlq,
        "dq_issues": dq,
        "missingness": missing,
        "domain_coverage": coverage,
        "pipeline_failures": [{k: r[k] for k in ("run_id", "pipeline", "status", "started_at", "error")} for r in failures],
        "query_performance": {"cohort_runs": cohort_perf, "api": api_perf},
        "retrieval": rag,
        "eval_trend": evals,
    }


def pipeline_run(run_id: str) -> dict[str, Any] | None:
    with connect() as c:
        run = c.execute("SELECT * FROM ops.pipeline_runs WHERE run_id=%s", (run_id,)).fetchone()
        if not run:
            return None
        run["events"] = _q(c, "SELECT ts, level, stage, message, details FROM ops.ingestion_events WHERE run_id=%s ORDER BY event_id", (run_id,))
        run["dead_letter"] = _q(c, """SELECT dlq_id, record_locator, resource_type, stage, error_code, error_detail, status, retry_count
                                     FROM ops.dead_letter WHERE run_id=%s ORDER BY dlq_id""", (run_id,))
        run["files"] = _q(c, "SELECT source_file_id, file_name, source_format, size_bytes, received_at FROM ops.source_files WHERE first_run_id=%s", (run_id,))
        return run
