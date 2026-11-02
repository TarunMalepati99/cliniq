import json
from datetime import date
from pathlib import Path

import pytest

from cliniq.db import fetch_all, fetch_one
from cliniq.ingest import transform as T
from cliniq.ingest.pipeline import ingest_paths, retry_dead_letters


# ---------------------------------------------------------------- unit: transform

def _obs(code="2160-0", value=1.0, unit="mg/dL", when="2025-01-01"):
    return {"resourceType": "Observation", "id": "o1", "status": "final", "subject": {"reference": "Patient/p1"},
            "code": {"coding": [{"system": "http://loinc.org", "code": code}]}, "effectiveDateTime": when,
            "valueQuantity": {"value": value, "unit": unit}}


def test_unit_conversion_creatinine_umol():
    m = T.map_fhir(_obs(value=88.42, unit="umol/L"), 1, "r")
    assert m.row["value_numeric"] == pytest.approx(1.0)
    assert m.row["source_unit"] == "umol/L" and m.row["unit"] == "mg/dL"
    assert m.warnings[0][0] == "unit_converted"


@pytest.mark.parametrize("payload,stage,code", [
    (_obs(value=-1), "business_rule", "implausible_value"),
    (_obs(unit="mmol/L"), "standardization", "unknown_unit"),
    (_obs(when="2024-13-45"), "schema", "invalid_field"),
    (_obs(when="2099-01-01"), "business_rule", "future_date"),
    ({"resourceType": "Condition", "id": "c1", "subject": {"reference": "Patient/p1"}}, "schema", "missing_required_field"),
    ({"resourceType": "NotAFhirType", "id": "e1"}, "schema", "unsupported_resource_type"),
])
def test_rejections(payload, stage, code):
    with pytest.raises(T.RecordRejected) as ei:
        T.map_fhir(payload, 1, "r")
    assert (ei.value.stage, ei.value.code) == (stage, code)


def test_out_of_scope_is_not_a_rejection():
    with pytest.raises(T.OutOfScope):
        T.map_fhir({"resourceType": "Encounter", "id": "e1"}, 1, "r")
    with pytest.raises(T.OutOfScope):  # valid Observation whose code is outside the vocabulary (e.g. a vital sign)
        T.map_fhir(_obs(code="8867-4", unit="/min", value=72), 1, "r")


def test_urn_uuid_subject_resolved_from_bundle_context():
    obs = _obs()
    obs["subject"] = {"reference": "urn:uuid:4f8e1c2a-0000-0000-0000-000000000001"}
    with pytest.raises(T.RecordRejected) as ei:
        T.map_fhir(obs, 1, "r")
    assert ei.value.code == "unresolvable_reference"
    m = T.map_fhir(obs, 1, "r", {"subject": "Patient/p-42"})
    assert m.person_id == "p-42"


def test_snomed_condition_crosswalk_keeps_source_code():
    c = {"resourceType": "Condition", "id": "c9", "subject": {"reference": "Patient/p1"}, "onsetDateTime": "2020-01-01",
         "code": {"coding": [{"system": "http://snomed.info/sct", "code": "44054006", "display": "Diabetes mellitus type 2 (disorder)"}]}}
    m = T.map_fhir(c, 1, "r")
    assert (m.row["vocabulary"], m.row["code"], m.row["source_vocabulary"], m.row["source_code"]) == ("ICD10CM", "E11.9", "SNOMED", "44054006")
    c["code"]["coding"][0]["code"] = "315268008"  # Suspected prostate cancer: deliberately not mapped
    m = T.map_fhir(c, 1, "r")
    assert m.row["vocabulary"] == "SNOMED" and m.warnings[0][0] == "snomed_not_in_crosswalk"


