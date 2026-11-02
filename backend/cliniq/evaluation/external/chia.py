"""Chia (Kury et al., Scientific Data 2020) → CLINIQ concept-level evaluation.

Chia annotates eligibility-criteria text from 1,000 Phase IV trials with typed entities and
relations. It does not use CLINIQ's schema, so we evaluate at the level both share:
*which in-vocabulary concepts a criterion mentions, and whether each is an inclusion or an
exclusion*.

Gold construction (independent of the extractors' lexicon):
  - each criterion line of a Chia document is one evaluation unit
  - Chia entities of type Condition / Drug / Measurement / Procedure / Person / Observation are
    mapped to CLINIQ concept keys with the alias table below (written separately from
    extraction/rules.py; it maps annotated spans, it does not parse free text)
  - polarity: the section (inclusion/exclusion), flipped when the entity takes part in a
    Has_negation relation
  - biomarkers are compared at gene level (EGFR, ALK, …), labs by lab key, treatments by drug
    or class

Prediction: the extractor runs on the same line; concepts are normalized identically and
polarity is flipped for exclusion sections. Reported: concept P/R/F1, polarity accuracy on
matched concepts, and DSL *coverage* (share of annotated clinical entities that map to any
CLINIQ concept) — the latter quantifies how much of real-world eligibility text CLINIQ's
vocabulary can represent at all.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from ... import vocabulary as V

CLINICAL_TYPES = {"Condition", "Drug", "Measurement", "Procedure", "Observation", "Person", "Device"}

# concept → regexes matched against the *annotated entity text* (lowercased)
ALIASES: dict[tuple[str, str], list[str]] = {
    ("diagnosis", "nsclc"): [r"non[- ]?small[- ]?cell lung", r"\bnsclc\b"],
    ("diagnosis", "sclc"): [r"(?<!non-)(?<!non )\bsmall[- ]cell lung", r"\bsclc\b"],
    ("diagnosis", "lung_cancer"): [r"lung (?:cancer|carcinoma|malignan|neoplasm|tumou?r)"],
    ("diagnosis", "breast_cancer"): [r"breast (?:cancer|carcinoma|malignan|neoplasm|tumou?r)"],
    ("diagnosis", "colorectal_cancer"): [r"colorectal (?:cancer|carcinoma)", r"(?:colon|rectal) (?:cancer|carcinoma)"],
    ("diagnosis", "pancreatic_cancer"): [r"pancrea\w* (?:cancer|carcinoma|adenocarcinoma)"],
    ("diagnosis", "melanoma"): [r"melanoma"],
    ("diagnosis", "prostate_cancer"): [r"prostat\w* (?:cancer|carcinoma|adenocarcinoma)"],
    ("diagnosis", "brain_metastases"): [r"(?:brain|cns|central nervous system|intracranial|cerebral) metasta"],
    ("diagnosis", "type2_diabetes"): [r"type (?:2|ii) diabet", r"\bt2dm\b", r"non[- ]insulin[- ]dependent diabet", r"^diabetes(?: mellitus)?$"],
    ("diagnosis", "hypertension"): [r"hypertension", r"high blood pressure"],
    ("diagnosis", "heart_failure"): [r"heart failure", r"\bchf\b", r"cardiac failure"],
    ("diagnosis", "ckd"): [r"chronic kidney disease", r"\bckd\b", r"chronic renal (?:failure|insufficiency|disease)", r"renal insufficiency"],
    ("diagnosis", "cirrhosis"): [r"cirrhosis"],
    ("diagnosis", "interstitial_lung_disease"): [r"interstitial lung", r"\bild\b", r"pulmonary fibrosis"],
    ("diagnosis", "hiv"): [r"\bhiv\b", r"human immunodeficiency virus", r"\baids\b"],
    ("lab", "creatinine"): [r"creatinine(?! clearance)"],
    ("lab", "egfr_renal"): [r"\begfr\b", r"glomerular filtration", r"\bgfr\b"],
    ("lab", "hemoglobin"): [r"^h(?:a)?emoglobin$", r"^h(?:a)?emoglobin (?:level|concentration)", r"^hgb$", r"^hb$"],
    ("lab", "hba1c"): [r"hba1c", r"\ba1c\b", r"glycated h(?:a)?emoglobin", r"glycosylated h(?:a)?emoglobin"],
    ("lab", "alt"): [r"\balt\b", r"alanine aminotransferase", r"\bsgpt\b"],
    ("lab", "ast"): [r"\bast\b", r"aspartate aminotransferase", r"\bsgot\b"],
    ("lab", "bilirubin"): [r"bilirubin"],
    ("lab", "platelets"): [r"platelet"],
    ("lab", "wbc"): [r"white blood cell", r"\bwbc\b", r"leukocyte count"],
    ("lab", "pdl1_tps"): [r"pd-?l1"],
    ("lab", "ecog"): [r"\becog\b", r"performance status"],
    ("procedure", "radiation_therapy"): [r"radiation", r"radiotherapy", r"radiosurgery"],
    ("procedure", "surgical_resection"): [r"resection", r"lobectomy", r"mastectomy", r"colectomy", r"prostatectomy", r"pneumonectomy"],
    ("treatment", "any_chemotherapy"): [r"^(?:cytotoxic |systemic )?chemotherap"],
    ("treatment", "immunotherapy"): [r"immunotherap", r"checkpoint inhibitor", r"anti-?pd-?(?:l)?1", r"pd-1 inhibitor"],
    ("treatment", "platinum_chemotherapy"): [r"platinum"],
    ("treatment", "egfr_tki"): [r"egfr[- ]?(?:tki|tyrosine kinase)"],
    ("treatment", "endocrine_therapy"): [r"endocrine therap", r"hormon\w* therap", r"aromatase inhibitor"],
    ("treatment", "anticoagulant"): [r"anticoagula"],
    ("treatment", "any_systemic_therapy"): [r"^(?:prior |previous )?(?:systemic |anticancer |anti-cancer )?(?:therapy|treatment)$"],
    ("sex", "sex"): [r"^(?:women|woman|female|females|men|man|male|males|postmenopausal women)$"],
    ("age", "age"): [r"^age$", r"^aged?\b", r"years? (?:of age|old)", r"^adults?$", r"^elderly$", r"^older adults?$"],
}
for _drug in V.DRUG_TO_CLASS:
    ALIASES[("treatment", _drug)] = [rf"\b{re.escape(_drug)}\b"]
for _bm in V.BIOMARKERS.values():  # gene-level biomarker concepts
    gene = {"ERBB2": r"her-?2|erbb2", "MSI": r"\bmsi\b|microsatellite|mismatch repair|\bdmmr\b"}.get(_bm.gene, rf"\b{_bm.gene.lower()}\b")
    ALIASES[("biomarker", _bm.gene)] = [gene]
_COMPILED = [(k, [re.compile(p) for p in pats]) for k, pats in ALIASES.items()]


def map_entity(etype: str, text: str) -> tuple[str, str] | None:
    t = re.sub(r"\s+", " ", text.lower()).strip(" .,;:()")
    if etype not in CLINICAL_TYPES:
        return None
    if etype == "Person" and not re.search(r"age|year|old|adult|elderly|wom[ae]n|\bm[ae]n\b|male|female", t):
        return None
    hits = [k for k, pats in _COMPILED if any(p.search(t) for p in pats)]
    if not hits:
        return None
    # specific beats generic (nsclc over lung_cancer, drug over class)
    order = {"nsclc": 0, "sclc": 0, "lung_cancer": 1, "any_systemic_therapy": 2}
    hits.sort(key=lambda k: (order.get(k[1], 0.5), -len(k[1])))
    return hits[0]


def prediction_concepts(defn: dict[str, Any], exclusion_section: bool) -> dict[tuple[str, str], set[str]]:
    out: dict[tuple[str, str], set[str]] = {}
    for role in ("inclusion", "exclusion"):
        for c in defn.get(role, []):
            t = c["type"]
            if t == "biomarker":
                keys = {("biomarker", V.BIOMARKERS[m].gene) for m in c["markers"]}
            elif t == "diagnosis":
                keys = {("diagnosis", c["condition"])}
            elif t == "lab":
                keys = {("lab", c["lab"])}
            elif t == "treatment":
                keys = {("treatment", c.get("drug") or c.get("drug_class"))}
            elif t == "procedure":
                keys = {("procedure", c["procedure_class"])}
            elif t in ("age", "sex"):
                keys = {(t, t)}
            else:
                keys = {(t, t)}
            pol = role if not exclusion_section else ("exclusion" if role == "inclusion" else "inclusion")
            for k in keys:
                out.setdefault(k, set()).add(pol)
    return out


def units_from_record(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Split one normalized Chia record into criterion lines with gold concepts."""
    text = rec["text"]
    negated: set[str] = set()
    ents = {e["id"]: e for e in rec["entities"]}
    for r in rec["relations"]:
        if r["type"].lower() == "has_negation":
            for a in (r["arg1"], r["arg2"]):
                if ents.get(a, {}).get("type") != "Negation":
                    negated.add(a)
    excl = rec["section"] == "exclusion"
    units = []
    pos = 0
    for line in text.split("\n"):
        start, end = pos, pos + len(line)
        pos = end + 1
        if not line.strip():
            continue
        gold: dict[tuple[str, str], set[str]] = {}
        clinical = mapped = 0
        unmapped: list[str] = []
        for e in rec["entities"]:
            if not e["spans"] or not (start <= e["spans"][0][0] < end):
                continue
            if e["type"] in CLINICAL_TYPES - {"Person"}:
                clinical += 1
            k = map_entity(e["type"], e["text"])
            if k is None:
                if e["type"] == "Condition":
                    unmapped.append(e["text"].lower()[:60])
                continue
            if e["type"] != "Person":
                mapped += 1
            pol = "exclusion" if excl else "inclusion"
            if e["id"] in negated:
                pol = "inclusion" if pol == "exclusion" else "exclusion"
            gold.setdefault(k, set()).add(pol)
        units.append({"unit_id": f"{rec['doc_id']}#{start}", "trial_id": rec["trial_id"], "section": rec["section"], "text": line.strip(),
                      "gold": gold, "clinical_entities": clinical, "mapped_entities": mapped, "unmapped_conditions": unmapped})
    return units


