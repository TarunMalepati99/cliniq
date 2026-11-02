-- Support for third-party FHIR (e.g. Synthea):
--  * records whose resource type is outside CLINIQ's scope are landed (lineage) and marked
--    'out_of_scope' with counts in run metrics -- never silently dropped, never dead-lettered
--  * bundle-level context (fullUrl -> resource id, referenced Medication codings) needed to
--    resolve urn:uuid references without mutating the immutable raw payload
--  * OMOP-style source vs standard codes: SNOMED source codes are kept alongside the
--    crosswalked ICD-10-CM code used for cohort logic
ALTER TABLE raw.records DROP CONSTRAINT records_process_status_check;
ALTER TABLE raw.records ADD CONSTRAINT records_process_status_check
    CHECK (process_status IN ('pending','loaded','rejected','duplicate','out_of_scope'));
ALTER TABLE raw.records ADD COLUMN context jsonb;
ALTER TABLE clinical.condition_occurrence ADD COLUMN source_vocabulary text;
ALTER TABLE clinical.condition_occurrence ADD COLUMN source_code text;
ALTER TABLE clinical.condition_occurrence ADD COLUMN source_display text;
