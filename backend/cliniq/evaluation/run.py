"""Benchmark runner: extraction, retrieval and evidence suites.

Every number in a report is computed here from an actual run. Runs are recorded in
ops.eval_runs (+ per-item results) together with: system version, prompt hash (LLM),
benchmark file hash, data version and code version, so any two runs can be compared.

Metric definitions (see docs/evaluation_report.md for discussion):
  structured_validity      share of outputs that validate as CohortDefinition
  concept_{p,r,f1}         micro P/R/F1 over (type, key) concepts, role ignored
  criterion_{p,r,f1}       micro P/R/F1 over exact (role, criterion) pairs incl. values
  exact_match              share of items whose full criterion set equals gold
  polarity_accuracy        among concepts found in both, share with the correct role
  hallucination_rate       predicted criteria whose concept has no lexical anchor in the request
  sql_success_rate         valid outputs that compile, pass the SQL guard and execute
  cohort_{precision,recall} vs. the gold definition's *eligible* set, executed on the same data
                           (items where both sets are empty count as P=R=1 and are reported)
  retrieval recall@k, mrr  doc-level, over answerable queries
  abstention_*             insufficient-evidence flag vs. unanswerable queries
  citation_precision       evidence citations whose document is tagged with the criterion's concept
  unsupported_claim_rate   inferred statements whose cited passages fail the anchor check
"""
from __future__ import annotations

import hashlib
import json
import statistics
import subprocess
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from ..cohort.compiler import classify, compile_definition
from ..cohort.engine import data_version
from ..cohort.models import CohortDefinition, criterion_key
from ..cohort.sqlguard import validate_sql
from ..config import REPO_ROOT, get_settings
from ..db import connect, read_only
from ..extraction.llm import get_extractor, prompt_sha256
from ..retrieval.corpus import load_corpus
from ..retrieval.evidence import ANCHORS, anchored, evidence_for_definition, search
from ..retrieval.index import HybridIndex, chunk_document

BENCH = REPO_ROOT / "eval" / "benchmark"
REPORTS = REPO_ROOT / "eval" / "reports"


def _sha_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def code_version() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=5).stdout.strip() or "nogit"
    except Exception:  # noqa: BLE001
        return "nogit"


def load_jsonl(p: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def _canon(c: dict[str, Any]) -> str:
    d = {k: v for k, v in c.items() if v is not None and not (k == "lookback_days" and v == 365)}
    if "value" in d:
        d["value"] = round(float(d["value"]), 2)
    if "value_max" in d:
        d["value_max"] = round(float(d["value_max"]), 2)
    if "markers" in d:
        d["markers"] = sorted(d["markers"])
    if "states" in d:
        d["states"] = sorted(d["states"])
    if d.get("type") == "biomarker":
        d.setdefault("status", "detected")
    return json.dumps(d, sort_keys=True)


def _crit_set(defn: dict[str, Any]) -> set[tuple[str, str]]:
    return {("inclusion", _canon(c)) for c in defn.get("inclusion", [])} | {("exclusion", _canon(c)) for c in defn.get("exclusion", [])}


def _concepts(defn: dict[str, Any]) -> dict[tuple[str, str], set[str]]:
    out: dict[tuple[str, str], set[str]] = {}
    m = CohortDefinition.model_validate({**defn, "name": "x"})
    for role, items in (("inclusion", m.inclusion), ("exclusion", m.exclusion)):
        for c in items:
            out.setdefault(criterion_key(c), set()).add(role)
    return out


# Independent alias list for the hallucination check. It detects *fabricated* concepts (no
# surface evidence in the request at all); it cannot detect a semantically wrong mapping.
GROUND_EXTRA = {
    "age": r"\bage|\d+\s*(?:years|yrs|\+|or older|or younger)|adults?|older|over \d|under \d|between \d|>=?\s*\d|<=?\s*\d",
    "sex": r"wom[ae]n|\bm[ae]n\b|female|male|menopausal", "geography": r"\bin [A-Z]|\bfrom [A-Z]|living in|resid",
    "any_systemic_therapy": r"treat|therap|na[iï]ve|line", "nsclc": r"non[- ]?small|nsclc|lung adenocarcinoma|lung .*non",
    "sclc": r"sclc|small[- ]cell", "colorectal_cancer": r"\bcrc\b|colon|rectal|colorectal", "brain_metastases": r"brain|cns|intracranial",
    "ckd": r"\bckd\b|kidney|renal", "egfr_exon19del": r"\begfr\b", "egfr_l858r": r"\begfr\b", "egfr_t790m": r"\begfr\b|t790m",
    "braf_v600e": r"\bbraf\b", "kras_g12c": r"\bkras\b", "brca1_pathogenic": r"brca", "brca2_pathogenic": r"brca",
    "erbb2_amplification": r"her2|erbb2", "msi_high": r"msi|microsatellite|mismatch", "pdl1_tps": r"pd-?l1",
    "type2_diabetes": r"diabet|t2dm", "interstitial_lung_disease": r"interstitial|\bild\b", "heart_failure": r"heart failure|\bchf\b",
}


def grounded(request: str, key: tuple[str, str]) -> bool:
    t, k = key
    pats = []
    if t in ("age", "sex", "geography"):
        pats.append(GROUND_EXTRA[t])
    for part in k.split("|"):
        if part in ANCHORS:
            pats.append(ANCHORS[part])
        if part in GROUND_EXTRA:
            pats.append(GROUND_EXTRA[part])
    if t == "treatment" and not pats:
        pats.append(re.escape(k))
    import re as _re
    return any(_re.search(p, request, _re.I) for p in pats) if pats else True


import re  # noqa: E402  (used in grounded)


def _prf(tp: int, fp: int, fn: int) -> dict[str, float]:
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(2 * p * r / (p + r), 4) if p + r else 0.0}


