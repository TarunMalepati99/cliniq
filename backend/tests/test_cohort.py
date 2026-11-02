from datetime import date

import pytest
from pydantic import ValidationError

from cliniq.cohort import engine
from cliniq.cohort.compiler import classify, compile_definition
from cliniq.cohort.models import CohortDefinition
from cliniq.cohort.sqlguard import validate_sql
from cliniq.db import connect, read_only

AS_OF = date(2026, 1, 1)
DEMO = {"name": "EGFR ex19 NSCLC", "inclusion": [
    {"type": "age", "min_years": 40, "max_years": 70}, {"type": "diagnosis", "condition": "nsclc"},
    {"type": "biomarker", "markers": ["egfr_exon19del"]}, {"type": "treatment", "drug_class": "any_systemic_therapy"}],
    "exclusion": [{"type": "diagnosis", "condition": "brain_metastases"}]}


# ---------------------------------------------------------------- DSL validation

@pytest.mark.parametrize("bad", [
    {"inclusion": [{"type": "diagnosis", "condition": "clinical.person; DROP TABLE x"}]},
    {"inclusion": [{"type": "biomarker", "markers": ["egfr_exon20ins"]}]},
    {"inclusion": [{"type": "lab", "lab": "creatinine", "op": "!=", "value": 1}]},
    {"inclusion": [{"type": "age", "min_years": 70, "max_years": 40}]},
    {"inclusion": [{"type": "treatment", "drug_class": "egfr_tki", "drug": "osimertinib"}]},
    {"inclusion": [{"type": "sql", "query": "SELECT 1"}]},
    {"inclusion": [], "exclusion": []},
])
def test_invalid_definitions_rejected(bad):
    with pytest.raises(ValidationError):
        CohortDefinition.model_validate(bad)


def test_definition_hash_is_order_insensitive():
    a = CohortDefinition.model_validate(DEMO)
    b = CohortDefinition.model_validate({**DEMO, "inclusion": list(reversed(DEMO["inclusion"]))})
    assert a.sha256() == b.sha256()


# ---------------------------------------------------------------- compiler / guard

def test_compiler_is_deterministic_and_parameterized():
    d = CohortDefinition.model_validate(DEMO)
    q1, q2 = compile_definition(d, AS_OF), compile_definition(d, AS_OF)
    assert q1.sql == q2.sql and q1.sha256 == q2.sha256
    assert "egfr_exon19del" not in q1.sql  # values are bind parameters, never inlined
    assert validate_sql(q1.sql).ok


@pytest.mark.parametrize("sql,failed", [
    ("SELECT 1; DROP TABLE clinical.person", "single_statement"),
    ("DELETE FROM clinical.person", "select_only"),
    ("SELECT * FROM ops.dead_letter", "allowlisted_tables"),
    ("SELECT pg_sleep(10) FROM clinical.person", "allowlisted_functions"),
    ("SELECT * FROM clinical.person WHERE state = 'IN'", "no_inline_values"),
])
def test_sql_guard_blocks_unsafe_sql(sql, failed):
    rep = validate_sql(sql)
    assert not rep.ok
    assert any(c["check"] == failed and not c["passed"] for c in rep.checks)


def test_reader_role_cannot_write_or_read_ops(db):
    with read_only() as c:
        with pytest.raises(Exception):
            c.execute("SELECT * FROM ops.dead_letter LIMIT 1")
    with read_only() as c:
        with pytest.raises(Exception):
            c.execute("DELETE FROM clinical.person")


# ---------------------------------------------------------------- tri-state semantics (hand-built fixtures)

def _rows(defn: dict, ids: list[str]) -> dict[str, dict]:
    q = compile_definition(CohortDefinition.model_validate(defn), AS_OF)
    with read_only() as c:
        rows = c.execute(q.sql, q.params).fetchall()
    out = {r["person_id"]: r for r in rows if r["person_id"] in ids}
    for r in out.values():
        r["_class"] = classify(r, q.criteria)
    return out


