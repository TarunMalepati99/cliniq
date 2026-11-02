-- Least-privilege role used to execute approved cohort SQL.
-- It can only SELECT from the clinical schema; it cannot read ops/cohort/raw or write anything.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cliniq_reader') THEN
        CREATE ROLE cliniq_reader LOGIN;  -- password is set from CLINIQ_READER_PASSWORD by the migration runner
    END IF;
END $$;

REVOKE ALL ON SCHEMA ops, raw, cohort, rag FROM cliniq_reader;
GRANT USAGE ON SCHEMA clinical TO cliniq_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA clinical TO cliniq_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA clinical GRANT SELECT ON TABLES TO cliniq_reader;
