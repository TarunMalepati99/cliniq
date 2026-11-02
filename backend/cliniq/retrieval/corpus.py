"""Evidence corpus: synthetic trial eligibility documents + authored reference notes.

Synthetic trials are generated deterministically from a spec table with *varied phrasings*
of each concept (so lexical and semantic retrieval differ). Each document carries concept
metadata that defines retrieval gold relevance. Nothing here is a real trial; documents are
labeled as synthetic in their body and metadata. Real public trials can be added with
scripts/fetch_ctgov.py (ClinicalTrials.gov API v2), which writes into data/corpus/ctgov/.
"""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

PHRASES: dict[str, list[str]] = {
    "nsclc": ["non-small cell lung cancer (NSCLC)", "histologically confirmed NSCLC", "advanced non-squamous non-small-cell lung carcinoma"],
    "sclc": ["extensive-stage small cell lung cancer", "small-cell lung carcinoma"],
    "breast_cancer": ["breast cancer", "invasive breast carcinoma"],
    "colorectal_cancer": ["metastatic colorectal cancer", "adenocarcinoma of the colon or rectum"],
    "pancreatic_cancer": ["pancreatic ductal adenocarcinoma", "metastatic pancreatic cancer"],
    "melanoma": ["unresectable cutaneous melanoma", "advanced melanoma of the skin"],
    "prostate_cancer": ["metastatic castration-resistant prostate cancer", "prostate adenocarcinoma"],
    "egfr_exon19del": ["an EGFR exon 19 deletion", "an in-frame deletion within exon 19 of the EGFR gene", "EGFR ex19del"],
    "egfr_l858r": ["an EGFR L858R point mutation", "the exon 21 L858R substitution in EGFR"],
    "egfr_t790m": ["an acquired EGFR T790M resistance mutation", "T790M detected on tissue or plasma testing"],
    "kras_g12c": ["a KRAS G12C mutation", "a glycine-to-cysteine substitution at codon 12 of KRAS"],
    "alk_fusion": ["an ALK rearrangement", "an ALK gene fusion such as EML4-ALK", "ALK-positive disease"],
    "ros1_fusion": ["a ROS1 fusion", "a ROS1-rearranged tumor"],
    "braf_v600e": ["a BRAF V600E mutation", "BRAF V600E-mutant tumor"],
    "erbb2_amplification": ["HER2-positive (ERBB2-amplified) disease", "HER2 overexpression or ERBB2 gene amplification"],
    "brca": ["a germline BRCA1 or BRCA2 pathogenic variant", "a deleterious BRCA1/2 mutation"],
    "msi_high": ["MSI-high or mismatch repair-deficient (dMMR) tumor", "microsatellite instability-high status"],
    "pdl1_high": ["PD-L1 tumor proportion score of 50% or greater", "high PD-L1 expression (TPS ≥ 50%)"],
    "prior_platinum": ["progression after platinum-based chemotherapy", "at least one prior platinum-containing regimen"],
    "prior_egfr_tki": ["prior treatment with an EGFR tyrosine kinase inhibitor such as osimertinib", "progression on a third-generation EGFR TKI"],
    "prior_io": ["prior anti-PD-1 or anti-PD-L1 therapy", "previous immune checkpoint blockade"],
    "treatment_naive": ["no prior systemic therapy for advanced disease", "treatment-naive in the metastatic setting"],
    "prior_endocrine": ["progression on prior endocrine therapy", "previous treatment with an aromatase inhibitor"],
    "ex_brain_mets": ["Untreated or symptomatic brain metastases", "Active central nervous system metastases (treated, stable lesions are allowed)"],
    "ex_ild": ["History of interstitial lung disease or pneumonitis requiring corticosteroids"],
    "ex_heart_failure": ["Clinically significant heart failure (NYHA class III or IV)", "Symptomatic congestive heart failure"],
    "ex_hiv": ["Known uncontrolled HIV infection"],
    "ex_renal": ["Creatinine clearance below 45 mL/min", "Serum creatinine above 1.5 mg/dL"],
    "ex_hepatic": ["Total bilirubin above 1.5 times the upper limit of normal", "ALT or AST above 3 times the upper limit of normal"],
    "ecog_01": ["ECOG performance status of 0 or 1", "ECOG PS 0–1"],
    "ecog_02": ["ECOG performance status 0 to 2"],
    "hemoglobin_9": ["Hemoglobin of at least 9 g/dL", "Hemoglobin ≥ 9.0 g/dL without transfusion in the prior 2 weeks"],
    "platelets_100": ["Platelet count of at least 100 × 10^9/L"],
}

