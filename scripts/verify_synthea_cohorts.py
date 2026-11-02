"""Independent verification of CLINIQ cohort results on Synthea data.

An *oracle* computes eligible patients directly from the raw Synthea FHIR JSON using only the
standard library — no CLINIQ code — and the result is compared, patient by patient, with what
CLINIQ returns after ingest → compile → approve → execute.

What this verifies: ingestion (reference resolution, crosswalk application, unit/plausibility
rules, dates), the SQL compiler, tri-state classification and execution are consistent with
the declared semantics. What it does NOT verify: whether the SNOMED→ICD-10 crosswalk itself is
clinically right (the oracle uses the same declared code lists, written out independently below).

Usage (database must already contain the ingested Synthea files):
  CLINIQ_DATABASE_URL=... CLINIQ_READER_DATABASE_URL=... \
  python scripts/verify_synthea_cohorts.py /path/to/synthea/output/fhir
"""
from __future__ import annotations

import glob
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

AS_OF = date(2026, 1, 1)

# Code lists written out independently of cliniq/vocabulary.py (same declared semantics).
# Creatinine: LOINC 2160-0 (serum/plasma) and 38483-4 (blood) are treated as equivalent.
HYPERTENSION = {"59621000"}
T2DM = {"44054006", "90781000119102", "157141000119108", "368581000119106", "1551000119108"}
CKD = {"431855005", "431856006", "433144002", "431857002", "46177005"}


def d(s: str | None) -> date | None:
    return date.fromisoformat(s[:10]) if s else None


def years(birth: date, asof: date) -> int:
    return asof.year - birth.year - ((asof.month, asof.day) < (birth.month, birth.day))


def load(fhir_dir: str) -> dict[str, dict]:
    pts: dict[str, dict] = {}
    for f in sorted(glob.glob(str(Path(fhir_dir) / "*.json"))):
        with open(f, encoding="utf-8") as fh:
            b = json.load(fh)
        ref = {e["fullUrl"]: e["resource"] for e in b["entry"] if "fullUrl" in e}
        p = next(e["resource"] for e in b["entry"] if e["resource"]["resourceType"] == "Patient")
        rec = {"birth": d(p.get("birthDate")), "sex": p.get("gender"), "conds": set(), "n_conds": 0,
               "creat": [], "meds": [], "n_meds": 0}
        for e in b["entry"]:
            r = e["resource"]
            t = r["resourceType"]
            if t == "Condition":
                on = d(r.get("onsetDateTime"))
                if on is None or on <= AS_OF:
                    rec["n_conds"] += 1
                    rec["conds"] |= {c["code"] for c in r["code"]["coding"]}
            elif t == "Observation" and any(c.get("code") in ("2160-0", "38483-4") for c in r["code"].get("coding", [])):
                vq = r.get("valueQuantity") or {}
                when = d(r.get("effectiveDateTime"))
                if vq.get("unit") == "mg/dL" and 0.1 <= vq.get("value", -1) <= 20 and when:  # CLINIQ's documented plausibility rule
                    primary = any(c.get("code") == "2160-0" for c in r["code"].get("coding", []))
                    rec["creat"].append((when, vq["value"], 0 if primary else 1))
            elif t == "MedicationRequest":
                cc = r.get("medicationCodeableConcept") or (ref.get((r.get("medicationReference") or {}).get("reference"), {}).get("code") or {})
                disp = " ".join(c.get("display", "") for c in cc.get("coding", []))
                start = d(r.get("authoredOn"))
                if start is None or start <= AS_OF:
                    rec["n_meds"] += 1
                    rec["meds"].append(disp.lower())
        pts[p["id"]] = rec
    return pts


def latest(values: list[tuple[date, float, int]], days: int) -> list[float] | None:
    """Values on the most recent date in the window, restricted to the best-priority code (2160-0 over 38483-4)."""
    win = [x for x in values if AS_OF - timedelta(days=days) < x[0] <= AS_OF]
    if not win:
        return None
    day = max(x[0] for x in win)
    best = min(x[2] for x in win if x[0] == day)
    return [x[1] for x in win if x[0] == day and x[2] == best]


def creat_status(p: dict, days: int) -> str:
    vals = latest(p["creat"], days)
    if vals is None:
        return "unknown"
    passes = [v < 1.5 for v in vals]
    return "met" if all(passes) else ("not_met" if not any(passes) else "unknown")


