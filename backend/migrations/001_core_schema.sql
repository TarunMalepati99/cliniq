-- CLINIQ core schema.
-- Schemas:
--   ops       pipeline runs, ingestion events, dead-letter queue, data-quality issues, query/eval telemetry
--   raw       immutable landing zone (content-addressed source records)
--   clinical  normalized analytical tables inspired by (NOT compliant with) OMOP CDM v5.4
--   cohort    saved cohort definitions, approvals, runs, per-patient explanations
--   rag       evidence documents and chunks

CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS clinical;
CREATE SCHEMA IF NOT EXISTS cohort;
CREATE SCHEMA IF NOT EXISTS rag;

-- ---------------------------------------------------------------- ops
CREATE TABLE ops.pipeline_runs (
    run_id          uuid PRIMARY KEY,
    pipeline        text NOT NULL,                       -- 'ingest', 'dbt', 'rag_index', ...
    status          text NOT NULL CHECK (status IN ('running','succeeded','failed','partial')),
    triggered_by    text NOT NULL,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    duration_ms     integer,
    params          jsonb NOT NULL DEFAULT '{}'::jsonb,
    metrics         jsonb NOT NULL DEFAULT '{}'::jsonb,  -- ingested / rejected / duplicates / ...
    error           text
);

CREATE TABLE ops.source_files (
    source_file_id  bigserial PRIMARY KEY,
    file_name       text NOT NULL,
    file_sha256     text NOT NULL,
    source_format   text NOT NULL CHECK (source_format IN ('fhir_bundle','fhir_ndjson','lab_csv','genomic_csv','notes_jsonl')),
    source_system   text NOT NULL,
    size_bytes      bigint NOT NULL,
    first_run_id    uuid NOT NULL REFERENCES ops.pipeline_runs(run_id),
    received_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (file_sha256)
);

