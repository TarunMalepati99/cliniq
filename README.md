# CLINIQ — Clinical Intelligence & Cohort Feasibility Platform

> **Research and engineering prototype.** CLINIQ runs on synthetic data. It is not a clinically
> validated product and must not be used for medical decision-making or patient care. See
> [docs/limitations.md](docs/limitations.md).

CLINIQ takes heterogeneous clinical data (FHIR R4 bundles, lab and genomic CSV feeds, clinical notes)
and turns it into a validated, queryable, explainable analytical dataset. Researchers can then ask
feasibility questions in plain language:

> *"Find patients aged 40–70 diagnosed with non-small-cell lung cancer, with an EGFR exon 19 deletion,
> and a documented history of prior treatment."*

From there, CLINIQ:

1. extracts structured inclusion and exclusion criteria,
2. compiles them deterministically into parameterized SQL that passes a static guard,
3. waits for a human to approve that exact SQL (the approval is bound to its hash),
4. runs it read-only, and
5. returns an attrition funnel with per-patient, per-criterion explanations, each traced back to a
   source record.

A separate evidence engine cites supporting passages from trial-eligibility documents. A benchmark
harness measures every AI component against gold data.

![Cohort results](docs/screenshots/06_cohort_results_funnel.png)

## What makes it more than a demo

| Concern | How CLINIQ handles it |
|---|---|
| **LLMs and SQL** | No model writes or runs SQL. Extractors emit a Pydantic `CohortDefinition`, which validates against a vocabulary allowlist. A deterministic compiler produces the SQL, and every value travels as a bind parameter. A `sqlglot` AST guard then checks the SQL: one statement, `SELECT` only, allowlisted tables and functions, no inline literals. The query is `EXPLAIN`ed as a SELECT-only database role and finally executed in a `READ ONLY` transaction with a timeout. |
| **Human in the loop** | Approval binds to the SQL hash the reviewer saw. At execution, CLINIQ recompiles the definition and refuses to run if the hash has drifted (tampering, or a compiler upgrade). An optional four-eyes rule stops authors approving their own queries. |
| **Missing ≠ negative** | Every criterion evaluates to one of four evidence types. **positive**: a matching record. **negative**: explicit contradicting evidence, such as a test reported "not detected" or a different documented histology. **absent**: the patient has data in that domain, but nothing matches. **unknown**: no data in that domain. Unknown never excludes a patient; such patients are reported as *potentially eligible*, together with which criterion is blocking them. |
| **Reproducibility** | Each run stores the definition hash, SQL hash, data version, fixed as-of date and a hash of the result. "Reproduce run" re-executes and compares. The synthetic data generator is seeded and byte-identical across runs. |
| **Traceability** | Every clinical row carries `source_record_id` → `raw.records` (content-addressed payload) → `ops.source_files` → `ops.pipeline_runs`. The UI exposes this for every row and every piece of cohort evidence. |
| **No silent drops** | Every landed record ends as *loaded*, *duplicate* or *rejected*, and every rejection gets a dead-letter row with its stage, error code and payload. On the seeded dataset the pipeline rejects exactly the faults the generator injected (39 of 39), and tests assert this. |
| **Measured AI** | 125 gold extraction requests (35 hand-written, 90 templated), 42 retrieval queries (6 deliberately unanswerable), and citation grounding checks. Runs are tracked by extractor version, prompt hash, benchmark hash, data version and code version. CI enforces regression floors. Results: [docs/evaluation_report.md](docs/evaluation_report.md). |

## Quick start (local, no cloud)

Requirements: Python ≥ 3.11, PostgreSQL 16, Node 22.

```bash
# 1. database (any PostgreSQL 16; user needs CREATEROLE for the read-only role)
createuser -s cliniq && psql -c "ALTER USER cliniq PASSWORD 'cliniq'" && createdb -O cliniq cliniq
export CLINIQ_DATABASE_URL=postgresql://cliniq:cliniq@localhost:5432/cliniq
export CLINIQ_READER_DATABASE_URL=postgresql://cliniq_reader:cliniq_reader@localhost:5432/cliniq

# 2. backend + full pipeline: migrate → generate 1,200 synthetic patients → ingest → dbt build → index evidence
pip install -e "backend[dbt,dev]"
cliniq bootstrap           # ~40 s
cliniq eval                # benchmark → eval/reports/*.json
cliniq serve               # API on :8000, OpenAPI docs at /docs

# 3. frontend
cd frontend && npm ci && npm run build && npm run start   # http://localhost:3000
```

