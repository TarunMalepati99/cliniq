"""Build the CLINIQ evaluation benchmarks (deterministic).

extraction_gold.jsonl  ≥100 natural-language cohort requests with gold CohortDefinitions
  - subset 'handwritten' (35): free phrasing written by hand, including deliberately hard
    cases (nested negation, OR-lists, histology in parentheses, pregnancy exclusions).
  - subset 'templated' (90): generated from a seeded phrase grammar; gold is derived from
    the same parameters, so it is correct by construction but shares vocabulary with the
    rule lexicon (leakage risk → always report subsets separately).
retrieval_gold.jsonl   paraphrased evidence queries with doc-level relevance derived from
  corpus concept metadata, plus unanswerable queries (expected: insufficient evidence).

review_status: 'author-reviewed' means the gold was checked by the author who also wrote
the extractor. Independent clinical review has NOT been performed; reviewers can flip the
field to 'independently-reviewed' after checking an item.

Run:  python eval/benchmark/build_benchmark.py
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "backend"))
from cliniq.cohort.models import CohortDefinition  # noqa: E402
from cliniq.retrieval.corpus import load_corpus  # noqa: E402


def A(lo=None, hi=None): return {"type": "age", "min_years": lo, "max_years": hi}
def S(s): return {"type": "sex", "sex": s}
def D(c): return {"type": "diagnosis", "condition": c}
def B(m, status="detected"): return {"type": "biomarker", "markers": m if isinstance(m, list) else [m], "status": status}
def L(lab, op, v, vmax=None): return {"type": "lab", "lab": lab, "op": op, "value": v, **({"value_max": vmax} if vmax is not None else {})}
def T(c): return {"type": "treatment", "drug_class": c}
def Tx(d): return {"type": "treatment", "drug": d}
def P(c): return {"type": "procedure", "procedure_class": c}
def G(*s): return {"type": "geography", "states": list(s)}


EGFR = ["egfr_exon19del", "egfr_l858r"]
BRCA = ["brca1_pathogenic", "brca2_pathogenic"]

HANDWRITTEN = [
    ("Find patients aged 40–70 diagnosed with non-small-cell lung cancer, with an EGFR exon 19 deletion, and a documented history of prior treatment.",
     [A(40, 70), D("nsclc"), B("egfr_exon19del"), T("any_systemic_therapy")], [], "Example from the project brief."),
    ("Women over 50 with HER2-positive breast cancer who have not received trastuzumab, excluding anyone with heart failure.",
     [A(51), S("female"), B("erbb2_amplification"), D("breast_cancer")], [Tx("trastuzumab"), D("heart_failure")], ""),
    ("ALK-negative NSCLC patients with ECOG 0-1 and no prior immunotherapy",
     [B("alk_fusion", "not_detected"), D("nsclc"), L("ecog", "<=", 1)], [T("immunotherapy")], ""),
    ("Treatment-naive colorectal cancer with MSI-H status in Indiana or Ohio",
     [D("colorectal_cancer"), B("msi_high"), G("IN", "OH")], [T("any_systemic_therapy")], ""),
    ("Adults with KRAS G12C lung adenocarcinoma previously treated with platinum-based chemotherapy, without chronic kidney disease, and creatinine below 1.5 mg/dL.",
     [A(18), B("kras_g12c"), D("nsclc"), T("platinum_chemotherapy"), L("creatinine", "<", 1.5)], [D("ckd")], ""),
    ("Melanoma patients with BRAF V600E who received prior checkpoint inhibitors",
     [D("melanoma"), B("braf_v600e"), T("immunotherapy")], [], ""),
    ("Postmenopausal women with breast cancer on endocrine therapy and a germline BRCA mutation",
     [S("female"), D("breast_cancer"), T("endocrine_therapy"), B(BRCA)], [], "Menopausal status itself is not modeled; 'postmenopausal women' maps to sex only."),
    ("Men 65 or older with prostate cancer and a BRCA2 mutation, excluding prior PARP inhibitors",
     [A(65), S("male"), D("prostate_cancer"), B("brca2_pathogenic")], [T("parp_inhibitor")], ""),
    ("NSCLC with PD-L1 TPS >= 50% and no brain metastases",
     [D("nsclc"), L("pdl1_tps", ">=", 50)], [D("brain_metastases")], ""),
    ("Patients with pancreatic adenocarcinoma, ECOG 0 to 1, hemoglobin at least 9 g/dL, platelet count of at least 100,000 per microliter",
     [D("pancreatic_cancer"), L("ecog", "<=", 1), L("hemoglobin", ">=", 9), L("platelets", ">=", 100)], [], ""),
    ("Small cell lung cancer patients aged 18 to 75 who have received cisplatin",
     [D("sclc"), A(18, 75), Tx("cisplatin")], [], ""),
    ("Identify EGFR-mutant NSCLC patients who progressed on osimertinib and have a T790M mutation",
     [B(EGFR), D("nsclc"), Tx("osimertinib"), B("egfr_t790m")], [], "Progression is not modeled; mapped to treatment history."),
    ("Breast cancer patients who are HER2-negative, aged under 60",
     [D("breast_cancer"), B("erbb2_amplification", "not_detected"), A(None, 59)], [], ""),
    ("Colorectal cancer patients with a BRAF V600E mutation previously treated with oxaliplatin, excluding those with heart failure or HIV",
     [D("colorectal_cancer"), B("braf_v600e"), Tx("oxaliplatin")], [D("heart_failure"), D("hiv")], ""),
    ("Lung cancer patients of any histology with prior radiotherapy",
     [D("lung_cancer"), P("radiation_therapy")], [], ""),
    ("NSCLC patients with interstitial lung disease",
     [D("nsclc"), D("interstitial_lung_disease")], [], ""),
    ("Exclusion criteria: brain metastases; heart failure; HIV. Inclusion criteria: NSCLC with an ALK rearrangement.",
     [D("nsclc"), B("alk_fusion")], [D("brain_metastases"), D("heart_failure"), D("hiv")], "Header-structured request."),
    ("Patients with type 2 diabetes and HbA1c above 8%",
     [D("type2_diabetes"), L("hba1c", ">", 8)], [], ""),
    ("Find people with cirrhosis and total bilirubin over 2 mg/dL",
     [D("cirrhosis"), L("bilirubin", ">", 2)], [], ""),
    ("Patients with NSCLC who never received an EGFR TKI and are EGFR wild-type",
     [D("nsclc"), B(EGFR, "not_detected")], [T("egfr_tki")], "Hard: negation scope must end before 'and are'."),
    ("Patients aged 30-50 with stage IV colorectal cancer, life expectancy of at least 12 weeks",
     [A(30, 50), D("colorectal_cancer")], [], "Stage and life expectancy are not representable (expected unsupported)."),
    ("Older adults with hypertension living in Texas",
     [A(65), D("hypertension"), G("TX")], [], "'Older adults' → age ≥ 65 by convention."),
    ("ROS1-rearranged NSCLC, excluding pregnant women",
     [B("ros1_fusion"), D("nsclc")], [], "Hard: pregnancy is unsupported; must NOT become an exclusion of all women."),
    ("patients >= 18 with HIV and Kaposi sarcoma",
     [A(18), D("hiv")], [], "Kaposi sarcoma is outside the vocabulary."),
    ("Individuals with heart failure treated with apixaban, no history of CKD",
     [D("heart_failure"), Tx("apixaban")], [D("ckd")], ""),
    ("NSCLC, KRAS G12C, prior platinum chemo and prior immunotherapy, ECOG ≤ 1, no untreated brain metastases",
     [D("nsclc"), B("kras_g12c"), T("platinum_chemotherapy"), T("immunotherapy"), L("ecog", "<=", 1)], [D("brain_metastases")],
     "'untreated' qualifier cannot be represented; maps to any brain metastases."),
    ("Patients with eGFR >= 60 and creatinine less than 1.2 mg/dL who have chronic kidney disease",
     [L("egfr_renal", ">=", 60), L("creatinine", "<", 1.2), D("ckd")], [], ""),
    ("Find prostate cancer patients previously treated with enzalutamide or abiraterone",
     [D("prostate_cancer"), T("antiandrogen")], [], "Hard: OR over drugs equals the drug class."),
    ("Patients diagnosed with melanoma who have had surgical resection, aged between 25 and 80",
     [D("melanoma"), P("surgical_resection"), A(25, 80)], [], ""),
    ("Lung cancer (non-small cell) with L858R, untreated",
     [D("nsclc"), B("egfr_l858r")], [T("any_systemic_therapy")], "Hard: histology given in parentheses."),
    ("Patients with an EGFR mutation and hemoglobin of at least 100 g/L",
     [B(EGFR), L("hemoglobin", ">=", 10)], [], "Unit conversion g/L → g/dL."),
    ("NSCLC patients with WBC above 3 and platelets above 75",
     [D("nsclc"), L("wbc", ">", 3), L("platelets", ">", 75)], [], ""),
    ("Colon cancer patients that are microsatellite stable",
     [D("colorectal_cancer"), B("msi_high", "not_detected")], [], ""),
    ("Patients with HER2+ breast cancer previously treated with a taxane, excluding brain mets",
     [B("erbb2_amplification"), D("breast_cancer"), T("taxane")], [D("brain_metastases")], ""),
    ("Women aged 35 or younger with breast cancer and BRCA1",
     [A(None, 35), S("female"), D("breast_cancer"), B("brca1_pathogenic")], [], ""),
]

DISEASES = {
    "nsclc": ["non-small cell lung cancer", "NSCLC", "non-small-cell lung carcinoma"],
    "sclc": ["small cell lung cancer", "SCLC"],
    "breast_cancer": ["breast cancer", "breast carcinoma"],
    "colorectal_cancer": ["colorectal cancer", "colon cancer", "CRC"],
    "melanoma": ["melanoma", "cutaneous melanoma"],
    "pancreatic_cancer": ["pancreatic adenocarcinoma", "pancreatic cancer"],
    "prostate_cancer": ["prostate cancer", "prostate adenocarcinoma"],
}
BIOMARKERS = {
    "nsclc": [("an EGFR exon 19 deletion", B("egfr_exon19del")), ("EGFR ex19del", B("egfr_exon19del")), ("EGFR L858R", B("egfr_l858r")),
              ("KRAS G12C", B("kras_g12c")), ("an ALK rearrangement", B("alk_fusion")), ("ALK-positive disease", B("alk_fusion")),
              ("a ROS1 fusion", B("ros1_fusion")), ("BRAF V600E", B("braf_v600e")), ("EGFR-mutant disease", B(EGFR)),
              ("ALK-negative status", B("alk_fusion", "not_detected")), ("EGFR wild-type status", B(EGFR, "not_detected"))],
    "breast_cancer": [("HER2-positive disease", B("erbb2_amplification")), ("HER2-negative status", B("erbb2_amplification", "not_detected")),
                      ("a BRCA1/2 mutation", B(BRCA)), ("a BRCA2 mutation", B("brca2_pathogenic"))],
    "colorectal_cancer": [("MSI-H tumors", B("msi_high")), ("BRAF V600E", B("braf_v600e")), ("KRAS G12C", B("kras_g12c")),
                          ("microsatellite stable tumors", B("msi_high", "not_detected"))],
    "melanoma": [("BRAF V600E", B("braf_v600e")), ("a BRAF mutation", B("braf_v600e"))],
    "pancreatic_cancer": [("a germline BRCA mutation", B(BRCA))],
    "prostate_cancer": [("a BRCA2 mutation", B("brca2_pathogenic"))],
    "sclc": [],
}
TX_INCL = {
    "*": [("previously treated", T("any_systemic_therapy")), ("with prior platinum-based chemotherapy", T("platinum_chemotherapy")),
          ("who received prior immunotherapy", T("immunotherapy"))],
    "nsclc": [("previously treated with osimertinib", Tx("osimertinib")), ("after an EGFR TKI", T("egfr_tki"))],
    "breast_cancer": [("previously treated with trastuzumab", Tx("trastuzumab")), ("on endocrine therapy", T("endocrine_therapy"))],
    "prostate_cancer": [("previously treated with enzalutamide", Tx("enzalutamide"))],
}
LABS = [("ECOG 0-1", L("ecog", "<=", 1)), ("ECOG performance status of 0 or 1", L("ecog", "<=", 1)), ("creatinine < 1.5 mg/dL", L("creatinine", "<", 1.5)),
        ("hemoglobin >= 9 g/dL", L("hemoglobin", ">=", 9)), ("hemoglobin of at least 90 g/L", L("hemoglobin", ">=", 9)),
        ("platelets >= 100,000/uL", L("platelets", ">=", 100)), ("total bilirubin below 1.5 mg/dL", L("bilirubin", "<", 1.5)),
        ("ECOG <= 2", L("ecog", "<=", 2))]
EXCL = [("brain metastases", D("brain_metastases")), ("heart failure", D("heart_failure")), ("HIV", D("hiv")),
        ("chronic kidney disease", D("ckd")), ("interstitial lung disease", D("interstitial_lung_disease")), ("prior immunotherapy", T("immunotherapy")),
        ("cirrhosis", D("cirrhosis"))]
INTROS = ["Find patients", "Identify patients", "How many patients", "Show me patients", "I need patients", "Count patients"]


def templated(n: int = 90, seed: int = 2026) -> list[dict]:
    rng = random.Random(seed)
    items = []
    while len(items) < n:
        dz = rng.choice(list(DISEASES))
        inc: list[dict] = []
        exc: list[dict] = []
        parts = [rng.choice(INTROS)]
        sexw = ""
        if dz == "breast_cancer" and rng.random() < 0.3:
            sexw, _ = "women", inc.append(S("female"))
        elif dz == "prostate_cancer" and rng.random() < 0.3:
            sexw, _ = "men", inc.append(S("male"))
        r = rng.random()
        age = ""
        if r < 0.25:
            a, b = rng.choice([(18, 65), (40, 70), (30, 75), (50, 80)])
            age = rng.choice([f"aged {a}-{b}", f"between {a} and {b} years old"])
            inc.append(A(a, b))
        elif r < 0.4:
            a = rng.choice([18, 21, 50, 65])
            age = f"aged {a} or older"
            inc.append(A(a))
        elif r < 0.5:
            a = rng.choice([40, 60, 70])
            age = f"over {a}"
            inc.append(A(a + 1))
        elif r < 0.58:
            b = rng.choice([50, 65, 75])
            age = f"under {b}"
            inc.append(A(None, b - 1))
        if sexw:
            parts[0] = parts[0].replace("patients", sexw)
        subject = " ".join(x for x in [parts[0], age] if x)
        text = f"{subject} with {rng.choice(DISEASES[dz])}"
        inc.append(D(dz))
        if BIOMARKERS[dz] and rng.random() < 0.8:
            p, c = rng.choice(BIOMARKERS[dz])
            text += f" and {p}"
            inc.append(c)
        if rng.random() < 0.15:
            text += ", treatment-naive"
            exc.append(T("any_systemic_therapy"))
        elif rng.random() < 0.55:
            p, c = rng.choice(TX_INCL["*"] + TX_INCL.get(dz, []))
            text += f", {p}"
            inc.append(c)
        if rng.random() < 0.45:
            for p, c in rng.sample(LABS, rng.choice([1, 1, 2])):
                if c["lab"] not in {x.get("lab") for x in inc}:
                    text += f", {p}"
                    inc.append(c)
        if rng.random() < 0.15:
            st = rng.choice([("in Indiana", G("IN")), ("from Texas or California", G("CA", "TX")), ("living in Ohio", G("OH"))])
            text += f", {st[0]}"
            inc.append(st[1])
        if rng.random() < 0.55:
            choices = [e for e in EXCL if e[1] not in exc and e[1] not in inc and not (e[1]["type"] == "treatment" and any(x["type"] == "treatment" for x in exc))]
            picks = rng.sample(choices, rng.choice([1, 1, 2]))
            names = " and ".join(p for p, _ in picks)
            form = rng.choice(["; excluding patients with {}", ", without {}", ", excluding {}"])
            text += form.format(names)
            exc += [c for _, c in picks]
        text += rng.choice([".", "", "."])
        try:
            CohortDefinition.model_validate({"inclusion": inc, "exclusion": exc})
        except Exception:
            continue
        items.append((text, inc, exc, ""))
    return items


def main() -> None:
    out = []
    for i, (text, inc, exc, note) in enumerate(HANDWRITTEN, 1):
        CohortDefinition.model_validate({"inclusion": inc, "exclusion": exc})
        out.append({"id": f"H{i:03d}", "subset": "handwritten", "request": text, "gold": {"inclusion": inc, "exclusion": exc},
                    "notes": note, "review_status": "author-reviewed"})
    for i, (text, inc, exc, note) in enumerate(templated(), 1):
        out.append({"id": f"T{i:03d}", "subset": "templated", "request": text, "gold": {"inclusion": inc, "exclusion": exc},
                    "notes": note, "review_status": "generated"})
    (HERE / "extraction_gold.jsonl").write_text("".join(json.dumps(x) + "\n" for x in out), encoding="utf-8")

    corpus = load_corpus(HERE.parents[1] / "data" / "corpus")
    by_concept: dict[str, list[str]] = {}
    for d in corpus:
        for c in d["concepts"]:
            by_concept.setdefault(c, []).append(d["doc_id"])
    queries = [
        ("trials enrolling tumors with an exon 19 deletion of EGFR", "egfr_exon19del"),
        ("studies requiring the L858R substitution", "egfr_l858r"),
        ("eligibility after acquired T790M resistance", "egfr_t790m"),
        ("KRAS codon 12 cysteine substitution studies", "kras_g12c"),
        ("ALK fusion positive lung cancer trials", "alk_fusion"),
        ("ROS1 rearranged tumors", "ros1_fusion"),
        ("BRAF V600E mutant cancers", "braf_v600e"),
        ("HER2 amplified breast cancer eligibility", "erbb2_amplification"),
        ("germline BRCA1 or BRCA2 carriers", "brca"),
        ("mismatch repair deficient tumors", "msi_high"),
        ("high PD-L1 expression requirement", "pdl1_high"),
        ("patients who progressed after platinum doublet chemotherapy", "prior_platinum"),
        ("prior EGFR tyrosine kinase inhibitor exposure required", "prior_egfr_tki"),
        ("previously received anti-PD-1 checkpoint blockade", "prior_io"),
        ("first-line studies for patients without prior systemic therapy", "treatment_naive"),
        ("progressed on aromatase inhibitor or other endocrine therapy", "prior_endocrine"),
        ("exclusion of untreated CNS metastases", "ex_brain_mets"),
        ("pneumonitis or interstitial lung disease exclusion", "ex_ild"),
        ("NYHA class III-IV heart failure excluded", "ex_heart_failure"),
        ("uncontrolled HIV infection exclusion", "ex_hiv"),
        ("renal function threshold creatinine clearance", "ex_renal"),
        ("liver function bilirubin and transaminase limits", "ex_hepatic"),
        ("performance status zero or one", "ecog_01"),
        ("ECOG up to 2 allowed", "ecog_02"),
        ("minimum hemoglobin requirement", "hemoglobin_9"),
        ("platelet count threshold", "platelets_100"),
        ("non-small cell lung carcinoma trials", "nsclc"),
        ("extensive-stage small-cell lung cancer", "sclc"),
        ("colorectal adenocarcinoma studies", "colorectal_cancer"),
        ("pancreatic ductal adenocarcinoma", "pancreatic_cancer"),
        ("castration-resistant prostate cancer", "prostate_cancer"),
        ("advanced cutaneous melanoma", "melanoma"),
        ("invasive breast carcinoma trials", "breast_cancer"),
        ("how to interpret untested biomarkers versus negative results", "missing_data"),
        ("medication history incomplete when treated elsewhere", "prior_platinum"),
        ("what does ECOG 2 mean", "ecog_02"),
    ]
    unanswerable = ["CAR-T cell therapy for multiple myeloma", "pediatric asthma inhaler adherence", "hip replacement rehabilitation protocols",
                    "influenza vaccine immunogenicity in infants", "glaucoma eye drop dosing", "dental implant osseointegration"]
    rows = []
    for i, (q, concept) in enumerate(queries, 1):
        rel = sorted(set(by_concept.get(concept, [])))
        assert rel, f"no relevant docs for {concept}"
        rows.append({"id": f"R{i:03d}", "query": q, "concept": concept, "relevant_doc_ids": rel, "answerable": True})
    for j, q in enumerate(unanswerable, 1):
        rows.append({"id": f"U{j:03d}", "query": q, "concept": None, "relevant_doc_ids": [], "answerable": False})
    (HERE / "retrieval_gold.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
    print(f"extraction items: {len(out)} (handwritten {len(HANDWRITTEN)}), retrieval queries: {len(rows)}")


if __name__ == "__main__":
    main()