def test_medication_request_rxnorm_and_reference():
    mr = {"resourceType": "MedicationRequest", "id": "m1", "status": "active", "subject": {"reference": "Patient/p1"},
          "authoredOn": "2021-02-03T10:00:00-05:00",
          "medicationCodeableConcept": {"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "314076", "display": "lisinopril 10 MG Oral Tablet"}]}}
    m = T.map_fhir(mr, 1, "r")
    assert (m.row["drug_name"], m.row["drug_class"], m.row["vocabulary"]) == ("lisinopril", "antihypertensive", "RXNORM")
    del mr["medicationCodeableConcept"]
    mr["medicationReference"] = {"reference": "urn:uuid:aaaaaaaa-0000-0000-0000-000000000000"}
    m = T.map_fhir(mr, 1, "r", {"medication": {"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "1", "display": "Metformin 500 MG"}})
    assert m.row["drug_name"] == "metformin"


def test_partial_fhir_dates():
    from cliniq.ingest.fhir_models import parse_fhir_date
    assert parse_fhir_date("2024") == date(2024, 1, 1)
    assert parse_fhir_date("2024-05") == date(2024, 5, 1)
    assert parse_fhir_date("2024-05-07T10:00:00Z") == date(2024, 5, 7)


def test_note_fact_extraction():
    facts = T.extract_note_facts("Social history: former smoker. ECOG performance status 2. Also ECOG PS: 1.")
    assert {(f["fact_key"], f["fact_value"]) for f in facts} == {("smoking_status", "former"), ("ecog", "2"), ("ecog", "1")}


def test_patient_names_not_persisted():
    p = {"resourceType": "Patient", "id": "p1", "gender": "female", "birthDate": "1960-02-03",
         "name": [{"family": "Quill", "given": ["Nora"]}]}
    m = T.map_fhir(p, 1, "r")
    assert "Quill" not in json.dumps(m.row, default=str) and m.row["match_key"]


# ---------------------------------------------------------------- integration

def test_rejections_match_injected_fault_ledger(db, synth_dir: Path):
    manifest = json.loads((synth_dir / "manifest.json").read_text())
    m = db["metrics"]
    assert m["records_rejected"] == manifest["expected_rejections"]
    assert m["duplicates_skipped"] == manifest["injected_faults"]["exact_duplicate_resource"]
    assert m["records_updated"] == manifest["injected_faults"].get("updated_resource_version", 0)
    assert m["transformation_failures"] == 0
    assert db["status"] == "partial"  # partial == completed with rejections, never silent


def test_every_landed_record_has_terminal_status(db):
    r = fetch_one("SELECT count(*) AS n FROM raw.records WHERE process_status='pending'")
    assert r["n"] == 0


def test_lineage_every_clinical_row_traces_to_source_file(db):
    for table in ["person", "condition_occurrence", "measurement", "drug_exposure", "genomic_variant", "note"]:
        r = fetch_one(f"""SELECT count(*) AS n FROM clinical.{table} t
                          LEFT JOIN raw.records r ON r.raw_id = t.source_record_id
                          LEFT JOIN ops.source_files f ON f.source_file_id = r.source_file_id
                          WHERE f.file_name IS NULL""")
        assert r["n"] == 0, table


def test_probable_duplicate_persons_flagged(db, synth_dir: Path):
    manifest = json.loads((synth_dir / "manifest.json").read_text())
    n = fetch_one("SELECT count(*) AS n FROM ops.dq_issues WHERE check_name='probable_duplicate_person'")["n"]
    assert n == manifest["injected_faults"].get("probable_duplicate_person", 0)


def test_reingestion_is_idempotent(db, synth_dir: Path):
    before = {t: fetch_one(f"SELECT count(*) AS n FROM clinical.{t}")["n"] for t in ["person", "measurement", "genomic_variant"]}
    dlq_before = fetch_one("SELECT count(*) AS n FROM ops.dead_letter")["n"]
    again = ingest_paths([synth_dir], triggered_by="pytest-rerun")
    after = {t: fetch_one(f"SELECT count(*) AS n FROM clinical.{t}")["n"] for t in before}
    assert before == after
    assert again["metrics"]["records_landed"] == 0
    assert fetch_one("SELECT count(*) AS n FROM ops.dead_letter")["n"] == dlq_before


def test_dead_letter_retry_resolves_after_reference_arrives(db, tmp_path: Path):
    orphan = fetch_one("""SELECT dlq_id, error_detail->>'person_id' AS pid FROM ops.dead_letter
                          WHERE error_code='unresolved_patient_reference' AND status='open' LIMIT 1""")
    assert orphan is not None
    # the missing patient arrives later in a new file
    bundle = {"resourceType": "Bundle", "type": "collection", "entry": [{"resource": {
        "resourceType": "Patient", "id": orphan["pid"], "gender": "female", "birthDate": "1970-01-01"}}]}
    f = tmp_path / "late_patient.json"
    f.write_text(json.dumps(bundle))
    ingest_paths([f], triggered_by="pytest")
    out = retry_dead_letters([orphan["dlq_id"]], "pytest")
    assert out["resolved"] == 1
    assert fetch_one("SELECT status FROM ops.dead_letter WHERE dlq_id=%s", (orphan["dlq_id"],))["status"] == "resolved"


def test_corrupt_file_rejected_once(db):
    rows = fetch_all("SELECT count(*) AS n FROM ops.dead_letter WHERE stage='parse'")
    assert rows[0]["n"] == 1
