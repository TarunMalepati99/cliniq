"""Deterministic synthetic clinical data generator.

Produces, for a given seed:
  fhir/bundle_NNN.json   FHIR R4 'collection' Bundles (Patient, Condition, Observation,
                         MedicationStatement, Procedure)
  labs_feed.csv          lab results from a simulated LIS feed (subset of patients)
  genomics_feed.csv      biomarker results from a simulated sequencing-vendor feed
  notes.jsonl            synthetic clinical notes
  manifest.json          seed, counts and an exact ledger of injected faults

Every person, name and value is synthetic. No real patient data is used or derivable.
Faults are injected deliberately and counted so tests can assert the pipeline catches them.
"""
from __future__ import annotations

import csv
import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .. import vocabulary as V

AS_OF = date(2026, 1, 1)
GENERATOR_VERSION = "synth-1.0.0"

FIRST_F = ["Avery", "Maya", "Lena", "Priya", "Rosa", "Hana", "Ines", "Joan", "Kara", "Nora", "Olive", "Tess", "Uma", "Vera", "Wren", "Zara"]
FIRST_M = ["Aaron", "Bao", "Caleb", "Dev", "Eli", "Felix", "Gus", "Hugo", "Ivan", "Jon", "Kofi", "Luis", "Milo", "Nico", "Omar", "Raj"]
LAST = ["Ashdown", "Brightwater", "Calder", "Dunmore", "Elsworth", "Fairbanks", "Greystone", "Hollins", "Ironwood", "Juniper",
        "Kestrel", "Larkin", "Merriweather", "Northcott", "Oakhurst", "Pemberton", "Quill", "Ravensworth", "Stonebridge", "Thorne"]
STATE_WEIGHTS = {"IN": 14, "OH": 9, "IL": 10, "MI": 7, "KY": 5, "TX": 12, "CA": 13, "NY": 10, "FL": 9, "PA": 7, "GA": 4, "WA": 3, "MA": 3}

LOINC_GENETIC = "69548-6"     # Genetic variant assessment
LOINC_GENE_STUDIED = "48018-6"
LOINC_AA_CHANGE = "48005-3"
LA_PRESENT, LA_ABSENT = "LA9633-4", "LA9634-2"


@dataclass
class Ledger:
    """Exact record of injected faults; tests compare pipeline output against this."""
    faults: dict[str, int] = field(default_factory=dict)

    def add(self, key: str, n: int = 1) -> None:
        self.faults[key] = self.faults.get(key, 0) + n


@dataclass
class Truth:
    """Latent patient attributes (kept for documentation/debugging; never ingested)."""
    disease: str | None = None
    histology: str | None = None
    drivers: list[str] = field(default_factory=list)


def _d(x: date) -> str:
    return x.isoformat()