CREATE TABLE ops.ingestion_events (
    event_id        bigserial PRIMARY KEY,
    run_id          uuid NOT NULL REFERENCES ops.pipeline_runs(run_id),
    ts              timestamptz NOT NULL DEFAULT now(),
    level           text NOT NULL CHECK (level IN ('debug','info','warning','error')),
    stage           text NOT NULL,
    message         text NOT NULL,
    details         jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX ON ops.ingestion_events (run_id);

CREATE TABLE ops.dead_letter (
    dlq_id          bigserial PRIMARY KEY,
    run_id          uuid NOT NULL REFERENCES ops.pipeline_runs(run_id),
    source_file_id  bigint REFERENCES ops.source_files(source_file_id),
    record_locator  text NOT NULL,           -- e.g. 'bundle_003.json#entry[17]' or 'labs.csv#line 88'
    resource_type   text,
    stage           text NOT NULL,           -- 'parse','schema','business_rule','reference','load'
    error_code      text NOT NULL,
    error_detail    jsonb NOT NULL,
    payload         jsonb NOT NULL,
    status          text NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved','retry_failed','ignored')),
    retry_count     integer NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    resolved_at     timestamptz
);
CREATE INDEX ON ops.dead_letter (status);

CREATE TABLE ops.dq_issues (
    issue_id        bigserial PRIMARY KEY,
    run_id          uuid NOT NULL REFERENCES ops.pipeline_runs(run_id),
    check_name      text NOT NULL,           -- 'possible_duplicate_person', 'unit_converted', ...
    severity        text NOT NULL CHECK (severity IN ('info','warning','error')),
    entity          text NOT NULL,
    entity_id       text,
    details         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ops.api_requests (
    request_id      uuid PRIMARY KEY,
    ts              timestamptz NOT NULL DEFAULT now(),
    method          text NOT NULL,
    path            text NOT NULL,
    status_code     integer NOT NULL,
    duration_ms     integer NOT NULL,
    user_id         text
);

-- ---------------------------------------------------------------- raw
CREATE TABLE raw.records (
    raw_id          bigserial PRIMARY KEY,
    source_file_id  bigint NOT NULL REFERENCES ops.source_files(source_file_id),
    run_id          uuid NOT NULL REFERENCES ops.pipeline_runs(run_id),
    record_locator  text NOT NULL,
    record_type     text NOT NULL,           -- FHIR resourceType or 'LabRow'/'GenomicRow'/'Note'
    source_key      text,                    -- FHIR id / natural key of the row
    content_sha256  text NOT NULL,
    payload         jsonb NOT NULL,
    process_status  text NOT NULL DEFAULT 'pending' CHECK (process_status IN ('pending','loaded','rejected','duplicate')),
    landed_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (record_type, content_sha256)     -- idempotent landing: identical content lands once
);
CREATE INDEX ON raw.records (process_status);
CREATE INDEX ON raw.records (run_id);

-- ---------------------------------------------------------------- clinical (OMOP-inspired)
-- Concept dictionary. vocabulary 'ICD10CM', 'LOINC', 'ICDO3' are real code systems
-- (curated subset); 'CLINIQ' marks local codes used where a standard code was not mapped.
CREATE TABLE clinical.concept (
    vocabulary      text NOT NULL,
    code            text NOT NULL,
    name            text NOT NULL,
    domain          text NOT NULL,           -- Condition, Measurement, Drug, Procedure, Histology, Gene
    concept_class   text,                    -- e.g. drug class 'egfr_tki'
    PRIMARY KEY (vocabulary, code)
);

CREATE TABLE clinical.person (
    person_id           text PRIMARY KEY,    -- FHIR Patient.id (synthetic)
    birth_date          date,
    sex                 text CHECK (sex IN ('female','male','other','unknown')),
    state               text,                -- US state (2-letter) of current address
    smoking_status      text,
    deceased            boolean,
    source_record_id    bigint NOT NULL REFERENCES raw.records(raw_id),
    run_id              uuid NOT NULL REFERENCES ops.pipeline_runs(run_id),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE clinical.condition_occurrence (
    condition_id        text PRIMARY KEY,    -- source resource id
    person_id           text NOT NULL REFERENCES clinical.person(person_id),
    vocabulary          text NOT NULL,
    code                text NOT NULL,
    histology_code      text,                -- ICD-O-3 morphology when documented
    onset_date          date,
    clinical_status     text,
    source_record_id    bigint NOT NULL REFERENCES raw.records(raw_id),
    run_id              uuid NOT NULL REFERENCES ops.pipeline_runs(run_id)
);
CREATE INDEX ON clinical.condition_occurrence (person_id);
CREATE INDEX ON clinical.condition_occurrence (code);

CREATE TABLE clinical.measurement (
    measurement_id      text PRIMARY KEY,
    person_id           text NOT NULL REFERENCES clinical.person(person_id),
    vocabulary          text NOT NULL,
    code                text NOT NULL,
    lab_key             text NOT NULL,       -- canonical key, e.g. 'creatinine'
    value_numeric       double precision,
    unit                text,                -- canonical unit after normalization
    source_value        double precision,
    source_unit         text,
    measured_at         date NOT NULL,
    source_record_id    bigint NOT NULL REFERENCES raw.records(raw_id),
    run_id              uuid NOT NULL REFERENCES ops.pipeline_runs(run_id)
);
CREATE INDEX ON clinical.measurement (person_id, lab_key, measured_at DESC);

CREATE TABLE clinical.drug_exposure (
    drug_exposure_id    text PRIMARY KEY,
    person_id           text NOT NULL REFERENCES clinical.person(person_id),
    vocabulary          text NOT NULL,
    code                text NOT NULL,
    drug_name           text NOT NULL,
    drug_class          text,
    start_date          date,
    end_date            date,
    status              text,
    source_record_id    bigint NOT NULL REFERENCES raw.records(raw_id),
    run_id              uuid NOT NULL REFERENCES ops.pipeline_runs(run_id)
);
CREATE INDEX ON clinical.drug_exposure (person_id);

CREATE TABLE clinical.procedure_occurrence (
    procedure_id        text PRIMARY KEY,
    person_id           text NOT NULL REFERENCES clinical.person(person_id),
    vocabulary          text NOT NULL,
    code                text NOT NULL,
    procedure_class     text,
    performed_date      date,
    source_record_id    bigint NOT NULL REFERENCES raw.records(raw_id),
    run_id              uuid NOT NULL REFERENCES ops.pipeline_runs(run_id)
);
CREATE INDEX ON clinical.procedure_occurrence (person_id);

-- One row per biomarker test result. result in (detected, not_detected, indeterminate).
CREATE TABLE clinical.genomic_variant (
    variant_result_id   text PRIMARY KEY,
    person_id           text NOT NULL REFERENCES clinical.person(person_id),
    gene                text NOT NULL,
    biomarker_key       text NOT NULL,       -- canonical, e.g. 'egfr_exon19del'
    hgvs_p              text,
    result              text NOT NULL CHECK (result IN ('detected','not_detected','indeterminate')),
    assay               text,
    reported_date       date,
    source_record_id    bigint NOT NULL REFERENCES raw.records(raw_id),
    run_id              uuid NOT NULL REFERENCES ops.pipeline_runs(run_id)
);
CREATE INDEX ON clinical.genomic_variant (person_id, gene);

CREATE TABLE clinical.note (
    note_id             text PRIMARY KEY,
    person_id           text NOT NULL REFERENCES clinical.person(person_id),
    note_date           date,
    note_type           text,
    note_text           text NOT NULL,
    source_record_id    bigint NOT NULL REFERENCES raw.records(raw_id),
    run_id              uuid NOT NULL REFERENCES ops.pipeline_runs(run_id)
);

-- Facts extracted from notes by a deterministic pattern extractor (provenance = note).
CREATE TABLE clinical.note_fact (
    note_fact_id        bigserial PRIMARY KEY,
    note_id             text NOT NULL REFERENCES clinical.note(note_id) ON DELETE CASCADE,
    person_id           text NOT NULL REFERENCES clinical.person(person_id),
    fact_key            text NOT NULL,       -- 'ecog', 'smoking_status'
    fact_value          text NOT NULL,
    span_start          integer NOT NULL,
    span_end            integer NOT NULL,
    extractor_version   text NOT NULL,
    UNIQUE (note_id, fact_key, span_start)
);

-- ---------------------------------------------------------------- cohort
CREATE TABLE cohort.definitions (
    definition_id       uuid PRIMARY KEY,
    name                text NOT NULL,
    nl_request          text,
    definition          jsonb NOT NULL,
    definition_sha256   text NOT NULL,
    extraction          jsonb,               -- extractor output incl. spans/unsupported phrases
    extractor_version   text,
    compiler_version    text NOT NULL,
    compiled_sql        text NOT NULL,
    compiled_params     jsonb NOT NULL,
    sql_sha256          text NOT NULL,
    validation          jsonb NOT NULL,
    status              text NOT NULL CHECK (status IN ('draft','approved','rejected')),
    created_by          text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE cohort.approvals (
    approval_id         bigserial PRIMARY KEY,
    definition_id       uuid NOT NULL REFERENCES cohort.definitions(definition_id),
    sql_sha256          text NOT NULL,
    decision            text NOT NULL CHECK (decision IN ('approved','rejected')),
    decided_by          text NOT NULL,
    comment             text,
    decided_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE cohort.runs (
    cohort_run_id       uuid PRIMARY KEY,
    definition_id       uuid NOT NULL REFERENCES cohort.definitions(definition_id),
    sql_sha256          text NOT NULL,
    data_version        text NOT NULL,
    as_of_date          date NOT NULL,
    executed_by         text NOT NULL,
    started_at          timestamptz NOT NULL DEFAULT now(),
    duration_ms         integer,
    status              text NOT NULL CHECK (status IN ('running','succeeded','failed')),
    summary             jsonb,
    result_sha256       text,
    error               text
);

CREATE TABLE cohort.run_patients (
    cohort_run_id       uuid NOT NULL REFERENCES cohort.runs(cohort_run_id) ON DELETE CASCADE,
    person_id           text NOT NULL,
    classification      text NOT NULL CHECK (classification IN ('eligible','potentially_eligible','ineligible')),
    criteria            jsonb NOT NULL,      -- per-criterion status/evidence
    PRIMARY KEY (cohort_run_id, person_id)
);

-- ---------------------------------------------------------------- rag
CREATE TABLE rag.documents (
    doc_id              text PRIMARY KEY,
    title               text NOT NULL,
    doc_type            text NOT NULL,       -- 'trial_eligibility', 'reference_note'
    source              text NOT NULL,       -- provenance statement
    license             text NOT NULL,
    content_sha256      text NOT NULL,
    body                text NOT NULL,
    indexed_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE rag.chunks (
    chunk_id            text PRIMARY KEY,
    doc_id              text NOT NULL REFERENCES rag.documents(doc_id) ON DELETE CASCADE,
    ordinal             integer NOT NULL,
    section             text,
    text                text NOT NULL,
    char_start          integer NOT NULL,
    char_end            integer NOT NULL
);

CREATE TABLE rag.queries (
    rag_query_id        uuid PRIMARY KEY,
    ts                  timestamptz NOT NULL DEFAULT now(),
    query               text NOT NULL,
    mode                text NOT NULL,
    top_k               integer NOT NULL,
    n_results           integer NOT NULL,
    max_score           double precision,
    insufficient        boolean NOT NULL,
    duration_ms         integer NOT NULL,
    user_id             text
);

-- ---------------------------------------------------------------- evaluation
CREATE TABLE ops.eval_runs (
    eval_run_id         uuid PRIMARY KEY,
    suite               text NOT NULL,       -- 'extraction', 'retrieval', 'evidence'
    system_version      text NOT NULL,       -- extractor/retriever version
    prompt_sha256       text,
    benchmark_sha256    text NOT NULL,
    data_version        text,
    code_version        text,
    started_at          timestamptz NOT NULL DEFAULT now(),
    duration_ms         integer,
    metrics             jsonb NOT NULL,
    n_items             integer NOT NULL,
    report_path         text
);

CREATE TABLE ops.eval_item_results (
    eval_run_id         uuid NOT NULL REFERENCES ops.eval_runs(eval_run_id) ON DELETE CASCADE,
    item_id             text NOT NULL,
    passed              boolean NOT NULL,
    failure_category    text,
    details             jsonb NOT NULL,
    PRIMARY KEY (eval_run_id, item_id)
);