# (id_suffix, condition, phase, inclusion concepts, exclusion concepts, therapy description)
SPECS: list[tuple[str, str, str, list[str], list[str], str]] = [
    ("L01", "nsclc", "2", ["egfr_exon19del", "prior_egfr_tki", "ecog_01"], ["ex_brain_mets", "ex_ild"], "a hypothetical fourth-generation EGFR inhibitor"),
    ("L02", "nsclc", "3", ["egfr_exon19del", "egfr_l858r", "treatment_naive", "ecog_01"], ["ex_ild", "ex_heart_failure"], "a hypothetical EGFR inhibitor combined with chemotherapy"),
    ("L03", "nsclc", "2", ["egfr_t790m", "prior_egfr_tki", "ecog_01"], ["ex_brain_mets"], "a hypothetical T790M-selective agent"),
    ("L04", "nsclc", "1", ["kras_g12c", "prior_platinum", "prior_io", "ecog_01"], ["ex_brain_mets", "ex_hepatic"], "a hypothetical covalent KRAS G12C inhibitor"),
    ("L05", "nsclc", "2", ["kras_g12c", "treatment_naive", "pdl1_high"], ["ex_ild"], "a hypothetical KRAS G12C inhibitor with immunotherapy"),
    ("L06", "nsclc", "3", ["alk_fusion", "treatment_naive", "ecog_02"], ["ex_brain_mets", "ex_heart_failure"], "a hypothetical next-generation ALK inhibitor"),
    ("L07", "nsclc", "2", ["ros1_fusion", "ecog_01"], ["ex_renal"], "a hypothetical ROS1 inhibitor"),
    ("L08", "nsclc", "2", ["braf_v600e", "prior_platinum"], ["ex_hepatic"], "a hypothetical BRAF/MEK combination"),
    ("L09", "nsclc", "3", ["pdl1_high", "treatment_naive", "ecog_01"], ["ex_ild", "ex_hiv"], "a hypothetical anti-PD-1 antibody"),
    ("L10", "nsclc", "2", ["prior_platinum", "prior_io", "hemoglobin_9", "platelets_100"], ["ex_brain_mets", "ex_renal"], "a hypothetical antibody-drug conjugate"),
    ("L11", "sclc", "2", ["prior_platinum", "ecog_02"], ["ex_brain_mets"], "a hypothetical DLL3-directed agent"),
    ("L12", "sclc", "3", ["treatment_naive", "ecog_01"], ["ex_ild", "ex_hiv"], "a hypothetical checkpoint inhibitor with chemotherapy"),
    ("B01", "breast_cancer", "3", ["erbb2_amplification", "treatment_naive", "ecog_01"], ["ex_heart_failure"], "a hypothetical HER2-directed antibody"),
    ("B02", "breast_cancer", "2", ["erbb2_amplification", "prior_endocrine"], ["ex_brain_mets", "ex_heart_failure"], "a hypothetical HER2 tyrosine kinase inhibitor"),
    ("B03", "breast_cancer", "2", ["brca", "prior_platinum"], ["ex_renal"], "a hypothetical PARP inhibitor"),
    ("B04", "breast_cancer", "3", ["prior_endocrine", "ecog_01", "hemoglobin_9"], ["ex_hepatic"], "a hypothetical oral SERD"),
    ("C01", "colorectal_cancer", "3", ["msi_high", "treatment_naive"], ["ex_hiv", "ex_ild"], "a hypothetical anti-PD-1 antibody"),
    ("C02", "colorectal_cancer", "2", ["braf_v600e", "prior_platinum", "ecog_01"], ["ex_brain_mets"], "a hypothetical BRAF inhibitor with anti-EGFR antibody"),
    ("C03", "colorectal_cancer", "2", ["kras_g12c", "prior_platinum"], ["ex_hepatic"], "a hypothetical KRAS G12C inhibitor"),
    ("C04", "colorectal_cancer", "2", ["prior_platinum", "platelets_100", "hemoglobin_9"], ["ex_renal", "ex_heart_failure"], "a hypothetical oral multikinase inhibitor"),
    ("P01", "pancreatic_cancer", "2", ["brca", "prior_platinum"], ["ex_hepatic"], "a hypothetical PARP inhibitor maintenance"),
    ("P02", "pancreatic_cancer", "3", ["treatment_naive", "ecog_01"], ["ex_heart_failure", "ex_renal"], "a hypothetical stromal-targeting agent with chemotherapy"),
    ("M01", "melanoma", "3", ["braf_v600e", "treatment_naive"], ["ex_brain_mets"], "a hypothetical BRAF/MEK combination"),
    ("M02", "melanoma", "2", ["prior_io", "ecog_01"], ["ex_brain_mets", "ex_hiv"], "a hypothetical tumor-infiltrating lymphocyte therapy"),
    ("R01", "prostate_cancer", "3", ["brca", "ecog_01"], ["ex_heart_failure"], "a hypothetical PARP inhibitor with an androgen-receptor inhibitor"),
    ("R02", "prostate_cancer", "2", ["prior_endocrine", "platelets_100"], ["ex_brain_mets"], "a hypothetical radioligand therapy"),
    ("X01", "nsclc", "1", ["msi_high", "prior_io"], ["ex_hiv"], "a hypothetical tissue-agnostic agent"),
    ("X02", "breast_cancer", "1", ["pdl1_high", "prior_platinum"], ["ex_ild"], "a hypothetical bispecific antibody"),
]


