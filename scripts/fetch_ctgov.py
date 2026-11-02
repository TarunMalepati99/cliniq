"""Fetch real, public clinical-trial eligibility text from ClinicalTrials.gov (API v2) into the
evidence corpus (data/corpus/ctgov/). Run on a machine with internet access, then
`cliniq index`. Respect ClinicalTrials.gov terms of use; cite NCT IDs when reusing content.

UNTESTED in the build environment (outbound network was restricted there).

Usage: python scripts/fetch_ctgov.py "EGFR exon 19 non-small cell lung cancer" --max 25
"""
import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://clinicaltrials.gov/api/v2/studies"
OUT = Path(__file__).resolve().parents[1] / "data" / "corpus" / "ctgov"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--max", type=int, default=25)
    a = ap.parse_args()
    params = {"query.term": a.query, "pageSize": min(a.max, 100), "format": "json",
              "fields": "NCTId,BriefTitle,EligibilityCriteria,Condition,Phase"}
    with urllib.request.urlopen(f"{API}?{urllib.parse.urlencode(params)}", timeout=30) as r:
        data = json.load(r)
    OUT.mkdir(parents=True, exist_ok=True)
    n = 0
    for s in data.get("studies", []):
        ps = s.get("protocolSection", {})
        nct = ps.get("identificationModule", {}).get("nctId")
        title = ps.get("identificationModule", {}).get("briefTitle", "")
        crit = ps.get("eligibilityModule", {}).get("eligibilityCriteria")
        if not (nct and crit):
            continue
        meta = {"doc_id": nct, "title": f"{nct}: {title}", "doc_type": "trial_eligibility", "concepts": [],
                "source": f"ClinicalTrials.gov https://clinicaltrials.gov/study/{nct}", "license": "ClinicalTrials.gov terms of use"}
        body = f"# {nct}: {title}\n\n## Eligibility Criteria\n{crit}\n"
        (OUT / f"{nct}.md").write_text(f"<!-- meta: {json.dumps(meta)} -->\n{body}")
        n += 1
    print(f"wrote {n} studies to {OUT}; run `cliniq index` to re-index")


if __name__ == "__main__":
    main()
