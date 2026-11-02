"""TREC Clinical Trials (2021/2022) patient → trial retrieval evaluation.

Topics are synthetic patient admission notes; qrels grade each judged trial as 0 (not
relevant), 1 (excluded) or 2 (eligible). We re-rank the *judged pool* of each topic with
CLINIQ's retrievers and report the track's standard measures on that pool:
  nDCG@10 (gains 0/1/2), P@10 (eligible only), MRR (first eligible), Recall@100 (eligible).

Caveats (also printed in reports):
- Re-ranking the judged pool is easier than full-corpus retrieval; numbers are not
  comparable with official TREC submissions.
- CLINIQ's retriever was designed for criterion → passage evidence lookup, not for matching
  a whole patient note to trials; this suite measures how far that transfers.
- Only judged trials whose text was available (corpus parts present) are included; coverage
  is reported.
"""
from __future__ import annotations

import json
import math
import re
import statistics
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any

from ...retrieval.index import Chunk, HybridIndex


def load_topics(path: Path) -> dict[str, str]:
    root = ET.parse(path).getroot()
    out = {}
    for t in root.iter("topic"):
        num = t.get("number") or t.findtext("number")
        text = "".join(t.itertext()).strip()
        if num:
            out[str(num)] = re.sub(r"\s+", " ", text)
    return out


def load_qrels(path: Path) -> dict[str, dict[str, int]]:
    q: dict[str, dict[str, int]] = defaultdict(dict)
    for line in path.read_text(encoding="utf-8").splitlines():
        f = line.split()
        if len(f) >= 4:
            q[f[0]][f[2]] = int(f[3])
    return q


def load_trials(path: Path) -> dict[str, dict[str, Any]]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            t = json.loads(line)
            out[t["nct_id"]] = t
    return out


def build_index(trials: dict[str, dict[str, Any]], max_chars: int = 4000) -> HybridIndex:
    chunks = []
    for i, (nct, t) in enumerate(sorted(trials.items())):
        text = " ".join(filter(None, [", ".join(t.get("conditions") or []), t.get("summary", "")[:800],
                                      (t.get("eligibility") or "")[:max_chars]]))
        chunks.append(Chunk(f"{nct}#0", nct, i, "Eligibility", text, 0, len(text), t.get("title") or nct, "trial_eligibility"))
    return HybridIndex(chunks)


def _dcg(gains: list[int]) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def eval_trec(trec_dir: Path, years: tuple[int, ...] = (2021, 2022), modes: tuple[str, ...] = ("bm25", "dense", "hybrid"),
              limit: int | None = None) -> dict[str, Any]:
    trials = load_trials(trec_dir / "trials_judged.jsonl")
    topics: dict[str, str] = {}
    qrels: dict[str, dict[str, int]] = {}
    used_years = []
    for y in years:
        tp, qp = trec_dir / f"topics{y}.xml", trec_dir / f"qrels{y}.txt"
        if tp.exists() and qp.exists():
            used_years.append(y)
            for k, v in load_topics(tp).items():
                topics[f"{y}-{k}"] = v
            for k, v in load_qrels(qp).items():
                qrels[f"{y}-{k}"] = v
    if not topics:
        raise FileNotFoundError(f"no topics/qrels found in {trec_dir}")
    t0 = time.monotonic()
    idx = build_index(trials)
    build_s = round(time.monotonic() - t0, 1)
    judged_total = sum(len(v) for v in qrels.values())
    judged_avail = sum(1 for v in qrels.values() for d in v if d in trials)
    keys = sorted(k for k in topics if k in qrels)
    if limit:
        keys = keys[:limit]
    results: dict[str, Any] = {}
    items = []
    for mode in modes:
        nd, p10, rr, r100 = [], [], [], []
        for k in keys:
            pool = {d: g for d, g in qrels[k].items() if d in trials}
            if not any(g == 2 for g in pool.values()):
                continue
            hits = idx.search(topics[k], k=len(idx.chunks), mode=mode, pool=len(idx.chunks))
            ranked = [h.chunk.doc_id for h in hits if h.chunk.doc_id in pool]
            ranked += [d for d in pool if d not in set(ranked)]  # unretrieved judged docs go last (deterministic order)
            gains = [pool[d] for d in ranked]
            ideal = sorted(pool.values(), reverse=True)
            nd.append(_dcg(gains[:10]) / _dcg(ideal[:10]) if _dcg(ideal[:10]) else 0.0)
            p10.append(sum(1 for g in gains[:10] if g == 2) / 10)
            first = next((i for i, g in enumerate(gains) if g == 2), None)
            rr.append(1 / (first + 1) if first is not None else 0.0)
            n_el = sum(1 for g in pool.values() if g == 2)
            r100.append(sum(1 for g in gains[:100] if g == 2) / n_el)
            if mode == "hybrid":
                items.append({"item_id": k, "passed": nd[-1] >= 0.5, "failure_category": None if nd[-1] >= 0.5 else "low_ndcg",
                              "details": {"ndcg@10": round(nd[-1], 4), "p@10": p10[-1], "pool": len(pool), "eligible_in_pool": n_el}})
        results[mode] = {"ndcg@10": round(statistics.mean(nd), 4) if nd else None, "p@10": round(statistics.mean(p10), 4) if p10 else None,
                         "mrr": round(statistics.mean(rr), 4) if rr else None, "recall@100": round(statistics.mean(r100), 4) if r100 else None,
                         "topics_scored": len(nd)}
    metrics = {"years": used_years, "topics": len(keys), "trials_indexed": len(trials), "judgments_total": judged_total,
               "judgments_with_text": judged_avail, "judgment_coverage": round(judged_avail / judged_total, 4) if judged_total else None,
               "index_build_s": build_s, "setting": "re-rank judged pool per topic (not full-corpus retrieval)", **results}
    return {"suite": "trec_ct", "system_version": f"{idx.embedder.name}|bm25|rrf60", "metrics": metrics, "items": items,
            "benchmark": trec_dir / f"qrels{used_years[-1]}.txt"}