def build_trials(out_dir: Path, seed: int = 11) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    docs = []
    for sfx, cond, phase, inc, exc, therapy in SPECS:
        doc_id = f"SYN-{sfx}"
        title = f"{doc_id}: Phase {phase} study of {therapy} in {rng.choice(PHRASES[cond])}"
        inc_lines = ["Age 18 years or older.", f"Diagnosis of {rng.choice(PHRASES[cond])}."]
        for c in inc:
            p = rng.choice(PHRASES[c])
            if c.startswith(("prior_", "treatment_")):
                inc_lines.append(f"Documented {p}.")
            elif c in ("ecog_01", "ecog_02", "hemoglobin_9", "platelets_100"):
                inc_lines.append(f"{p[0].upper() + p[1:]}.")
            else:
                inc_lines.append(f"Tumor harboring {p}, confirmed by a validated assay.")
        inc_lines.append("Measurable disease per RECIST v1.1.")
        exc_lines = [rng.choice(PHRASES[c]) + "." for c in exc]
        exc_lines.append("Pregnant or breastfeeding.")
        body = (f"# {title}\n\nSYNTHETIC DOCUMENT — generated for CLINIQ engineering demonstration; not a real clinical trial.\n\n"
                f"## Summary\nThis synthetic study evaluates {therapy} in patients with {rng.choice(PHRASES[cond])}.\n\n"
                f"## Inclusion Criteria\n" + "\n".join(f"- {line}" for line in inc_lines) +
                "\n\n## Exclusion Criteria\n" + "\n".join(f"- {line}" for line in exc_lines) + "\n")
        meta = {"doc_id": doc_id, "title": title, "doc_type": "trial_eligibility", "condition": cond,
                "concepts": sorted({cond, "age", *inc, *exc}), "source": "CLINIQ synthetic trial generator (deterministic, seed 11)",
                "license": "CC0 (synthetic, generated)"}
        (out_dir / f"{doc_id}.md").write_text(f"<!-- meta: {json.dumps(meta)} -->\n{body}")
        docs.append(meta)
    return docs


def load_corpus(corpus_dir: Path) -> list[dict[str, Any]]:
    docs = []
    for path in sorted(corpus_dir.rglob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        text = path.read_text()
        if not text.startswith("<!-- meta:"):
            continue
        header, body = text.split("-->\n", 1)
        meta = json.loads(header[len("<!-- meta:"):].strip())
        docs.append({**meta, "body": body, "path": str(path.relative_to(corpus_dir))})
    return docs


if __name__ == "__main__":
    import sys
    print(len(build_trials(Path(sys.argv[1]))), "synthetic trial documents written")
