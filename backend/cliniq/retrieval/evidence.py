"""Evidence engine: retrieve, verify and cite supporting passages; flag insufficient evidence.

Output separates three kinds of content:
  retrieved  verbatim passages with source document, section and character offsets
  inferred   short system-generated statements, each pointing at the citations it relies on
  flags      'insufficient_evidence' when no retrieved passage passes the support check

The composer is extractive and deterministic. A passage only becomes a citation if it
passes an anchor check (the criterion's concept must literally appear in the passage), so
every inferred statement is traceable to text the user can read.
"""
from __future__ import annotations

import re
import time
import uuid
from typing import Any

from .. import vocabulary as V
from ..cohort.models import CohortDefinition, describe
from ..db import connect
from .index import HybridIndex, get_index, tokenize

EVIDENCE_VERSION = "evidence-1.0.0"

ANCHORS: dict[str, str] = {
    "nsclc": r"non[- ]?small[- ]?cell|nsclc", "sclc": r"small[- ]cell lung|(?<!non-)small cell lung", "lung_cancer": r"lung",
    "breast_cancer": r"breast", "colorectal_cancer": r"colorectal|colon|rectum", "pancreatic_cancer": r"pancrea",
    "melanoma": r"melanoma", "prostate_cancer": r"prostat", "brain_metastases": r"brain metasta|central nervous system metasta|cns",
    "heart_failure": r"heart failure", "interstitial_lung_disease": r"interstitial lung|pneumonitis", "hiv": r"\bhiv\b",
    "ckd": r"renal|kidney|creatinine", "cirrhosis": r"cirrhosis|hepatic", "hypertension": r"hypertension", "type2_diabetes": r"diabet",
    "egfr_exon19del": r"exon 19|ex19del", "egfr_l858r": r"l858r", "egfr_t790m": r"t790m", "kras_g12c": r"g12c|codon 12 of kras",
    "alk_fusion": r"\balk\b", "ros1_fusion": r"ros1", "braf_v600e": r"v600", "erbb2_amplification": r"her2|erbb2",
    "brca1_pathogenic": r"brca", "brca2_pathogenic": r"brca", "msi_high": r"msi|mismatch repair|microsatellite",
    "creatinine": r"creatinine|renal", "egfr_renal": r"gfr|creatinine clearance", "hemoglobin": r"hemoglobin", "platelets": r"platelet",
    "bilirubin": r"bilirubin", "alt": r"\balt\b", "ast": r"\bast\b", "pdl1_tps": r"pd-l1", "ecog": r"ecog", "hba1c": r"a1c", "wbc": r"white blood|wbc",
    "egfr_tki": r"egfr (?:tyrosine kinase|tki)|osimertinib|erlotinib|gefitinib", "platinum_chemotherapy": r"platinum",
    "immunotherapy": r"pd-1|pd-l1 therapy|checkpoint|immunotherapy", "endocrine_therapy": r"endocrine|aromatase",
    "any_systemic_therapy": r"prior (?:systemic )?(?:therapy|treatment)|treatment-naive|prior .*regimen|progression (?:on|after)|lines? of therapy",
    "any_chemotherapy": r"chemotherapy", "alk_inhibitor": r"alk inhibitor", "parp_inhibitor": r"parp", "her2_targeted": r"her2-directed|trastuzumab",
    "taxane": r"taxane|paclitaxel|docetaxel", "antimetabolite": r"pemetrexed|gemcitabine|fluorouracil", "kras_g12c_inhibitor": r"g12c inhibitor",
    "braf_inhibitor": r"braf inhibitor|braf/mek", "antiandrogen": r"androgen", "radiation_therapy": r"radiation|radiotherapy|radiosurgery",
    "surgical_resection": r"resection|surgery", "age": r"age \d+|years or older", "sex": r"\b(?:women|men|female|male)\b",
    "missing_data": r"missing|unknown|absence",
}


def _concepts(c: Any) -> list[str]:
    t = c.type
    if t == "diagnosis":
        return [c.condition]
    if t == "biomarker":
        return list(c.markers)
    if t == "lab":
        return [c.lab]
    if t == "treatment":
        return [c.drug_class] if c.drug_class else [V.DRUG_TO_CLASS[c.drug]]
    if t == "procedure":
        return [c.procedure_class]
    return [t]


def _query_for(c: Any) -> str:
    extra = {"biomarker": "tumor harboring mutation", "lab": "laboratory requirement", "treatment": "prior therapy",
             "diagnosis": "diagnosis of", "procedure": "prior procedure"}.get(c.type, "")
    return f"{describe(c)} {extra}".strip()