def _eligible(defn: dict[str, Any], conn: Any) -> set[str]:
    q = compile_definition(CohortDefinition.model_validate({**defn, "name": "eval"}), get_settings().as_of_date)
    rows = conn.execute(q.sql, q.params).fetchall()
    return {r["person_id"] for r in rows if classify(r, q.criteria) == "eligible"}


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))], 3)


def eval_extraction(extractor_name: str, with_db: bool = True, subset: str | None = None) -> dict[str, Any]:
    items = load_jsonl(BENCH / "extraction_gold.jsonl")
    if subset:
        items = [i for i in items if i["subset"] == subset]
    ex = get_extractor(extractor_name)
    per_item: list[dict[str, Any]] = []
    tp = fp = fn = ctp = cfp = cfn = 0
    pol_ok = pol_n = 0
    halluc = n_pred = 0
    lat: list[float] = []
    cost: list[float] = []
    sql_ok = sql_n = 0
    coh_p: list[float] = []
    coh_r: list[float] = []
    both_empty = 0
    gold_cache: dict[str, set[str]] = {}
    conn_ctx = read_only() if with_db else None
    conn = conn_ctx.__enter__() if conn_ctx else None
    try:
        for it in items:
            res = ex.extract(it["request"])
            lat.append(res.latency_ms)
            if res.usage.get("cost_usd") is not None:
                cost.append(res.usage["cost_usd"])
            gold = it["gold"]
            pred = res.definition or {"inclusion": [], "exclusion": []}
            gs, ps = _crit_set(gold), _crit_set(pred)
            ctp += len(gs & ps)
            cfp += len(ps - gs)
            cfn += len(gs - ps)
            gc = _concepts(gold)
            pc = _concepts(pred) if res.valid else {}
            tp += len(set(gc) & set(pc))
            fp += len(set(pc) - set(gc))
            fn += len(set(gc) - set(pc))
            for k in set(gc) & set(pc):
                pol_n += 1
                pol_ok += gc[k] == pc[k]
            for k in pc:
                n_pred += 1
                halluc += not grounded(it["request"], k)
            cats: list[str] = []
            if not res.valid:
                cats.append("invalid_output")
            if set(gc) - set(pc):
                cats.append("missed_concept")
            if set(pc) - set(gc):
                cats.append("spurious_concept")
            if any(gc[k] != pc[k] for k in set(gc) & set(pc)):
                cats.append("wrong_polarity")
            if not cats and gs != ps:
                cats.append("wrong_value")
            detail: dict[str, Any] = {"request": it["request"], "subset": it["subset"], "predicted": pred,
                                      "missing": sorted(f"{x[0]}: {x[1]}" for x in gs - ps), "extra": sorted(f"{x[0]}: {x[1]}" for x in ps - gs),
                                      "unsupported": [u["text"] for u in res.unsupported], "latency_ms": res.latency_ms}
            if conn is not None and res.valid:
                sql_n += 1
                try:
                    q = compile_definition(CohortDefinition.model_validate(pred), get_settings().as_of_date)
                    if not validate_sql(q.sql).ok:
                        raise RuntimeError("sql guard rejected")
                    pe = _eligible(pred, conn)
                    sql_ok += 1
                    if it["id"] not in gold_cache:
                        gold_cache[it["id"]] = _eligible(gold, conn)
                    ge = gold_cache[it["id"]]
                    if not pe and not ge:
                        both_empty += 1
                        p = r = 1.0
                    else:
                        p = len(pe & ge) / len(pe) if pe else (1.0 if not ge else 0.0)
                        r = len(pe & ge) / len(ge) if ge else 1.0
                    coh_p.append(p)
                    coh_r.append(r)
                    detail.update(cohort_pred=len(pe), cohort_gold=len(ge), cohort_precision=round(p, 4), cohort_recall=round(r, 4))
                    if (p < 1 or r < 1) and not cats:
                        cats.append("cohort_mismatch")
                except Exception as exc:  # noqa: BLE001
                    conn.rollback()
                    cats.append("sql_failure")
                    detail["sql_error"] = str(exc)[:300]
            per_item.append({"item_id": it["id"], "passed": not cats, "failure_category": cats[0] if cats else None,
                             "categories": cats, "details": detail})
    finally:
        if conn_ctx:
            conn_ctx.__exit__(None, None, None)

    def subset_metrics(name: str) -> dict[str, Any]:
        xs = [x for x in per_item if x["details"]["subset"] == name]
        return {"n": len(xs), "exact_match": round(sum(x["passed"] or x["failure_category"] == "cohort_mismatch" and False for x in xs) / len(xs), 4) if xs else None}

    exact = sum(1 for x in per_item if not x["details"]["missing"] and not x["details"]["extra"])
    by_subset = {}
    for s in sorted({x["details"]["subset"] for x in per_item}):
        xs = [x for x in per_item if x["details"]["subset"] == s]
        by_subset[s] = {"n": len(xs), "exact_match": round(sum(1 for x in xs if not x["details"]["missing"] and not x["details"]["extra"]) / len(xs), 4),
                        "failure_categories": dict(Counter(c for x in xs for c in x["categories"]))}
    metrics = {
        "n_items": len(per_item),
        "structured_validity": round(sum(1 for x in per_item if "invalid_output" not in x["categories"]) / len(per_item), 4),
        "concept": _prf(tp, fp, fn), "criterion": _prf(ctp, cfp, cfn),
        "exact_match": round(exact / len(per_item), 4),
        "polarity_accuracy": round(pol_ok / pol_n, 4) if pol_n else None,
        "hallucination_rate": round(halluc / n_pred, 4) if n_pred else 0.0,
        "sql_success_rate": round(sql_ok / sql_n, 4) if sql_n else None,
        "cohort_precision": round(statistics.mean(coh_p), 4) if coh_p else None,
        "cohort_recall": round(statistics.mean(coh_r), 4) if coh_r else None,
        "cohort_items_both_empty": both_empty,
        "latency_ms": {"p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95)},
        "cost_usd_total": round(sum(cost), 6) if cost else (0.0 if extractor_name.startswith("rules") else None),
        "failure_categories": dict(Counter(c for x in per_item for c in x["categories"])),
        "by_subset": by_subset,
    }
    del subset_metrics
    return {"suite": "extraction", "system_version": ex.version, "metrics": metrics, "items": per_item,
            "prompt_sha256": prompt_sha256() if extractor_name.startswith(("llm", "gemini")) else None, "benchmark": BENCH / "extraction_gold.jsonl"}


def build_corpus_index() -> HybridIndex:
    chunks = [c for d in load_corpus(REPO_ROOT / "data" / "corpus") for c in chunk_document(d)]
    return HybridIndex(chunks)


def eval_retrieval(index: HybridIndex | None = None, ks: tuple[int, ...] = (1, 3, 5, 10)) -> dict[str, Any]:
    idx = index or build_corpus_index()
    items = load_jsonl(BENCH / "retrieval_gold.jsonl")
    out: dict[str, Any] = {}
    per_item = []
    for mode in ("bm25", "dense", "hybrid"):
        rec = {k: [] for k in ks}
        rr, lat = [], []
        abst_tp = abst_fp = abst_fn = abst_tn = 0
        for it in items:
            t0 = time.perf_counter()
            r = search(it["query"], k=max(ks) * 3, mode=mode, log=False, index=idx)
            lat.append((time.perf_counter() - t0) * 1000)
            docs: list[str] = []
            for h in r["results"]:
                if h["doc_id"] not in docs:
                    docs.append(h["doc_id"])
            rel = set(it["relevant_doc_ids"])
            if it["answerable"]:
                for k in ks:
                    rec[k].append(len(rel & set(docs[:k])) / len(rel))
                first = next((i for i, d in enumerate(docs) if d in rel), None)
                rr.append(1 / (first + 1) if first is not None else 0.0)
            flagged = r["insufficient_evidence"]
            if not it["answerable"]:
                abst_tp += flagged
                abst_fn += not flagged
            else:
                abst_fp += flagged
                abst_tn += not flagged
            if mode == "hybrid":
                per_item.append({"item_id": it["id"], "passed": (not it["answerable"] and flagged) or (it["answerable"] and bool(rel & set(docs[:5])) and not flagged),
                                 "failure_category": None if it["answerable"] and rel & set(docs[:5]) else ("missed_relevant" if it["answerable"] else ("not_abstained" if not flagged else None)),
                                 "details": {"query": it["query"], "top_docs": docs[:5], "relevant": sorted(rel), "insufficient": flagged}})
        out[mode] = {**{f"recall@{k}": round(statistics.mean(v), 4) for k, v in rec.items()}, "mrr": round(statistics.mean(rr), 4),
                     "abstention": {"precision": round(abst_tp / (abst_tp + abst_fp), 4) if abst_tp + abst_fp else None,
                                    "recall": round(abst_tp / (abst_tp + abst_fn), 4) if abst_tp + abst_fn else None,
                                    "false_abstentions_on_answerable": abst_fp},
                     "latency_ms": {"p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95)}}
    for it in per_item:  # fix up category for passed items
        if it["passed"]:
            it["failure_category"] = None
    return {"suite": "retrieval", "system_version": f"{idx.embedder.name}|bm25|rrf60|corpus-{idx.corpus_sha}",
            "metrics": {"n_items": len(items), "n_answerable": sum(i["answerable"] for i in items), **out}, "items": per_item,
            "benchmark": BENCH / "retrieval_gold.jsonl"}


# map cohort criterion concepts → corpus concept tags (for citation correctness)
CONCEPT_TO_CORPUS = {
    "brca1_pathogenic": "brca", "brca2_pathogenic": "brca", "brain_metastases": "ex_brain_mets", "heart_failure": "ex_heart_failure",
    "interstitial_lung_disease": "ex_ild", "hiv": "ex_hiv", "ckd": "ex_renal", "creatinine": "ex_renal", "egfr_renal": "ex_renal",
    "bilirubin": "ex_hepatic", "alt": "ex_hepatic", "ast": "ex_hepatic", "ecog": ("ecog_01", "ecog_02"), "hemoglobin": "hemoglobin_9",
    "platelets": "platelets_100", "pdl1_tps": "pdl1_high", "platinum_chemotherapy": "prior_platinum", "immunotherapy": ("prior_io", "pdl1_high"),
    "egfr_tki": "prior_egfr_tki", "endocrine_therapy": "prior_endocrine", "any_systemic_therapy": ("treatment_naive", "prior_platinum", "prior_io", "prior_egfr_tki", "prior_endocrine"),
    "lung_cancer": ("nsclc", "sclc"),
}


def eval_evidence(index: HybridIndex | None = None) -> dict[str, Any]:
    idx = index or build_corpus_index()
    corpus = {d["doc_id"]: set(d["concepts"]) for d in load_corpus(REPO_ROOT / "data" / "corpus")}
    covered = set().union(*corpus.values())
    items = load_jsonl(BENCH / "extraction_gold.jsonl")
    n_cit = n_cit_ok = n_stmt = n_stmt_bad = 0
    abst = Counter()
    per_item = []
    lat = []
    for it in items:
        defn = CohortDefinition.model_validate({**it["gold"], "name": it["id"]})
        out = evidence_for_definition(defn, index=idx)
        lat.append(out["latency_ms"])
        for e in out["items"]:
            tags: set[str] = set()
            for c in e["concepts"]:
                m = CONCEPT_TO_CORPUS.get(c, c)
                tags |= set(m) if isinstance(m, tuple) else {m}
            answerable = bool(tags & covered)
            for cit in e["citations"]:
                n_cit += 1
                n_cit_ok += bool(corpus.get(cit["doc_id"], set()) & tags)
            by_id = {c["chunk_id"]: c for c in e["citations"]}
            for s in e["statements"]:
                n_stmt += 1
                n_stmt_bad += not all(anchored(by_id[cid]["passage"], e["concepts"]) for cid in s["citations"] if cid in by_id) or \
                    any(cid not in by_id for cid in s["citations"])
            flagged = e["status"] == "insufficient_evidence"
            abst[("unanswerable" if not answerable else "answerable", flagged)] += 1
        per_item.append({"item_id": it["id"], "passed": True, "failure_category": None,
                         "details": {"criteria": [(e["label"], e["status"], [c["doc_id"] for c in e["citations"]]) for e in out["items"]]}})
    tp, fn = abst[("unanswerable", True)], abst[("unanswerable", False)]
    fp, tn = abst[("answerable", True)], abst[("answerable", False)]
    return {"suite": "evidence", "system_version": "evidence-1.0.0", "benchmark": BENCH / "extraction_gold.jsonl",
            "metrics": {"n_definitions": len(items), "n_criteria": tp + fn + fp + tn, "citations": n_cit,
                        "citation_precision": round(n_cit_ok / n_cit, 4) if n_cit else None,
                        "unsupported_claim_rate": round(n_stmt_bad / n_stmt, 4) if n_stmt else None,
                        "insufficient_evidence": {"correctly_flagged_unanswerable": tp, "missed_unanswerable": fn,
                                                  "flagged_but_answerable": fp, "supported_answerable": tn,
                                                  "abstention_recall": round(tp / (tp + fn), 4) if tp + fn else None,
                                                  "answerable_coverage": round(tn / (tn + fp), 4) if tn + fp else None},
                        "latency_ms": {"p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95)}},
            "items": per_item}


def record(result: dict[str, Any], duration_ms: int, with_db: bool = True) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    REPORTS.mkdir(parents=True, exist_ok=True)
    safe = result["system_version"].replace("/", "_").replace(":", "_").replace("|", "_")[:60]
    path = REPORTS / f"{ts}_{result['suite']}_{safe}.json"
    dv = data_version() if with_db else None
    payload = {"eval_run_id": run_id, "suite": result["suite"], "system_version": result["system_version"],
               "prompt_sha256": result.get("prompt_sha256"), "benchmark_sha256": _sha_file(result["benchmark"]),
               "data_version": dv, "code_version": code_version(), "timestamp": ts, "duration_ms": duration_ms,
               "metrics": result["metrics"], "items": result["items"]}
    path.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    if with_db:
        with connect() as c:
            c.execute("""INSERT INTO ops.eval_runs (eval_run_id, suite, system_version, prompt_sha256, benchmark_sha256, data_version, code_version,
                         duration_ms, metrics, n_items, report_path) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                      (run_id, result["suite"], result["system_version"], result.get("prompt_sha256"), payload["benchmark_sha256"], dv,
                       payload["code_version"], duration_ms, Jsonb(result["metrics"]), len(result["items"]), str(path.relative_to(REPO_ROOT))))
            with c.cursor() as cur:
                cur.executemany("INSERT INTO ops.eval_item_results (eval_run_id, item_id, passed, failure_category, details) VALUES (%s,%s,%s,%s,%s)",
                                [(run_id, x["item_id"], x["passed"], x["failure_category"], Jsonb(x["details"])) for x in result["items"]])
    return {"eval_run_id": run_id, "report_path": str(path), **{k: payload[k] for k in ("suite", "system_version", "metrics")}}


def run_suites(suites: list[str], extractors: list[str], with_db: bool = True) -> list[dict[str, Any]]:
    out = []
    idx = None
    for s in suites:
        if s == "extraction":
            for e in extractors:
                t0 = time.monotonic()
                r = eval_extraction(e, with_db=with_db)
                out.append(record(r, int((time.monotonic() - t0) * 1000), with_db))
        elif s in ("retrieval", "evidence"):
            idx = idx or build_corpus_index()
            t0 = time.monotonic()
            r = eval_retrieval(idx) if s == "retrieval" else eval_evidence(idx)
            out.append(record(r, int((time.monotonic() - t0) * 1000), with_db))
        else:
            raise ValueError(s)
    return out


def run_external(chia: Path | None = None, trec: Path | None = None, extractors: list[str] | None = None,
                 limit: int | None = None, with_db: bool = True) -> list[dict[str, Any]]:
    """External benchmarks (Chia concept extraction, TREC Clinical Trials retrieval)."""
    from .external.chia import eval_chia
    from .external.trec import eval_trec
    out = []
    if chia:
        for name in extractors or ["rules-v1", "rules-v2"]:
            t0 = time.monotonic()
            r = eval_chia(get_extractor(name), chia, limit=limit)
            r["prompt_sha256"] = prompt_sha256() if name.startswith(("llm", "gemini")) else None
            out.append(record(r, int((time.monotonic() - t0) * 1000), with_db))
    if trec:
        t0 = time.monotonic()
        r = eval_trec(trec, limit=limit)
        out.append(record(r, int((time.monotonic() - t0) * 1000), with_db))
    return out
