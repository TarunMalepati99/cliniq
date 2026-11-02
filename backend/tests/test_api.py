import pytest
from fastapi.testclient import TestClient

from cliniq.api.app import app

ADMIN = {"Authorization": "Bearer dev-admin-token"}
RESEARCHER = {"Authorization": "Bearer dev-researcher-token"}
REVIEWER = {"Authorization": "Bearer dev-reviewer-token"}
VIEWER = {"Authorization": "Bearer dev-viewer-token"}


@pytest.fixture(scope="module")
def client(db):

    from cliniq.config import REPO_ROOT
    from cliniq.retrieval.index import index_corpus
    index_corpus(REPO_ROOT / "data" / "corpus")
    return TestClient(app)


def test_auth_required_and_roles(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/me", headers=VIEWER).json()["role"] == "viewer"
    assert client.post("/api/cohort/extract", json={"text": "NSCLC"}, headers=VIEWER).status_code == 403
    assert client.post("/api/ingest/sample", headers=RESEARCHER).status_code == 403
    assert client.post("/api/eval/run", json={}, headers=RESEARCHER).status_code == 403


def test_request_id_propagation(client):
    r = client.get("/api/health", headers={"X-Request-ID": "11111111-2222-3333-4444-555555555555"})
    assert r.headers["X-Request-ID"] == "11111111-2222-3333-4444-555555555555"
    assert r.json()["disclaimer"].startswith("CLINIQ is a research")


def test_end_to_end_cohort_workflow(client):
    text = "Find patients aged 40-70 with NSCLC and an EGFR exon 19 deletion, excluding brain metastases"
    ext = client.post("/api/cohort/extract", json={"text": text}, headers=RESEARCHER).json()
    assert ext["valid"]
    d = client.post("/api/cohort/definitions", json={"definition": ext["definition"], "nl_request": text, "extraction": ext,
                                                     "extractor_version": ext["version"]}, headers=RESEARCHER).json()
    did = d["definition_id"]
    assert client.post(f"/api/cohort/definitions/{did}/execute", headers=RESEARCHER).status_code == 403  # not approved
    rv = client.post(f"/api/cohort/definitions/{did}/review", json={"sql_sha256": d["sql_sha256"], "approve": True}, headers=REVIEWER)
    assert rv.status_code == 200 and rv.json()["status"] == "approved"
    run = client.post(f"/api/cohort/definitions/{did}/execute", headers=RESEARCHER).json()
    assert run["status"] == "succeeded" and run["summary"]["funnel"]
    pts = client.get(f"/api/cohort/runs/{run['cohort_run_id']}/patients?classification=ineligible&limit=5", headers=VIEWER).json()
    assert all(p["classification"] == "ineligible" for p in pts)
    rep = client.post(f"/api/cohort/runs/{run['cohort_run_id']}/reproduce", headers=RESEARCHER).json()
    assert rep["identical_result"]
    evd = client.post("/api/evidence/definition", json={"definition_id": did}, headers=VIEWER).json()
    assert {i["status"] for i in evd["items"]} <= {"supported", "insufficient_evidence"}


def test_invalid_definition_rejected_by_api(client):
    bad = {"definition": {"inclusion": [{"type": "diagnosis", "condition": "x; DROP TABLE clinical.person"}]}}
    assert client.post("/api/cohort/definitions", json=bad, headers=RESEARCHER).status_code == 422


def test_upload_rejects_bad_filenames(client):
    r = client.post("/api/ingest/upload", files={"files": ("../../etc/passwd.sh", b"x")}, headers=ADMIN)
    assert r.status_code == 400


def test_upload_fhir_bundle(client):
    import json
    bundle = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": {"resourceType": "Patient", "id": "api-up-1", "gender": "male", "birthDate": "1959-04-02"}},
        {"resource": {"resourceType": "Condition", "id": "api-up-c1", "subject": {"reference": "Patient/api-up-1"},
                      "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "C61"}]}, "onsetDateTime": "2023-02-01"}}]}
    r = client.post("/api/ingest/upload?wait=true", files={"files": ("api_upload.json", json.dumps(bundle).encode())}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["metrics"]["records_loaded_new"] == 2
    det = client.get("/api/data/patients/api-up-1", headers=VIEWER).json()
    lin = client.get(f"/api/data/lineage/{det['conditions'][0]['source_record_id']}", headers=VIEWER).json()
    assert lin["file_name"] == "api_upload.json"


def test_monitoring_and_evidence_search(client):
    m = client.get("/api/monitoring/overview", headers=VIEWER).json()
    assert m["row_counts"]["person"] > 0 and "dead_letter" in m and m["query_performance"]["api"]
    s = client.get("/api/evidence/search", params={"q": "EGFR exon 19 deletion"}, headers=VIEWER).json()
    assert s["results"] and not s["insufficient_evidence"]


def test_background_job_lifecycle(client):
    import time
    r = client.post("/api/evidence/reindex", headers=ADMIN)
    assert r.status_code == 202
    jid = r.json()["job_id"]
    for _ in range(100):
        j = client.get(f"/api/jobs/{jid}", headers=ADMIN).json()
        if j["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.1)
    assert j["status"] == "succeeded" and j["result"]["status"] == "succeeded"
