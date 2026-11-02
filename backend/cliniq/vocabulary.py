"""Curated clinical vocabulary and allowlists.

This module is the single source of truth for what CLINIQ understands. The cohort
compiler only emits SQL for keys defined here, and the extractors can only produce keys
defined here — so a model can never smuggle an arbitrary table, column or code into SQL.

Code systems:
- ICD10CM, LOINC and ICDO3 codes are a small, hand-curated subset of the real systems.
- Codes in the 'CLINIQ' vocabulary are *local* codes (e.g. drugs, ECOG, PD-L1 TPS). A real
  deployment would map these to RxNorm / LOINC; we do not claim that mapping here.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ------------------------------------------------------------------ conditions
NSCLC_HISTOLOGY = {"8140/3": "Adenocarcinoma, NOS", "8070/3": "Squamous cell carcinoma, NOS",
                   "8012/3": "Large cell carcinoma, NOS", "8046/3": "Non-small cell carcinoma"}
SCLC_HISTOLOGY = {"8041/3": "Small cell carcinoma, NOS"}
OTHER_HISTOLOGY = {"8500/3": "Infiltrating duct carcinoma, NOS", "8720/3": "Malignant melanoma, NOS"}

ICD10_CODES: dict[str, str] = {
    "C34.10": "Malignant neoplasm of upper lobe, unspecified bronchus or lung",
    "C34.30": "Malignant neoplasm of lower lobe, unspecified bronchus or lung",
    "C34.90": "Malignant neoplasm of unspecified part of unspecified bronchus or lung",
    "C50.911": "Malignant neoplasm of unspecified site of right female breast",
    "C50.912": "Malignant neoplasm of unspecified site of left female breast",
    "C18.9": "Malignant neoplasm of colon, unspecified",
    "C20": "Malignant neoplasm of rectum",
    "C25.9": "Malignant neoplasm of pancreas, unspecified",
    "C43.9": "Malignant melanoma of skin, unspecified",
    "C61": "Malignant neoplasm of prostate",
    "C79.31": "Secondary malignant neoplasm of brain",
    "E11.9": "Type 2 diabetes mellitus without complications",
    "I10": "Essential (primary) hypertension",
    "I50.9": "Heart failure, unspecified",
    "N18.4": "Chronic kidney disease, stage 4 (severe)",
    "N18.9": "Chronic kidney disease, unspecified",
    "K74.60": "Unspecified cirrhosis of liver",
    "J84.9": "Interstitial pulmonary disease, unspecified",
    "B20": "Human immunodeficiency virus [HIV] disease",
    "C18.8": "Malignant neoplasm of overlapping sites of colon",
    "C50.919": "Malignant neoplasm of unspecified site of unspecified female breast",
    "N18.1": "Chronic kidney disease, stage 1",
    "N18.2": "Chronic kidney disease, stage 2 (mild)",
    "N18.30": "Chronic kidney disease, stage 3 unspecified",
    "N18.6": "End stage renal disease",
}

SNOMED_SYSTEM = "http://snomed.info/sct"
# Curated SNOMED CT -> ICD-10-CM crosswalk for cohort logic (code -> (ICD-10-CM, ICD-O-3 histology)).
# Built from concepts observed in Synthea output; only unambiguous mappings are included.
# Deliberately NOT mapped: suspected cancer, carcinoma in situ, unspecified "neoplasm of",
# prediabetes, diabetes-related kidney disorder of unspecified diabetes type.
# This is a cohort-grade crosswalk, not a billing-grade or official NLM map.
SNOMED_TO_ICD10: dict[str, tuple[str, str | None]] = {
    "59621000": ("I10", None),            # Essential hypertension
    "44054006": ("E11.9", None),          # Diabetes mellitus type 2
    "90781000119102": ("E11.9", None),    # Microalbuminuria due to type 2 diabetes mellitus
    "157141000119108": ("E11.9", None),   # Proteinuria due to type 2 diabetes mellitus
    "368581000119106": ("E11.9", None),   # Neuropathy due to type 2 diabetes mellitus
    "1551000119108": ("E11.9", None),     # Nonproliferative diabetic retinopathy due to type 2 DM
    "431855005": ("N18.1", None),         # CKD stage 1
    "431856006": ("N18.2", None),         # CKD stage 2
    "433144002": ("N18.30", None),        # CKD stage 3
    "431857002": ("N18.4", None),         # CKD stage 4
    "46177005": ("N18.6", None),          # End-stage renal disease
    "88805009": ("I50.9", None),          # Chronic congestive heart failure
    "93761005": ("C18.9", None),          # Primary malignant neoplasm of colon
    "109838007": ("C18.8", None),         # Overlapping malignant neoplasm of colon
    "254837009": ("C50.919", None),       # Malignant neoplasm of breast
    "254637007": ("C34.90", "8046/3"),    # Non-small cell lung cancer
    "254632001": ("C34.90", "8041/3"),    # Small cell carcinoma of lung
}


@dataclass(frozen=True)
class ConditionSet:
    key: str
    label: str
    icd10_prefixes: tuple[str, ...]
    # If set, the set additionally requires histology in `histology_in`; a documented
    # histology outside it is negative evidence; missing histology is unknown.
    histology_in: tuple[str, ...] = ()


CONDITION_SETS: dict[str, ConditionSet] = {c.key: c for c in [
    ConditionSet("nsclc", "Non-small-cell lung cancer", ("C34",), tuple(NSCLC_HISTOLOGY)),
    ConditionSet("sclc", "Small-cell lung cancer", ("C34",), tuple(SCLC_HISTOLOGY)),
    ConditionSet("lung_cancer", "Lung cancer (any histology)", ("C34",)),
    ConditionSet("breast_cancer", "Breast cancer", ("C50",)),
    ConditionSet("colorectal_cancer", "Colorectal cancer", ("C18", "C19", "C20")),
    ConditionSet("pancreatic_cancer", "Pancreatic cancer", ("C25",)),
    ConditionSet("melanoma", "Cutaneous melanoma", ("C43",)),
    ConditionSet("prostate_cancer", "Prostate cancer", ("C61",)),
    ConditionSet("brain_metastases", "Brain metastases", ("C79.31",)),
    ConditionSet("type2_diabetes", "Type 2 diabetes", ("E11",)),
    ConditionSet("hypertension", "Hypertension", ("I10",)),
    ConditionSet("heart_failure", "Heart failure", ("I50",)),
    ConditionSet("ckd", "Chronic kidney disease", ("N18",)),
    ConditionSet("cirrhosis", "Cirrhosis of liver", ("K74.6",)),
    ConditionSet("interstitial_lung_disease", "Interstitial lung disease", ("J84",)),
    ConditionSet("hiv", "HIV disease", ("B20",)),
]}

# ------------------------------------------------------------------ biomarkers


@dataclass(frozen=True)
class Biomarker:
    key: str
    gene: str
    label: str
    hgvs_p: str | None


BIOMARKERS: dict[str, Biomarker] = {b.key: b for b in [
    Biomarker("egfr_exon19del", "EGFR", "EGFR exon 19 deletion", "p.E746_A750del"),
    Biomarker("egfr_l858r", "EGFR", "EGFR L858R", "p.L858R"),
    Biomarker("egfr_t790m", "EGFR", "EGFR T790M", "p.T790M"),
    Biomarker("kras_g12c", "KRAS", "KRAS G12C", "p.G12C"),
    Biomarker("alk_fusion", "ALK", "ALK fusion", None),
    Biomarker("ros1_fusion", "ROS1", "ROS1 fusion", None),
    Biomarker("braf_v600e", "BRAF", "BRAF V600E", "p.V600E"),
    Biomarker("erbb2_amplification", "ERBB2", "HER2 (ERBB2) amplification", None),
    Biomarker("brca1_pathogenic", "BRCA1", "BRCA1 pathogenic variant", None),
    Biomarker("brca2_pathogenic", "BRCA2", "BRCA2 pathogenic variant", None),
    Biomarker("msi_high", "MSI", "Microsatellite instability-high", None),
]}

GENE_PANELS: dict[str, list[str]] = {}
for _b in BIOMARKERS.values():
    GENE_PANELS.setdefault(_b.gene, []).append(_b.key)

# ------------------------------------------------------------------ labs


@dataclass(frozen=True)
class Lab:
    key: str
    label: str
    vocabulary: str
    code: str
    unit: str
    plausible: tuple[float, float]
    conversions: dict[str, float] = field(default_factory=dict)  # source unit -> multiplier to canonical
    alt_codes: tuple[str, ...] = ()  # equivalent codes in the same vocabulary (e.g. blood vs serum specimen)


LABS: dict[str, Lab] = {lab.key: lab for lab in [
    # 38483-4 = Creatinine [Mass/volume] in Blood (used by Synthea); treated as equivalent to serum/plasma 2160-0
    Lab("creatinine", "Serum creatinine", "LOINC", "2160-0", "mg/dL", (0.1, 20), {"umol/L": 1 / 88.42, "µmol/L": 1 / 88.42}, ("38483-4",)),
    Lab("hemoglobin", "Hemoglobin", "LOINC", "718-7", "g/dL", (2, 25), {"g/L": 0.1}),
    Lab("hba1c", "Hemoglobin A1c", "LOINC", "4548-4", "%", (3, 20)),
    Lab("alt", "Alanine aminotransferase (ALT)", "LOINC", "1742-6", "U/L", (1, 5000)),
    Lab("ast", "Aspartate aminotransferase (AST)", "LOINC", "1920-8", "U/L", (1, 5000)),
    Lab("bilirubin", "Total bilirubin", "LOINC", "1975-2", "mg/dL", (0.05, 40), {"umol/L": 1 / 17.1, "µmol/L": 1 / 17.1}),
    Lab("platelets", "Platelet count", "LOINC", "777-3", "10*3/uL", (1, 2000), {"10*9/L": 1.0}),
    Lab("wbc", "White blood cell count", "LOINC", "6690-2", "10*3/uL", (0.1, 300), {"10*9/L": 1.0}),
    Lab("egfr_renal", "Estimated GFR", "LOINC", "33914-3", "mL/min/1.73m2", (1, 200), {"mL/min/{1.73_m2}": 1.0}),
    Lab("pdl1_tps", "PD-L1 tumor proportion score", "CLINIQ", "PDL1_TPS", "%", (0, 100)),
    Lab("ecog", "ECOG performance status", "CLINIQ", "ECOG", "{score}", (0, 5)),
]}
LAB_BY_CODE = {(lab.vocabulary, code): lab for lab in LABS.values() for code in (lab.code, *lab.alt_codes)}

# ------------------------------------------------------------------ drugs & procedures
DRUG_CLASSES: dict[str, tuple[str, list[str]]] = {
    "egfr_tki": ("EGFR tyrosine kinase inhibitor", ["osimertinib", "erlotinib", "gefitinib", "afatinib"]),
    "alk_inhibitor": ("ALK inhibitor", ["alectinib", "crizotinib", "lorlatinib"]),
    "kras_g12c_inhibitor": ("KRAS G12C inhibitor", ["sotorasib", "adagrasib"]),
    "braf_inhibitor": ("BRAF inhibitor", ["dabrafenib", "vemurafenib"]),
    "platinum_chemotherapy": ("Platinum-based chemotherapy", ["carboplatin", "cisplatin", "oxaliplatin"]),
    "taxane": ("Taxane chemotherapy", ["paclitaxel", "docetaxel"]),
    "antimetabolite": ("Antimetabolite chemotherapy", ["pemetrexed", "gemcitabine", "fluorouracil", "capecitabine"]),
    "immunotherapy": ("Immune checkpoint inhibitor", ["pembrolizumab", "nivolumab", "atezolizumab", "durvalumab"]),
    "her2_targeted": ("HER2-targeted therapy", ["trastuzumab", "pertuzumab"]),
    "endocrine_therapy": ("Endocrine therapy", ["tamoxifen", "letrozole", "anastrozole"]),
    "parp_inhibitor": ("PARP inhibitor", ["olaparib", "niraparib"]),
    "antiandrogen": ("Androgen-receptor inhibitor", ["enzalutamide", "abiraterone"]),
    "antidiabetic": ("Antidiabetic agent", ["metformin", "insulin glargine"]),
    "anticoagulant": ("Anticoagulant", ["apixaban", "warfarin"]),
    "antihypertensive": ("Antihypertensive", ["lisinopril", "amlodipine"]),
}
ANTICANCER_CLASSES = ("egfr_tki", "alk_inhibitor", "kras_g12c_inhibitor", "braf_inhibitor", "platinum_chemotherapy",
                      "taxane", "antimetabolite", "immunotherapy", "her2_targeted", "endocrine_therapy",
                      "parp_inhibitor", "antiandrogen")
# Virtual class: any systemic anticancer therapy.
VIRTUAL_DRUG_CLASSES = {
    "any_systemic_therapy": ("Any systemic anticancer therapy", ANTICANCER_CLASSES),
    "any_chemotherapy": ("Cytotoxic chemotherapy", ("platinum_chemotherapy", "taxane", "antimetabolite")),
}
DRUG_TO_CLASS = {d: c for c, (_, ds) in DRUG_CLASSES.items() for d in ds}

PROCEDURE_CLASSES: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "radiation_therapy": ("Radiation therapy", [("RT-THORAX", "Thoracic radiotherapy"), ("RT-SRS", "Stereotactic radiosurgery")]),
    "surgical_resection": ("Surgical resection", [("SURG-LOBECTOMY", "Lobectomy"), ("SURG-MASTECTOMY", "Mastectomy"),
                                                   ("SURG-COLECTOMY", "Colectomy")]),
}
PROCEDURE_TO_CLASS = {code: c for c, (_, items) in PROCEDURE_CLASSES.items() for code, _ in items}

US_STATES = ["AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA",
             "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
             "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC"]
STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}

FHIR_SYSTEMS = {
    "http://hl7.org/fhir/sid/icd-10-cm": "ICD10CM",
    "http://loinc.org": "LOINC",
    "urn:oid:2.16.840.1.113883.6.43.1": "ICDO3",  # ICD-O-3 (as used in FHIR examples)
    "https://cliniq.local/fhir/CodeSystem/local": "CLINIQ",
    "https://cliniq.local/fhir/CodeSystem/drug": "CLINIQ",
    "https://cliniq.local/fhir/CodeSystem/procedure": "CLINIQ",
    "https://cliniq.local/fhir/CodeSystem/biomarker": "CLINIQ",
}
SYSTEM_URI = {v: k for k, v in FHIR_SYSTEMS.items() if not k.endswith(("drug", "procedure", "biomarker"))}
DRUG_SYSTEM = "https://cliniq.local/fhir/CodeSystem/drug"
PROCEDURE_SYSTEM = "https://cliniq.local/fhir/CodeSystem/procedure"
BIOMARKER_SYSTEM = "https://cliniq.local/fhir/CodeSystem/biomarker"


def concept_rows() -> list[tuple[str, str, str, str, str | None]]:
    """Rows for clinical.concept (vocabulary, code, name, domain, class)."""
    rows: list[tuple[str, str, str, str, str | None]] = []
    rows += [("ICD10CM", c, n, "Condition", None) for c, n in ICD10_CODES.items()]
    rows += [("ICDO3", c, n, "Histology", None) for c, n in {**NSCLC_HISTOLOGY, **SCLC_HISTOLOGY, **OTHER_HISTOLOGY}.items()]
    rows += [(lab.vocabulary, lab.code, lab.label, "Measurement", lab.key) for lab in LABS.values()]
    rows += [("CLINIQ", f"RX-{d.upper().replace(' ', '_')}", d, "Drug", c) for d, c in DRUG_TO_CLASS.items()]
    rows += [("CLINIQ", code, name, "Procedure", cls) for cls, (_, items) in PROCEDURE_CLASSES.items() for code, name in items]
    rows += [("CLINIQ", f"BM-{b.key.upper()}", b.label, "Gene", b.gene) for b in BIOMARKERS.values()]
    return rows


def drug_code(name: str) -> str:
    return f"RX-{name.upper().replace(' ', '_')}"
