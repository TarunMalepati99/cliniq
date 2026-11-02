"""Assemble the TREC Clinical Trials 2021/2022 benchmark folder used by `cliniq eval --trec`.

Sources (in order of preference for trial text):
  1. official TREC CT corpus zips (ClinicalTrials.2021-04-27.part*.zip from trec-cds.org)
  2. NCBI TrialGPT preprocessed files (github.com/ncbi-nlp/TrialGPT, dataset/trec_2021|2022):
     topics (queries.jsonl / retrieved_trials.json), qrels (qrels/test.tsv) and trial text for
     the trials TrialGPT retrieved
Topics and qrels come from TrialGPT's copies of the official TREC files (NIST was unreachable
from the build sandbox); use scripts/fetch_external.py to fetch them directly from NIST.

Usage:
  git clone --depth 1 https://github.com/ncbi-nlp/TrialGPT /tmp/TrialGPT
  python scripts/build_trec_benchmark.py /tmp/TrialGPT "~/Downloads/ClinicalTrials*2021-04-27*part*.zip"
"""
import csv
import glob
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    tg = Path(sys.argv[1])
    zips = sorted(glob.glob(str(Path(sys.argv[2]).expanduser()))) if len(sys.argv) > 2 else []
    out = ROOT / "data" / "external" / "trec"
    out.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("fx", ROOT / "scripts" / "fetch_external.py")
    fx = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fx)
    commit = subprocess.run(["git", "-C", str(tg), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    manifest = {"source": "github.com/ncbi-nlp/TrialGPT (Jin et al., Nature Communications 2024): preprocessed TREC CT topics/qrels/trial text",
                "trialgpt_commit": commit, "corpus_zips": [Path(z).name for z in zips], "years": {}}
    judged, tg_trials = set(), {}
    for y in (2021, 2022):
        d = tg / "dataset" / f"trec_{y}"
        rt = json.loads((d / "retrieved_trials.json").read_text(encoding="utf-8"))
        topics = {p["patient_id"]: p["patient"] for p in rt}
        for line in (d / "queries.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                q = json.loads(line)
                topics.setdefault(q["_id"], q["text"])
        xml = [f'<topics task="TREC Clinical Trials {y} (via TrialGPT)">']
        xml += [f'<topic number="{k.replace(f"trec-{y}", "")}">{escape(v)}</topic>'
                for k, v in sorted(topics.items(), key=lambda kv: int(kv[0].replace(f"trec-{y}", "")))]
        (out / f"topics{y}.xml").write_text("\n".join(xml + ["</topics>"]), encoding="utf-8")
        rows = list(csv.DictReader((d / "qrels" / "test.tsv").open(), delimiter="\t"))
        (out / f"qrels{y}.txt").write_text("".join(f'{r["query-id"].replace(f"trec-{y}", "")} 0 {r["corpus-id"]} {r["score"]}\n' for r in rows), encoding="utf-8")
        judged |= {r["corpus-id"] for r in rows}
        for p in rt:
            for lab in ("0", "1", "2"):
                for t in p.get(lab, []):
                    tg_trials[t["NCTID"]] = t
        manifest["years"][y] = {"topics": len(topics), "judgments": len(rows), "eligible": sum(r["score"] == "2" for r in rows),
                                "excluded": sum(r["score"] == "1" for r in rows)}
    merged = fx.trials_from_corpus(zips, judged) if zips else {}
    n_official = len(merged)
    for nct, t in tg_trials.items():
        if nct not in merged:
            conds = t.get("diseases_list") if isinstance(t.get("diseases_list"), list) else []
            merged[nct] = {"nct_id": nct, "title": t.get("brief_title", ""), "conditions": conds, "summary": t.get("brief_summary", ""),
                           "eligibility": f"Inclusion {t.get('inclusion_criteria') or ''} Exclusion criteria{t.get('exclusion_criteria') or ''}",
                           "source": f"TrialGPT retrieved_trials.json@{commit[:7]}"}
    with (out / "trials_judged.jsonl").open("w", encoding="utf-8") as f:
        for t in merged.values():
            f.write(json.dumps(t) + "\n")
    manifest.update(judged_unique=len(judged), trials_with_text=len(merged), from_official_corpus=n_official,
                    from_trialgpt_only=len(merged) - n_official, coverage=round(len(merged) / len(judged), 4),
                    sha256={f: hashlib.sha256((out / f).read_bytes()).hexdigest()
                            for f in ("topics2021.xml", "qrels2021.txt", "topics2022.xml", "qrels2022.txt")})
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in manifest.items() if k != "sha256"}, indent=1))


if __name__ == "__main__":
    main()