def oracle(pts: dict[str, dict]) -> dict[str, set[str]]:
    c1 = {pid for pid, p in pts.items() if p["birth"] and years(p["birth"], AS_OF) >= 40 and p["conds"] & HYPERTENSION
          and not (p["conds"] & CKD) and p["n_conds"] > 0}
    c2 = {pid for pid, p in pts.items() if p["conds"] & T2DM and creat_status(p, 365) == "met"}
    c3 = {pid for pid, p in pts.items() if p["sex"] == "female" and any(re.search(r"\blisinopril\b", m) for m in p["meds"])}
    c4 = {pid for pid, p in pts.items() if p["conds"] & T2DM and creat_status(p, 1825) == "met"}
    # potentially eligible = diabetic, nothing disqualifying, creatinine unknown (missing or conflicting same-day results)
    c2_pot = {pid for pid, p in pts.items() if p["conds"] & T2DM and creat_status(p, 365) == "unknown"}
    return {"htn_40plus_no_ckd": c1, "t2dm_creatinine_lt_1_5": c2, "female_prior_lisinopril": c3,
            "t2dm_creatinine_lt_1_5_5y": c4, "t2dm_creatinine_lt_1_5:potentially_eligible": c2_pot}


DEFINITIONS = {
    "htn_40plus_no_ckd": {"name": "Hypertension, age ≥ 40, no CKD", "inclusion": [{"type": "age", "min_years": 40},
                          {"type": "diagnosis", "condition": "hypertension"}], "exclusion": [{"type": "diagnosis", "condition": "ckd"}]},
    "t2dm_creatinine_lt_1_5": {"name": "Type 2 diabetes with creatinine < 1.5", "inclusion": [{"type": "diagnosis", "condition": "type2_diabetes"},
                               {"type": "lab", "lab": "creatinine", "op": "<", "value": 1.5, "lookback_days": 365}], "exclusion": []},
    "t2dm_creatinine_lt_1_5_5y": {"name": "Type 2 diabetes with creatinine < 1.5 (5-year lookback)",
                                  "inclusion": [{"type": "diagnosis", "condition": "type2_diabetes"},
                                                {"type": "lab", "lab": "creatinine", "op": "<", "value": 1.5, "lookback_days": 1825}], "exclusion": []},
    "female_prior_lisinopril": {"name": "Women with prior lisinopril", "inclusion": [{"type": "sex", "sex": "female"},
                                {"type": "treatment", "drug": "lisinopril"}], "exclusion": []},
}


def main() -> int:
    fhir_dir = sys.argv[1]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    from cliniq.cohort import engine
    from cliniq.cohort.models import CohortDefinition

    expected = oracle(load(fhir_dir))
    ok = True
    report = []
    for key, spec in DEFINITIONS.items():
        dfn = engine.create_definition(CohortDefinition.model_validate(spec), user="verifier@cliniq.local")
        engine.decide(str(dfn["definition_id"]), "reviewer@cliniq.local", dfn["sql_sha256"], approve=True, comment="synthea verification")
        run = engine.execute(str(dfn["definition_id"]), "verifier@cliniq.local")
        got = {p["person_id"] for p in engine.run_patients(str(run["cohort_run_id"]), "eligible", limit=100000)}
        exp = expected[key]
        match = got == exp
        ok &= match
        row = {"cohort": spec["name"], "oracle_eligible": len(exp), "cliniq_eligible": len(got), "identical": match,
               "only_in_cliniq": sorted(got - exp)[:10], "only_in_oracle": sorted(exp - got)[:10],
               "potentially_eligible": run["summary"]["potentially_eligible"], "query_ms": run["summary"]["query_ms"]}
        report.append(row)
        print(json.dumps(row))
        if f"{key}:potentially_eligible" in expected:
            exp_p = expected[f"{key}:potentially_eligible"]
            got_p = {p["person_id"] for p in engine.run_patients(str(run["cohort_run_id"]), "potentially_eligible", limit=100000)}
            ok &= got_p == exp_p
            print(json.dumps({"cohort": spec["name"] + " — potentially eligible", "oracle": len(exp_p), "cliniq": len(got_p),
                              "identical": got_p == exp_p}))
    print("ALL IDENTICAL" if ok else "MISMATCHES FOUND")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