def anchored(passage: str, concepts: list[str]) -> list[str]:
    hits = []
    for k in concepts:
        pat = ANCHORS.get(k)
        if pat and re.search(pat, passage, re.I):
            hits.append(k)
    return hits


def coverage(query: str, passage: str) -> float:
    q = {t for t in tokenize(query) if len(t) > 2}
    if not q:
        return 0.0
    return len(q & set(tokenize(passage))) / len(q)


def search(query: str, k: int = 5, mode: str = "hybrid", user: str | None = None, log: bool = True,
           index: HybridIndex | None = None) -> dict[str, Any]:
    """Free-text evidence search. Flags insufficient evidence when the best passage covers less
    than half of the query's content terms *and* its embedding similarity is low."""
    t0 = time.perf_counter()
    idx = index or get_index()
    if idx is None:
        return {"query": query, "results": [], "insufficient_evidence": True, "reason": "evidence index is empty", "mode": mode}
    hits = idx.search(query, k=k, mode=mode)
    results = [h.as_dict() | {"term_coverage": round(coverage(query, h.chunk.text + " " + h.chunk.title), 3)} for h in hits]
    best_cov = max((r["term_coverage"] for r in results), default=0.0)
    best_dense = max((r["dense"] for r in results), default=0.0)
    insufficient = not results or (best_cov < 0.5 and best_dense < 0.45)
    out = {"query": query, "mode": mode, "results": results, "insufficient_evidence": insufficient,
           "reason": "no passage covers the query's key terms" if insufficient else None,
           "index_version": f"{idx.embedder.name}|{idx.corpus_sha}", "latency_ms": round((time.perf_counter() - t0) * 1000, 2)}
    if log:
        _log(query, mode, k, out, user)
    return out


def evidence_for_definition(defn: CohortDefinition, k: int = 5, user: str | None = None, index: HybridIndex | None = None) -> dict[str, Any]:
    t0 = time.perf_counter()
    idx = index or get_index()
    items = []
    for role, crits in (("inclusion", defn.inclusion), ("exclusion", defn.exclusion)):
        for n, c in enumerate(crits, start=1):
            cid = f"{role[0]}{n}"
            concepts = _concepts(c)
            q = _query_for(c)
            hits = idx.search(q, k=k) if idx else []
            citations = []
            for h in hits:
                matched = anchored(h.chunk.text, concepts)
                if matched:
                    citations.append(h.as_dict() | {"anchors": matched})
            trials = sorted({x["doc_id"] for x in citations if x["doc_type"] == "trial_eligibility"})
            refs = sorted({x["doc_id"] for x in citations if x["doc_type"] == "reference_note"})
            statements = []
            if trials:
                statements.append({"kind": "inferred", "text": f"{len(trials)} retrieved trial document(s) state a comparable criterion.",
                                   "citations": [x["chunk_id"] for x in citations if x["doc_type"] == "trial_eligibility"]})
            if refs:
                statements.append({"kind": "inferred", "text": f"{len(refs)} reference note(s) provide background on this concept.",
                                   "citations": [x["chunk_id"] for x in citations if x["doc_type"] == "reference_note"]})
            items.append({"cid": cid, "role": role, "label": describe(c), "query": q, "concepts": concepts,
                          "status": "supported" if citations else "insufficient_evidence", "citations": citations,
                          "statements": statements, "candidates_considered": len(hits)})
    out = {"version": EVIDENCE_VERSION, "items": items, "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
           "insufficient_count": sum(i["status"] == "insufficient_evidence" for i in items)}
    if user is not None:
        _log(f"[definition] {defn.name}", "definition", k, {"results": [c for i in items for c in i["citations"]],
                                                              "insufficient_evidence": out["insufficient_count"] == len(items),
                                                              "latency_ms": out["latency_ms"]}, user)
    return out


def _log(query: str, mode: str, k: int, out: dict[str, Any], user: str | None) -> None:
    try:
        with connect() as c:
            c.execute("INSERT INTO rag.queries (rag_query_id, query, mode, top_k, n_results, max_score, insufficient, duration_ms, user_id)"
                      " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                      (str(uuid.uuid4()), query[:1000], mode, k, len(out["results"]),
                       max((r.get("score", 0) for r in out["results"]), default=None), out["insufficient_evidence"], int(out["latency_ms"]), user))
    except Exception:  # noqa: BLE001 - telemetry must never break retrieval
        pass