@pytest.fixture(scope="module")
def fixture_patients(db):
    """Insert hand-crafted patients covering each evidence type."""
    with connect() as c:
        rid = c.execute("SELECT raw_id, run_id FROM raw.records LIMIT 1").fetchone()
        base = (rid["raw_id"], rid["run_id"])
        for pid, birth in [("fx-pos", "1970-01-01"), ("fx-neg", "1970-01-01"), ("fx-unk", None), ("fx-histunk", "1970-01-01"), ("fx-other", "1970-01-01")]:
            c.execute("INSERT INTO clinical.person (person_id, birth_date, sex, state, source_record_id, run_id) VALUES (%s,%s,'female','IN',%s,%s) ON CONFLICT DO NOTHING",
                      (pid, birth, *base))
        conds = [("fxc1", "fx-pos", "C34.90", "8140/3"), ("fxc2", "fx-neg", "C34.90", "8041/3"),
                 ("fxc3", "fx-histunk", "C34.90", None), ("fxc4", "fx-other", "I10", None)]
        for cid, pid, code, hist in conds:
            c.execute("INSERT INTO clinical.condition_occurrence (condition_id, person_id, vocabulary, code, histology_code, onset_date, source_record_id, run_id)"
                      " VALUES (%s,%s,'ICD10CM',%s,%s,'2024-01-01',%s,%s) ON CONFLICT DO NOTHING", (cid, pid, code, hist, *base))
        gx = [("fxg1", "fx-pos", "EGFR", "egfr_exon19del", "detected"), ("fxg2", "fx-neg", "EGFR", "egfr_exon19del", "not_detected"),
              ("fxg3", "fx-histunk", "EGFR", "egfr_exon19del", "indeterminate")]
        for vid, pid, gene, key, res in gx:
            c.execute("INSERT INTO clinical.genomic_variant (variant_result_id, person_id, gene, biomarker_key, result, reported_date, source_record_id, run_id)"
                      " VALUES (%s,%s,%s,%s,%s,'2024-02-01',%s,%s) ON CONFLICT DO NOTHING", (vid, pid, gene, key, res, *base))
        c.execute("INSERT INTO clinical.drug_exposure (drug_exposure_id, person_id, vocabulary, code, drug_name, drug_class, start_date, source_record_id, run_id)"
                  " VALUES ('fxd1','fx-pos','CLINIQ','RX-OSIMERTINIB','osimertinib','egfr_tki','2024-03-01',%s,%s),"
                  "        ('fxd2','fx-other','CLINIQ','RX-LISINOPRIL','lisinopril','antihypertensive','2024-03-01',%s,%s) ON CONFLICT DO NOTHING",
                  (*base, *base))
    yield ["fx-pos", "fx-neg", "fx-unk", "fx-histunk", "fx-other"]
    with connect() as c:
        for t in ["drug_exposure", "genomic_variant", "condition_occurrence"]:
            c.execute(f"DELETE FROM clinical.{t} WHERE person_id LIKE 'fx-%%'")
        c.execute("DELETE FROM clinical.person WHERE person_id LIKE 'fx-%%'")


def test_diagnosis_evidence_types(fixture_patients):
    r = _rows({"inclusion": [{"type": "diagnosis", "condition": "nsclc"}]}, fixture_patients)
    assert r["fx-pos"]["i1_evidence"] == "positive"
    assert r["fx-neg"]["i1_evidence"] == "negative"      # small-cell histology documented
    assert r["fx-histunk"]["i1_evidence"] == "unknown"   # lung cancer, histology undocumented
    assert r["fx-other"]["i1_evidence"] == "absent"      # has conditions, none matching
    assert r["fx-unk"]["i1_evidence"] == "unknown"       # no conditions at all