def load_units(path: Path) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            units += units_from_record(json.loads(line))
    return units


def eval_chia(extractor: Any, path: Path, limit: int | None = None, only_in_vocab: bool = True) -> dict[str, Any]:
    units = load_units(path)
    all_units = len(units)
    clinical = sum(u["clinical_entities"] for u in units)
    mapped = sum(u["mapped_entities"] for u in units)
    top_unmapped = Counter(c for u in units for c in u["unmapped_conditions"]).most_common(25)
    pool = [u for u in units if u["gold"]] if only_in_vocab else units
    pool.sort(key=lambda u: u["unit_id"])
    if limit:
        pool = pool[:limit]
    tp = fp = fn = pol_ok = pol_n = invalid = 0
    lat: list[float] = []
    items = []
    by_type: Counter = Counter()
    for u in pool:
        r = extractor.extract(u["text"])
        lat.append(r.latency_ms)
        if not r.valid:
            invalid += 1
        pred = prediction_concepts(r.definition or {}, u["section"] == "exclusion")
        g, p = set(u["gold"]), set(pred)
        tp += len(g & p)
        fp += len(p - g)
        fn += len(g - p)
        for k in g & p:
            pol_n += 1
            pol_ok += bool(u["gold"][k] & pred[k])
            by_type[(k[0], "tp")] += 1
        for k in g - p:
            by_type[(k[0], "fn")] += 1
        for k in p - g:
            by_type[(k[0], "fp")] += 1
        cats = (["missed_concept"] if g - p else []) + (["spurious_concept"] if p - g else []) + \
               (["wrong_polarity"] if any(not (u["gold"][k] & pred[k]) for k in g & p) else [])
        items.append({"item_id": u["unit_id"], "passed": not cats, "failure_category": cats[0] if cats else None,
                      "categories": cats, "details": {"text": u["text"], "section": u["section"],
                                                      "gold": {f"{k[0]}:{k[1]}": sorted(v) for k, v in u["gold"].items()},
                                                      "predicted": {f"{k[0]}:{k[1]}": sorted(v) for k, v in pred.items()},
                                                      "subset": "chia"}})
        if hasattr(extractor, "model"):  # be gentle with rate-limited APIs
            time.sleep(0)
    P = tp / (tp + fp) if tp + fp else 1.0
    R = tp / (tp + fn) if tp + fn else 1.0
    per_type = {}
    for t in sorted({k[0] for k in by_type}):
        a, b, c = by_type[(t, "tp")], by_type[(t, "fp")], by_type[(t, "fn")]
        pp, rr = (a / (a + b) if a + b else 1.0), (a / (a + c) if a + c else 1.0)
        per_type[t] = {"precision": round(pp, 4), "recall": round(rr, 4), "f1": round(2 * pp * rr / (pp + rr), 4) if pp + rr else 0.0, "support": a + c}
    lat_sorted = sorted(lat)
    metrics = {
        "n_units_total": all_units, "n_units_evaluated": len(pool), "selection": "lines with ≥1 in-vocabulary gold concept" if only_in_vocab else "all lines",
        "concept": {"precision": round(P, 4), "recall": round(R, 4), "f1": round(2 * P * R / (P + R), 4) if P + R else 0.0},
        "polarity_accuracy": round(pol_ok / pol_n, 4) if pol_n else None, "invalid_outputs": invalid,
        "per_concept_type": per_type,
        "vocabulary_coverage": {"clinical_entities": clinical, "mapped_to_cliniq": mapped, "share": round(mapped / clinical, 4) if clinical else None},
        "top_unmapped_conditions": top_unmapped,
        "latency_ms": {"p50": lat_sorted[len(lat_sorted) // 2] if lat_sorted else None, "p95": lat_sorted[int(0.95 * (len(lat_sorted) - 1))] if lat_sorted else None},
        "failure_categories": dict(Counter(c for x in items for c in x["categories"])),
    }
    return {"suite": "chia", "system_version": extractor.version, "metrics": metrics, "items": items, "benchmark": path}