class Generator:
    def __init__(self, seed: int = 42, n_patients: int = 1200, bundle_size: int = 100) -> None:
        self.rng = random.Random(seed)
        self.seed = seed
        self.n = n_patients
        self.bundle_size = bundle_size
        self.ledger = Ledger()
        self.lab_rows: list[dict[str, Any]] = []
        self.genomic_rows: list[dict[str, Any]] = []
        self.notes: list[dict[str, Any]] = []
        self.truth: dict[str, Truth] = {}
        self._seq = 0

    # ---------------------------------------------------------------- helpers
    def _id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:07d}"

    def _p(self, prob: float) -> bool:
        return self.rng.random() < prob

    def _date_between(self, start: date, end: date) -> date:
        return start + timedelta(days=self.rng.randint(0, max(0, (end - start).days)))

    # ---------------------------------------------------------------- resources
    def _patient(self, pid: str, sex: str, birth: date | None, state: str, name: tuple[str, str]) -> dict[str, Any]:
        r: dict[str, Any] = {
            "resourceType": "Patient", "id": pid,
            "meta": {"source": "synthetic-ehr"},
            "identifier": [{"system": "https://cliniq.local/mrn", "value": f"MRN{pid[3:]}"}],
            "name": [{"family": name[1], "given": [name[0]]}],
            "gender": sex,
            "address": [{"state": state, "country": "US"}],
        }
        if birth:
            r["birthDate"] = _d(birth)
        return r

    def _condition(self, pid: str, icd: str, onset: date, histology: str | None) -> dict[str, Any]:
        r: dict[str, Any] = {
            "resourceType": "Condition", "id": self._id("cond"),
            "subject": {"reference": f"Patient/{pid}"},
            "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
            "code": {"coding": [{"system": V.SYSTEM_URI["ICD10CM"], "code": icd, "display": V.ICD10_CODES.get(icd, "")}]},
            "onsetDateTime": _d(onset),
        }
        if histology:
            r["extension"] = [{"url": "https://cliniq.local/fhir/StructureDefinition/histology",
                               "valueCodeableConcept": {"coding": [{"system": V.SYSTEM_URI["ICDO3"], "code": histology}]}}]
        return r

    def _lab_obs(self, pid: str, lab: V.Lab, value: float, unit: str, when: date) -> dict[str, Any]:
        return {
            "resourceType": "Observation", "id": self._id("obs"), "status": "final",
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory"}]}],
            "code": {"coding": [{"system": V.SYSTEM_URI[lab.vocabulary], "code": lab.code, "display": lab.label}]},
            "subject": {"reference": f"Patient/{pid}"},
            "effectiveDateTime": _d(when),
            "valueQuantity": {"value": round(value, 2), "unit": unit},
        }

    def _genomic_obs(self, pid: str, bm: V.Biomarker, result: str, when: date) -> dict[str, Any]:
        value = {"detected": {"system": "http://loinc.org", "code": LA_PRESENT, "display": "Present"},
                 "not_detected": {"system": "http://loinc.org", "code": LA_ABSENT, "display": "Absent"},
                 "indeterminate": {"system": V.SYSTEM_URI["CLINIQ"], "code": "indeterminate", "display": "Indeterminate"}}[result]
        comps = [
            {"code": {"coding": [{"system": "http://loinc.org", "code": LOINC_GENE_STUDIED}]}, "valueCodeableConcept": {"text": bm.gene}},
            {"code": {"coding": [{"system": V.BIOMARKER_SYSTEM, "code": "biomarker-key"}]}, "valueCodeableConcept": {"coding": [{"system": V.BIOMARKER_SYSTEM, "code": bm.key}]}},
        ]
        if bm.hgvs_p:
            comps.append({"code": {"coding": [{"system": "http://loinc.org", "code": LOINC_AA_CHANGE}]}, "valueCodeableConcept": {"text": bm.hgvs_p}})
        return {
            "resourceType": "Observation", "id": self._id("gen"), "status": "final",
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory"}]}],
            "code": {"coding": [{"system": "http://loinc.org", "code": LOINC_GENETIC, "display": "Genetic variant assessment"}]},
            "subject": {"reference": f"Patient/{pid}"},
            "effectiveDateTime": _d(when),
            "valueCodeableConcept": {"coding": [value]},
            "component": comps,
        }

    def _med(self, pid: str, drug: str, start: date, end: date | None) -> dict[str, Any]:
        r: dict[str, Any] = {
            "resourceType": "MedicationStatement", "id": self._id("med"), "status": "completed" if end else "active",
            "medicationCodeableConcept": {"coding": [{"system": V.DRUG_SYSTEM, "code": V.drug_code(drug), "display": drug}]},
            "subject": {"reference": f"Patient/{pid}"},
            "effectivePeriod": {"start": _d(start)},
        }
        if end:
            r["effectivePeriod"]["end"] = _d(end)
        return r

    def _procedure(self, pid: str, code: str, when: date) -> dict[str, Any]:
        name = dict(V.PROCEDURE_CLASSES[V.PROCEDURE_TO_CLASS[code]][1])[code]
        return {"resourceType": "Procedure", "id": self._id("proc"), "status": "completed",
                "code": {"coding": [{"system": V.PROCEDURE_SYSTEM, "code": code, "display": name}]},
                "subject": {"reference": f"Patient/{pid}"}, "performedDateTime": _d(when)}

    # ---------------------------------------------------------------- clinical story
    def _disease(self, sex: str) -> str | None:
        r = self.rng.random()
        table = [("lung", 0.32), ("breast", 0.17), ("colorectal", 0.14), ("melanoma", 0.07), ("pancreatic", 0.06), ("prostate", 0.11)]
        acc = 0.0
        for d, p in table:
            acc += p
            if r < acc:
                if d == "prostate" and sex != "male":
                    return "colorectal"
                if d == "breast" and sex == "male" and not self._p(0.03):
                    return "lung"
                return d
        return None

    def _biomarker_plan(self, disease: str | None, histology: str | None) -> dict[str, list[tuple[str, str]]]:
        """Returns gene -> list of (biomarker_key, result) for genes that were tested."""
        tested: dict[str, list[tuple[str, str]]] = {}
        drivers: list[str] = []

        def panel(gene: str, positive: str | None) -> None:
            res = []
            for key in V.GENE_PANELS[gene]:
                if self._p(0.03):
                    res.append((key, "indeterminate"))
                else:
                    res.append((key, "detected" if key == positive else "not_detected"))
            tested[gene] = res

        if disease == "lung" and histology in V.NSCLC_HISTOLOGY:
            adeno = histology == "8140/3"
            r = self.rng.random()
            driver = None
            if adeno:
                for key, p in [("egfr_exon19del", 0.12), ("egfr_l858r", 0.09), ("kras_g12c", 0.13), ("alk_fusion", 0.05),
                               ("ros1_fusion", 0.015), ("braf_v600e", 0.02)]:
                    if r < p:
                        driver = key
                        break
                    r -= p
            elif r < 0.03:
                driver = "kras_g12c"
            test_p = 0.82 if adeno else 0.40
            for gene in ["EGFR", "KRAS", "ALK", "ROS1", "BRAF"]:
                gene_has_driver = driver is not None and V.BIOMARKERS[driver].gene == gene
                if self._p(test_p if gene in ("EGFR", "KRAS", "ALK") else test_p * 0.75) or (gene_has_driver and self._p(0.9)):
                    panel(gene, driver if gene_has_driver else None)
            if driver:
                drivers.append(driver)
        elif disease == "breast":
            if self._p(0.9):
                panel("ERBB2", "erbb2_amplification" if self._p(0.18) else None)
            if self._p(0.4):
                panel("BRCA1", "brca1_pathogenic" if self._p(0.05) else None)
                panel("BRCA2", "brca2_pathogenic" if self._p(0.05) else None)
        elif disease == "colorectal":
            if self._p(0.8):
                panel("MSI", "msi_high" if self._p(0.12) else None)
            if self._p(0.7):
                panel("KRAS", "kras_g12c" if self._p(0.03) else None)
            if self._p(0.6):
                panel("BRAF", "braf_v600e" if self._p(0.08) else None)
        elif disease == "melanoma":
            if self._p(0.85):
                panel("BRAF", "braf_v600e" if self._p(0.45) else None)
        elif disease == "pancreatic":
            if self._p(0.5):
                panel("BRCA1", "brca1_pathogenic" if self._p(0.02) else None)
                panel("BRCA2", "brca2_pathogenic" if self._p(0.05) else None)
            if self._p(0.4):
                panel("MSI", "msi_high" if self._p(0.01) else None)
        elif disease == "prostate":
            if self._p(0.4):
                panel("BRCA2", "brca2_pathogenic" if self._p(0.10) else None)
        return tested

    def _treatments(self, disease: str | None, histology: str | None, positives: set[str], pdl1: float | None) -> list[str]:
        drugs: list[str] = []
        if disease is None:
            return drugs
        if self._p(0.18):
            return drugs  # treatment-naive
        if disease == "lung":
            if histology in V.SCLC_HISTOLOGY:
                drugs += ["cisplatin", "atezolizumab"]
            elif positives & {"egfr_exon19del", "egfr_l858r"} and self._p(0.88):
                drugs.append(self.rng.choice(["osimertinib", "osimertinib", "erlotinib", "gefitinib"]))
            elif "alk_fusion" in positives and self._p(0.85):
                drugs.append("alectinib")
            else:
                if "kras_g12c" in positives and self._p(0.4):
                    drugs.append("sotorasib")
                if self._p(0.65):
                    drugs += ["carboplatin", "pemetrexed" if histology == "8140/3" else "paclitaxel"]
                if (pdl1 is not None and pdl1 >= 50 and self._p(0.75)) or self._p(0.3):
                    drugs.append("pembrolizumab")
        elif disease == "breast":
            if "erbb2_amplification" in positives and self._p(0.85):
                drugs.append("trastuzumab")
            if self._p(0.6):
                drugs.append(self.rng.choice(["tamoxifen", "letrozole", "anastrozole"]))
            if self._p(0.4):
                drugs.append("paclitaxel")
            if positives & {"brca1_pathogenic", "brca2_pathogenic"} and self._p(0.5):
                drugs.append("olaparib")
        elif disease == "colorectal":
            if self._p(0.7):
                drugs += ["oxaliplatin", "fluorouracil"]
            if "msi_high" in positives and self._p(0.5):
                drugs.append("pembrolizumab")
        elif disease == "melanoma":
            if "braf_v600e" in positives and self._p(0.6):
                drugs.append("dabrafenib")
            if self._p(0.6):
                drugs.append(self.rng.choice(["nivolumab", "pembrolizumab"]))
        elif disease == "pancreatic":
            if self._p(0.75):
                drugs.append("gemcitabine")
        elif disease == "prostate":
            if self._p(0.45):
                drugs.append(self.rng.choice(["enzalutamide", "abiraterone"]))
            if "brca2_pathogenic" in positives and self._p(0.4):
                drugs.append("olaparib")
        return drugs

    def _lab_value(self, key: str, ctx: dict[str, bool]) -> float:
        g = self.rng.gauss
        if key == "creatinine":
            return max(0.4, g(2.6, 0.7) if ctx["ckd"] else g(0.95, 0.22))
        if key == "egfr_renal":
            return max(8, g(24, 6) if ctx["ckd"] else g(82, 16))
        if key == "hemoglobin":
            return max(6.5, g(11.6, 1.6) if ctx["cancer"] else g(13.6, 1.3))
        if key == "hba1c":
            return max(4.3, g(7.9, 1.2) if ctx["t2d"] else g(5.4, 0.35))
        if key in ("alt", "ast"):
            return max(6, g(85, 30) if ctx["cirrhosis"] else g(26, 9))
        if key == "bilirubin":
            return max(0.2, g(2.6, 0.9) if ctx["cirrhosis"] else g(0.7, 0.25))
        if key == "platelets":
            return max(25, g(105, 30) if ctx["cirrhosis"] else g(245, 60))
        if key == "wbc":
            return max(1.2, g(6.8, 1.9))
        raise KeyError(key)

    # ---------------------------------------------------------------- main
    def generate(self, out_dir: Path) -> dict[str, Any]:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "fhir").mkdir(exist_ok=True)
        resources_by_patient: list[list[dict[str, Any]]] = []
        demographics: list[tuple[str, str, date | None, str, tuple[str, str]]] = []
        states = list(STATE_WEIGHTS)
        weights = list(STATE_WEIGHTS.values())

        for i in range(1, self.n + 1):
            pid = f"pt-{i:05d}"
            sex = "female" if self._p(0.52) else "male"
            disease = self._disease(sex)
            age = int(min(92, max(22, self.rng.gauss(64 if disease else 52, 12))))
            birth: date | None = AS_OF - timedelta(days=age * 365 + self.rng.randint(0, 364))
            if self._p(0.02):
                birth = None
                self.ledger.add("patient_missing_birthdate")
            state = self.rng.choices(states, weights)[0]
            name = (self.rng.choice(FIRST_F if sex == "female" else FIRST_M), self.rng.choice(LAST))
            demographics.append((pid, sex, birth, state, name))
            res: list[dict[str, Any]] = [self._patient(pid, sex, birth, state, name)]
            truth = Truth(disease=disease)

            onset = self._date_between(date(2019, 1, 1), date(2025, 6, 30))
            histology = None
            if disease == "lung":
                histology = self.rng.choices(["8140/3", "8070/3", "8012/3", "8046/3", "8041/3"], [0.52, 0.25, 0.03, 0.05, 0.15])[0]
                truth.histology = histology
                icd = self.rng.choice(["C34.10", "C34.30", "C34.90"])
                documented = histology if self._p(0.9) else None
                if documented is None:
                    self.ledger.add("lung_histology_undocumented")
                res.append(self._condition(pid, icd, onset, documented))
            elif disease:
                icd = {"breast": self.rng.choice(["C50.911", "C50.912"]), "colorectal": self.rng.choice(["C18.9", "C20"]),
                       "melanoma": "C43.9", "pancreatic": "C25.9", "prostate": "C61"}[disease]
                hist = {"breast": "8500/3", "melanoma": "8720/3", "colorectal": "8140/3", "pancreatic": "8500/3", "prostate": "8140/3"}[disease]
                res.append(self._condition(pid, icd, onset, hist if self._p(0.8) else None))

            ctx = {"cancer": disease is not None, "ckd": self._p(0.07), "t2d": self._p(0.2), "cirrhosis": self._p(0.02)}
            comorbid = []
            if self._p(0.4):
                comorbid.append("I10")
            if ctx["t2d"]:
                comorbid.append("E11.9")
            if ctx["ckd"]:
                comorbid.append(self.rng.choice(["N18.4", "N18.9"]))
            if ctx["cirrhosis"]:
                comorbid.append("K74.60")
            if self._p(0.05):
                comorbid.append("I50.9")
            if self._p(0.01):
                comorbid.append("B20")
            if disease == "lung" and self._p(0.04):
                comorbid.append("J84.9")
            if (disease == "lung" and self._p(0.14)) or (disease in ("breast", "melanoma") and self._p(0.07)):
                comorbid.append("C79.31")
            for code in comorbid:
                res.append(self._condition(pid, code, self._date_between(date(2012, 1, 1), date(2025, 10, 1)), None))

            # Biomarkers: ~65% FHIR, ~35% vendor CSV feed
            tested = self._biomarker_plan(disease, histology)
            positives = {k for rs in tested.values() for k, r in rs if r == "detected"}
            truth.drivers = sorted(positives)
            report_date = onset + timedelta(days=self.rng.randint(7, 45))
            via_csv = self._p(0.35)
            for _gene, results in tested.items():
                for key, result in results:
                    if via_csv:
                        self.genomic_rows.append({"patient_id": pid, "gene": V.BIOMARKERS[key].gene, "biomarker": key,
                                                  "hgvs_p": V.BIOMARKERS[key].hgvs_p or "", "result": result,
                                                  "assay": "SynthSeq NGS v2", "reported_date": _d(report_date)})
                    else:
                        res.append(self._genomic_obs(pid, V.BIOMARKERS[key], result, report_date))

            pdl1 = None
            if disease == "lung" and histology in V.NSCLC_HISTOLOGY and self._p(0.7):
                pdl1 = float(self.rng.choices([0, self.rng.randint(1, 49), self.rng.randint(50, 100)], [0.3, 0.35, 0.35])[0])
                res.append(self._lab_obs(pid, V.LABS["pdl1_tps"], pdl1, "%", report_date))

            # Treatments; 8% of patients have no medication data at all (unknown history)
            drugs = self._treatments(disease, histology, positives, pdl1)
            # acquired T790M testing after first-generation TKI
            if ("erlotinib" in drugs or "gefitinib" in drugs) and self._p(0.6):
                res.append(self._genomic_obs(pid, V.BIOMARKERS["egfr_t790m"], "detected" if self._p(0.5) else "not_detected",
                                             report_date + timedelta(days=300)))
            if "I10" in comorbid and self._p(0.7):
                drugs.append("lisinopril")
            if ctx["t2d"] and self._p(0.8):
                drugs.append("metformin")
            if self._p(0.04):
                drugs.append("apixaban")
            if self._p(0.08):
                drugs = []
                self.ledger.add("patient_without_medication_records")
            t = report_date + timedelta(days=14)
            for drug in drugs:
                start = t + timedelta(days=self.rng.randint(0, 60))
                end = start + timedelta(days=self.rng.randint(60, 400)) if self._p(0.7) else None
                res.append(self._med(pid, drug, start, min(end, AS_OF) if end else None))

            if disease == "lung" and self._p(0.25):
                res.append(self._procedure(pid, "SURG-LOBECTOMY", onset + timedelta(days=30)))
            if disease == "breast" and self._p(0.5):
                res.append(self._procedure(pid, "SURG-MASTECTOMY", onset + timedelta(days=30)))
            if disease == "colorectal" and self._p(0.5):
                res.append(self._procedure(pid, "SURG-COLECTOMY", onset + timedelta(days=30)))
            if disease and self._p(0.3):
                res.append(self._procedure(pid, "RT-SRS" if "C79.31" in comorbid else "RT-THORAX", onset + timedelta(days=90)))

            # Labs: 10% have none; 30% of patients' labs come from the LIS CSV feed
            if not self._p(0.10):
                lab_via_csv = self._p(0.30)
                for k in range(self.rng.randint(1, 3)):
                    when = AS_OF - timedelta(days=self.rng.randint(5 + k * 120, 120 + k * 200))
                    for key in ["creatinine", "egfr_renal", "hemoglobin", "hba1c", "alt", "ast", "bilirubin", "platelets", "wbc"]:
                        if key == "hba1c" and not ctx["t2d"] and self._p(0.6):
                            continue
                        lab = V.LABS[key]
                        v = self._lab_value(key, ctx)
                        unit = lab.unit
                        if key == "creatinine" and self._p(0.15):
                            v, unit = v * 88.42, "umol/L"
                            self.ledger.add("unit_variant_creatinine_umol")
                        elif key == "hemoglobin" and self._p(0.10):
                            v, unit = v * 10, "g/L"
                            self.ledger.add("unit_variant_hemoglobin_gL")
                        if lab_via_csv:
                            self.lab_rows.append({"patient_id": pid, "loinc": lab.code, "test_name": lab.label,
                                                  "value": f"{v:.2f}", "unit": unit, "collected_date": _d(when)})
                        else:
                            res.append(self._lab_obs(pid, lab, v, unit, when))

            # ECOG structured (50% of cancer patients) or in note text (~30%)
            ecog = self.rng.choices([0, 1, 2, 3], [0.35, 0.4, 0.18, 0.07])[0] if disease else None
            ecog_in_note = False
            if ecog is not None:
                if self._p(0.5):
                    res.append(self._lab_obs(pid, V.LABS["ecog"], ecog, "{score}", AS_OF - timedelta(days=self.rng.randint(10, 200))))
                elif self._p(0.6):
                    ecog_in_note = True
            if disease:
                self.notes.append(self._note(pid, disease, ecog if ecog_in_note else None, histology))

            self.truth[pid] = truth
            resources_by_patient.append(res)

        self._inject_fhir_faults(resources_by_patient, demographics)
        bundles = self._write_bundles(out_dir, resources_by_patient)
        self._inject_csv_faults()
        self._write_csvs(out_dir)
        (out_dir / "fhir" / "bundle_corrupt.json").write_text('{"resourceType": "Bundle", "type": "collection", "entry": [ {"resource": ')
        self.ledger.add("corrupt_file")

        manifest = {
            "generator_version": GENERATOR_VERSION, "seed": self.seed, "n_patients": self.n, "as_of_date": _d(AS_OF),
            "files": {"fhir_bundles": bundles, "lab_rows": len(self.lab_rows), "genomic_rows": len(self.genomic_rows), "notes": len(self.notes)},
            "injected_faults": dict(sorted(self.ledger.faults.items())),
            "expected_rejections": self._expected_rejections(),
            "notice": "Entirely synthetic data generated for engineering demonstration. Not real patients.",
        }
        (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        (out_dir / "truth.json").write_text(json.dumps({k: v.__dict__ for k, v in self.truth.items()}, indent=0))
        return manifest

    def _note(self, pid: str, disease: str, ecog: int | None, histology: str | None) -> dict[str, Any]:
        smoke = self.rng.choice(["never smoker", "former smoker, quit 10 years ago", "current smoker, 1 pack per day", "former smoker"])
        dz = {"lung": "lung cancer", "breast": "breast cancer", "colorectal": "colorectal cancer", "melanoma": "melanoma",
              "pancreatic": "pancreatic adenocarcinoma", "prostate": "prostate cancer"}[disease]
        parts = [f"Oncology follow-up. Patient with {dz}.", f"Social history: {smoke}."]
        if histology in V.SCLC_HISTOLOGY:
            parts.append("Pathology consistent with small cell carcinoma.")
        if ecog is not None:
            parts.append(self.rng.choice([f"ECOG performance status {ecog}.", f"ECOG PS: {ecog}.", f"Performance status ECOG {ecog}."]))
        parts.append("Discussed plan of care. Synthetic note for demonstration only.")
        return {"note_id": self._id("note"), "patient_id": pid, "note_date": _d(AS_OF - timedelta(days=self.rng.randint(5, 180))),
                "note_type": "oncology_progress_note", "text": " ".join(parts)}

    def _inject_fhir_faults(self, by_patient: list[list[dict[str, Any]]], demographics: list[Any]) -> None:
        rng = self.rng
        flat_idx = [(i, j) for i, rs in enumerate(by_patient) for j, r in enumerate(rs) if r["resourceType"] != "Patient"]
        picks = rng.sample(flat_idx, 40)
        cursor = 0

        def take(n: int) -> list[tuple[int, int]]:
            nonlocal cursor
            out = picks[cursor:cursor + n]
            cursor += n
            return out

        for i, j in take(5):  # orphan reference -> reference rejection
            by_patient[i][j]["subject"] = {"reference": f"Patient/pt-9{rng.randint(1000, 9999)}"}
            self.ledger.add("orphan_reference")
        for i, j in take(5):  # missing required code -> schema rejection
            by_patient[i][j].pop("code", None)
            by_patient[i][j].pop("medicationCodeableConcept", None)
            self.ledger.add("missing_required_code")
        for i, j in take(4):  # unparseable date -> schema rejection
            r = by_patient[i][j]
            for k in ("onsetDateTime", "effectiveDateTime", "performedDateTime"):
                if k in r:
                    r[k] = "2024-13-45"
            if "effectivePeriod" in r:
                r["effectivePeriod"]["start"] = "not-a-date"
            self.ledger.add("invalid_date")
        # implausible lab values -> business-rule rejection (only on numeric lab observations)
        labs = [(i, j) for i, rs in enumerate(by_patient) for j, r in enumerate(rs)
                if r["resourceType"] == "Observation" and "valueQuantity" in r and (i, j) not in picks]
        for i, j in rng.sample(labs, 6):
            by_patient[i][j]["valueQuantity"]["value"] = -1.0 if rng.random() < 0.5 else 99999.0
            self.ledger.add("implausible_lab_value")
        # future dated condition -> business-rule rejection
        conds = [(i, j) for i, rs in enumerate(by_patient) for j, r in enumerate(rs)
                 if r["resourceType"] == "Condition" and (i, j) not in picks and "onsetDateTime" in r]
        for i, j in rng.sample(conds, 3):
            by_patient[i][j]["onsetDateTime"] = "2031-05-01"
            self.ledger.add("future_date")
        # unmapped (unknown) ICD-10 code -> loaded with data-quality warning
        for i, j in rng.sample(conds, 4):
            if by_patient[i][j]["onsetDateTime"] != "2031-05-01":
                by_patient[i][j]["code"]["coding"][0]["code"] = "Z99.89"
                self.ledger.add("unmapped_condition_code")
        # exact duplicate resources appended to *other* patients' lists (cross-bundle duplicates)
        for _ in range(15):
            i = rng.randrange(len(by_patient))
            src = [r for r in by_patient[i] if r["resourceType"] != "Patient"]
            if src:
                by_patient[rng.randrange(len(by_patient))].append(json.loads(json.dumps(rng.choice(src))))
                self.ledger.add("exact_duplicate_resource")
        # same id, changed content (a later correction) -> treated as update
        for _ in range(3):
            i = rng.randrange(len(by_patient))
            meds = [r for j, r in enumerate(by_patient[i]) if r["resourceType"] == "MedicationStatement" and (i, j) not in picks]
            if meds:
                upd = json.loads(json.dumps(meds[0]))
                upd["status"] = "stopped"
                upd["note"] = [{"text": "status corrected by source system"}]
                by_patient[rng.randrange(len(by_patient))].append(upd)
                self.ledger.add("updated_resource_version")
        # probable duplicate persons: same name/DOB/sex/state, new id
        for k in range(3):
            pid, sex, birth, state, name = demographics[rng.randrange(len(demographics))]
            if birth is None:
                continue
            by_patient.append([self._patient(f"pt-{self.n + 1 + k:05d}", sex, birth, state, name)])
            self.ledger.add("probable_duplicate_person")

    def _inject_csv_faults(self) -> None:
        rng = self.rng
        for row in rng.sample(self.lab_rows, 5):
            row["value"] = "n/a"
            self.ledger.add("csv_lab_non_numeric")
        for row in rng.sample(self.lab_rows, 3):
            row["loinc"] = "99999-9"
            self.ledger.add("csv_lab_unknown_code")
        for row in rng.sample(self.lab_rows, 2):
            row["patient_id"] = ""
            self.ledger.add("csv_lab_missing_patient")
        for row in rng.sample(self.genomic_rows, 3):
            row["result"] = "positive?"
            self.ledger.add("csv_genomic_invalid_result")
        for note in rng.sample(self.notes, 2):
            note["text"] = "   "
            self.ledger.add("empty_note")

    def _expected_rejections(self) -> int:
        reject_keys = ["orphan_reference", "missing_required_code", "invalid_date", "implausible_lab_value", "future_date",
                       "csv_lab_non_numeric", "csv_lab_unknown_code", "csv_lab_missing_patient", "csv_genomic_invalid_result",
                       "empty_note", "corrupt_file"]
        return sum(self.ledger.faults.get(k, 0) for k in reject_keys)

    def _write_bundles(self, out_dir: Path, by_patient: list[list[dict[str, Any]]]) -> int:
        n = 0
        for b in range(0, len(by_patient), self.bundle_size):
            entries = [{"fullUrl": f"urn:cliniq:{r['resourceType']}/{r['id']}", "resource": r}
                       for rs in by_patient[b:b + self.bundle_size] for r in rs]
            bundle = {"resourceType": "Bundle", "id": f"synthetic-{self.seed}-{n:03d}", "type": "collection", "entry": entries}
            (out_dir / "fhir" / f"bundle_{n:03d}.json").write_text(json.dumps(bundle, separators=(",", ":")))
            n += 1
        return n

    def _write_csvs(self, out_dir: Path) -> None:
        with (out_dir / "labs_feed.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["patient_id", "loinc", "test_name", "value", "unit", "collected_date"])
            w.writeheader()
            w.writerows(self.lab_rows)
        with (out_dir / "genomics_feed.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["patient_id", "gene", "biomarker", "hgvs_p", "result", "assay", "reported_date"])
            w.writeheader()
            w.writerows(self.genomic_rows)
        with (out_dir / "notes.jsonl").open("w") as f:
            for n in self.notes:
                f.write(json.dumps(n) + "\n")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--patients", type=int, default=1200)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    print(json.dumps(Generator(a.seed, a.patients).generate(a.out), indent=2))