def test_biomarker_and_missing_is_not_negative(fixture_patients):
    r = _rows({"inclusion": [{"type": "biomarker", "markers": ["egfr_exon19del"]}]}, fixture_patients)
    assert r["fx-pos"]["i1_evidence"] == "positive"
    assert r["fx-neg"]["i1_evidence"] == "negative"
    assert r["fx-histunk"]["i1_evidence"] == "unknown"   # indeterminate result
    assert r["fx-unk"]["i1_evidence"] == "unknown"       # never tested
    assert r["fx-unk"]["_class"] == "potentially_eligible"  # unknown never silently excludes


def test_negative_biomarker_requirement(fixture_patients):
    r = _rows({"inclusion": [{"type": "biomarker", "markers": ["egfr_exon19del"], "status": "not_detected"}]}, fixture_patients)
    assert r["fx-neg"]["i1_evidence"] == "positive" and r["fx-pos"]["i1_evidence"] == "negative"


def test_treatment_absent_vs_unknown(fixture_patients):
    r = _rows({"inclusion": [{"type": "treatment", "drug_class": "egfr_tki"}]}, fixture_patients)
    assert r["fx-pos"]["i1_evidence"] == "positive"
    assert r["fx-other"]["i1_evidence"] == "absent"   # has medication records, none EGFR TKI
    assert r["fx-neg"]["i1_evidence"] == "unknown"    # no medication records at all


def test_exclusion_unknown_blocks_certainty(fixture_patients):
    r = _rows({"inclusion": [{"type": "sex", "sex": "female"}], "exclusion": [{"type": "treatment", "drug_class": "egfr_tki"}]}, fixture_patients)
    assert r["fx-pos"]["_class"] == "ineligible"
    assert r["fx-other"]["_class"] == "eligible"
    assert r["fx-neg"]["_class"] == "potentially_eligible"


# ---------------------------------------------------------------- workflow

def test_execution_requires_approval_and_is_reproducible(db):
    d = engine.create_definition(CohortDefinition.model_validate(DEMO), user="r1@test")
    did = str(d["definition_id"])
    assert d["validation"]["ok"] and d["status"] == "draft"
    with pytest.raises(engine.WorkflowError):
        engine.execute(did, "r1@test")
    with pytest.raises(engine.WorkflowError):
        engine.decide(did, "r2@test", "0" * 64, approve=True)  # stale/incorrect hash
    engine.decide(did, "r2@test", d["sql_sha256"], approve=True, comment="reviewed")
    run = engine.execute(did, "r1@test")
    s = run["summary"]
    assert run["status"] == "succeeded"
    assert s["eligible"] + s["potentially_eligible"] + s["ineligible"] == s["total_patients"]
    assert s["funnel"][0]["strict"] == s["total_patients"]
    strict = [f["strict"] for f in s["funnel"]]
    assert strict == sorted(strict, reverse=True)  # funnel is monotone
    assert s["funnel"][-1]["strict"] == s["eligible"]
    rep = engine.reproduce(str(run["cohort_run_id"]), "r1@test")
    assert rep["identical_result"] and rep["same_sql"] and rep["same_data_version"]


def test_rejected_definition_cannot_execute(db):
    d = engine.create_definition(CohortDefinition.model_validate(DEMO), user="r1@test")
    engine.decide(str(d["definition_id"]), "r2@test", d["sql_sha256"], approve=False, comment="no")
    with pytest.raises(engine.WorkflowError):
        engine.execute(str(d["definition_id"]), "r1@test")


def test_tampered_sql_refused(db):
    d = engine.create_definition(CohortDefinition.model_validate(DEMO), user="r1@test")
    engine.decide(str(d["definition_id"]), "r2@test", d["sql_sha256"], approve=True)
    with connect() as c:  # simulate tampering with the stored definition after approval
        c.execute("UPDATE cohort.definitions SET definition = jsonb_set(definition, '{inclusion,0,min_years}', '18') WHERE definition_id=%s",
                  (d["definition_id"],))
    with pytest.raises(engine.WorkflowError, match="differs from approved"):
        engine.execute(str(d["definition_id"]), "r1@test")