Sign in with a development role (Admin, Researcher, Reviewer, Viewer). These are non-secret dev
tokens and only work when `CLINIQ_ENV=dev`. In any other environment, configure
`CLINIQ_API_TOKENS` as described in [.env.example](.env.example).

**Docker:** `cp .env.example .env` (set the passwords) and run `docker compose up --build`.
The Dockerfiles and compose file were written but **could not be executed** in the build
environment, which had no Docker daemon. Treat them as untested.

## The demonstration

`scripts/demo_walkthrough.mjs` drives a headless browser through the full required workflow and
fails on any console error. The screenshots in [docs/screenshots](docs/screenshots) come from that run.

| # | Step | Where |
|---|---|---|
| 1–2 | Upload a synthetic FHIR bundle (`data/demo/demo_upload_bundle.json`, which contains one deliberately invalid lab value); validate and ingest it | Pipeline |
| 3 | Inspect the normalized database, including per-row lineage | Clinical data |
| 4–6 | Enter a request, extract criteria, review the structured definition | Cohort builder |
| 7–8 | Review the compiled SQL and validation checks, approve, execute | Cohort builder |
| 9 | Explore the cohort, the attrition funnel and per-patient explanations | Cohort results |
| 10–11 | Retrieve supporting evidence, inspect citations and insufficient-evidence flags | Evidence |
| 12–13 | Run the benchmark, inspect accuracy, failures and data-quality metrics | Evaluation, Quality & lineage |

```bash
cd scripts && npm i --no-save playwright && cd .. && node scripts/demo_walkthrough.mjs
```

## Repository layout

```
backend/cliniq/
  synth/        seeded synthetic data generator with an exact fault-injection ledger
  ingest/       FHIR subset models, transform/standardize, pipeline (DLQ, idempotency, lineage), dbt runner
  cohort/       criteria DSL, deterministic SQL compiler, SQL guard, workflow engine
  extraction/   rules-v1 (baseline), rules-v2, LLM adapter (+ versioned prompt)
  retrieval/    corpus builder, chunking, BM25 + LSA hybrid index, evidence engine
  evaluation/   benchmark runner, metrics, version tracking
  observability/ structured logging, monitoring queries
  api/          FastAPI app (RBAC, request IDs, background jobs)
backend/migrations/   forward-only SQL migrations (checksummed)
backend/tests/        61 unit, integration, API and benchmark-regression tests
dbt/                  staging views, marts, an incremental fact table, 18 data tests
frontend/             Next.js 16 / React 19 / Tailwind / Recharts dashboard (8 pages)
eval/benchmark/       gold extraction + retrieval benchmarks and their builder
eval/reports/         recorded benchmark runs (JSON)
data/corpus/          evidence corpus (authored reference notes; synthetic trials are generated)
docs/                 design doc, ADRs, evaluation report, limitations, API reference, diagrams, screenshots
```

## Documentation

- [Technical design](docs/design.md), including the optional GCP deployment path
- [Architecture decision records](docs/adr/)
- [Evaluation report](docs/evaluation_report.md), generated from recorded runs
- [Limitations and responsible use](docs/limitations.md)
- [API reference](docs/api.md) (also live at `/docs` and in [docs/openapi.json](docs/openapi.json))
- Diagrams: [architecture](docs/diagrams/architecture.svg) · [data pipeline](docs/diagrams/pipeline.svg) · [database schema](docs/diagrams/schema.svg) · [cohort workflow](docs/diagrams/cohort_workflow.svg)

## Tests

```bash
createdb -O cliniq cliniq_test
cd backend && pytest          # 61 tests
cd frontend && npm run typecheck && npm run build
```

The tests cover:

- **Transforms:** unit conversions, every rejection path, FHIR partial dates, note facts, name non-persistence.
- **Ingestion:** results match the generator's fault ledger; no pending records remain; full lineage; idempotent re-ingest; DLQ recovery after a late-arriving patient.
- **Cohort engine:** DSL validation (injection attempts rejected); compiler determinism; the SQL guard blocking DML, multi-statement, non-allowlisted tables, functions and inline literals; reader-role privileges; tri-state semantics on hand-built fixtures; the approval workflow and tamper detection; reproducibility.
- **API:** RBAC, request-ID propagation, the end-to-end cohort workflow, upload safety, background jobs.
- **Benchmark floors:** regression thresholds from `eval/thresholds.json`.
