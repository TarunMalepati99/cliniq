# ADR 0003 — PostgreSQL as the primary store; dbt for analytical marts; DuckDB not used

**Status:** accepted

## Context
We need transactional ingestion (savepoints, upserts, DLQ), a least-privilege execution role for
cohort SQL, and analytical transformations with tests.

## Decision
PostgreSQL 16 holds the raw, operational, clinical, cohort and RAG schemas. dbt builds staging views and
marts in `dbt_staging` / `dbt_marts`, and its run results are recorded as pipeline runs. DuckDB was
considered for local analytics but not adopted. At this data size PostgreSQL answers cohort queries in
about 100 ms, and a second engine would add a sync path and a second SQL dialect for the compiler with
no measured benefit.

## Consequences
- ➕ One engine, one dialect, one set of grants; simple local setup.
- ➖ Large-scale analytics would favor a columnar engine. The GCP path in `design.md` describes moving the analytical layer to BigQuery behind the same DSL.
