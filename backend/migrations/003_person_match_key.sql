-- Names are never persisted in analytical tables. For probable-duplicate detection we keep a
-- one-way hash of (normalized family name, given name, birth date, sex).
ALTER TABLE clinical.person ADD COLUMN match_key text;
CREATE INDEX ON clinical.person (match_key);
COMMENT ON COLUMN clinical.person.smoking_status IS 'Derived from clinical.note_fact (provenance: note text pattern extractor)';
