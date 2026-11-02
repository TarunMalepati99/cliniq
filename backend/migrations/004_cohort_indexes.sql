-- Indexes supporting the per-person lateral lookups emitted by the cohort compiler.
CREATE INDEX IF NOT EXISTS note_fact_person_key_idx ON clinical.note_fact (person_id, fact_key);
CREATE INDEX IF NOT EXISTS genomic_person_biomarker_idx ON clinical.genomic_variant (person_id, biomarker_key, reported_date DESC);
CREATE INDEX IF NOT EXISTS procedure_person_class_idx ON clinical.procedure_occurrence (person_id, procedure_class);
CREATE INDEX IF NOT EXISTS drug_person_class_idx ON clinical.drug_exposure (person_id, drug_class);
CREATE INDEX IF NOT EXISTS cohort_run_patients_class_idx ON cohort.run_patients (cohort_run_id, classification);
