"""Download and normalize external benchmark data for CLINIQ (run on your own machine).

  python scripts/fetch_external.py                 # → ~/Downloads/cliniq_external/
  python scripts/fetch_external.py --out D:/data/cliniq_external --trec-corpus "C:/Users/me/Downloads/ClinicalTrials*.zip"

Produces (all public data, no credentials needed):
  chia.jsonl              one record per criteria section (inclusion/exclusion) of each Chia trial,
                          normalized from either Hugging Face bigbio/chia (chia_bigbio_kb) or the
                          official figshare BRAT release (fallback)
  chia_sample_raw.json    first raw record as downloaded (lets CLINIQ verify the schema mapping)
  trec/topics2022.xml, trec/qrels2022.txt   (+ 2021 when available)  from trec.nist.gov
  trec/trials_judged.jsonl  eligibility text of every judged trial, extracted from the TREC
                          ClinicalTrials corpus zips (2021-04-27 snapshot, trec-cds.org); falls back
                          to the ClinicalTrials.gov API (current text — noted in the file)
  MANIFEST.json           what was fetched, from where, counts and sha256s

Requirements: Python 3.9+. Optional: `pip install "datasets<4"` for the Hugging Face path
(datasets 4.x dropped dataset scripts, which bigbio/chia uses). No API keys are needed here.

Licenses: Chia — see the figshare record (https://doi.org/10.6084/m9.figshare.11855817.v2);
TREC topics/qrels — NIST; ClinicalTrials.gov — NLM terms of use.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import io
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

CHIA_FIGSHARE = "https://figshare.com/ndownloader/articles/11855817/versions/2"
TREC = {2022: ("https://trec.nist.gov/data/trials/topics2022.xml", "https://trec.nist.gov/data/trials/qrels2022.txt"),
        2021: ("https://trec.nist.gov/data/trials/topics2021.xml", "https://trec.nist.gov/data/trials/qrels2021.txt")}
CTGOV = "https://clinicaltrials.gov/api/v2/studies"
UA = {"User-Agent": "cliniq-fetch/1.0 (research benchmark download)"}


def get(url: str, timeout: int = 120) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def section_of(name: str, fallback: str = "") -> str:
    n = (name or "").lower()
    if re.search(r"(^|[_\-\s])exc", n) or "exclusion" in n:
        return "exclusion"
    if re.search(r"(^|[_\-\s])inc", n) or "inclusion" in n:
        return "inclusion"
    return fallback


# ---------------------------------------------------------------- Chia via Hugging Face
def chia_from_hf(out: Path) -> list[dict] | None:
    try:
        from datasets import load_dataset
    except ImportError:
        print("  datasets not installed (pip install \"datasets<4\"); skipping Hugging Face")
        return None
    ds = None
    for kwargs in ({}, {"trust_remote_code": True}):
        try:
            ds = load_dataset("bigbio/chia", "chia_bigbio_kb", **kwargs)
            break
        except Exception as e:  # noqa: BLE001
            print(f"  load_dataset failed ({type(e).__name__}: {str(e)[:160]})")
    if ds is None:
        return None
    records: list[dict] = []
    first = True
    for split in ds:
        for ex in ds[split]:
            ex = dict(ex)
            if first:
                (out / "chia_sample_raw.json").write_text(json.dumps(ex, indent=1, default=str)[:200000], encoding="utf-8")
                first = False
            ents = [{"id": e["id"], "type": e["type"], "text": " ".join(e["text"]),
                     "spans": [list(map(int, o)) for o in e["offsets"]]} for e in ex.get("entities", [])]
            rels = [{"type": r["type"], "arg1": r["arg1_id"], "arg2": r["arg2_id"]} for r in ex.get("relations", [])]
            passages = ex.get("passages") or []
            for p in passages:
                ptext = " ".join(p["text"])
                (ps, pe) = map(int, p["offsets"][0])
                sec = section_of(p.get("type", ""), section_of(ex.get("document_id", ""), "inclusion"))
                pents = [{**e, "spans": [[s - ps, t - ps] for s, t in e["spans"]]} for e in ents
                         if e["spans"] and ps <= e["spans"][0][0] < pe]
                ids = {e["id"] for e in pents}
                records.append({"doc_id": f"{ex.get('document_id', ex.get('id'))}:{p.get('id', sec)}",
                                "trial_id": re.sub(r"_(inc|exc).*$", "", str(ex.get("document_id", ""))), "section": sec,
                                "text": ptext, "entities": pents,
                                "relations": [r for r in rels if r["arg1"] in ids or r["arg2"] in ids], "source": "hf:bigbio/chia:chia_bigbio_kb"})
    return records


# ---------------------------------------------------------------- Chia via figshare BRAT
def parse_brat(txt: str, ann: str) -> tuple[list[dict], list[dict]]:
    ents, rels = [], []
    for line in ann.splitlines():
        if line.startswith("T"):
            try:
                tid, meta, text = line.split("\t", 2)
            except ValueError:
                continue
            etype, locs = meta.split(" ", 1)
            spans = [[int(a), int(b)] for a, b in (seg.split() for seg in locs.split(";"))]
            ents.append({"id": tid, "type": etype, "text": text, "spans": spans})
        elif line.startswith("R"):
            parts = line.split("\t")
            if len(parts) >= 2:
                f = parts[1].split()
                if len(f) == 3:
                    rels.append({"type": f[0], "arg1": f[1].split(":", 1)[1], "arg2": f[2].split(":", 1)[1]})
        elif line.startswith("*"):  # equivalence / OR groups:  *\tOR T1 T2 ...
            parts = line.split("\t")
            if len(parts) >= 2:
                f = parts[1].split()
                for a, b in zip(f[1:], f[2:], strict=False):
                    rels.append({"type": f[0], "arg1": a, "arg2": b})
    return ents, rels


def chia_from_zip_bytes(blob: bytes, source: str) -> list[dict]:
    files: dict[str, bytes] = {}

    def walk(zb: bytes) -> None:
        with zipfile.ZipFile(io.BytesIO(zb)) as z:
            for n in z.namelist():
                if n.lower().endswith(".zip") and "without_scope" not in n.lower():
                    walk(z.read(n))
                elif n.endswith((".txt", ".ann")) and "__MACOSX" not in n:
                    files[Path(n).name] = z.read(n)
    walk(blob)
    records = []
    for name in sorted(files):
        if not name.endswith(".txt"):
            continue
        stem = name[:-4]
        ann = files.get(stem + ".ann")
        if ann is None:
            continue
        txt = files[name].decode("utf-8", errors="replace")
        ents, rels = parse_brat(txt, ann.decode("utf-8", errors="replace"))
        records.append({"doc_id": stem, "trial_id": re.sub(r"_(inc|exc).*$", "", stem), "section": section_of(stem, "inclusion"),
                        "text": txt, "entities": ents, "relations": rels, "source": source})
    return records


def chia_from_figshare(out: Path, local_zip: str | None) -> list[dict] | None:
    try:
        if local_zip:
            blob = Path(local_zip).read_bytes()
            src = f"local:{Path(local_zip).name}"
        else:
            print("  downloading Chia from figshare …")
            blob = get(CHIA_FIGSHARE, timeout=600)
            src = CHIA_FIGSHARE
    except Exception as e:  # noqa: BLE001
        print(f"  figshare download failed: {e}")
        return None
    recs = chia_from_zip_bytes(blob, src)
    if recs:
        (out / "chia_sample_raw.json").write_text(json.dumps(recs[0], indent=1)[:200000], encoding="utf-8")
    return recs


# ---------------------------------------------------------------- TREC
def trec_files(out: Path, manifest: dict) -> dict[int, tuple[Path, Path]]:
    d = out / "trec"
    d.mkdir(parents=True, exist_ok=True)
    got = {}
    for year, (t_url, q_url) in TREC.items():
        try:
            t, q = get(t_url), get(q_url)
        except Exception as e:  # noqa: BLE001
            print(f"  TREC {year}: not downloaded ({e})")
            continue
        (d / f"topics{year}.xml").write_bytes(t)
        (d / f"qrels{year}.txt").write_bytes(q)
        manifest["trec"][str(year)] = {"topics": t_url, "qrels": q_url, "topics_sha256": sha(t), "qrels_sha256": sha(q),
                                       "judgments": sum(1 for line in q.decode().splitlines() if line.strip())}
        got[year] = (d / f"topics{year}.xml", d / f"qrels{year}.txt")
        print(f"  TREC {year}: topics + {manifest['trec'][str(year)]['judgments']} judgments")
    return got


def judged_ids(qrels: list[Path]) -> set[str]:
    ids: set[str] = set()
    for q in qrels:
        for line in q.read_text(encoding="utf-8").splitlines():
            f = line.split()
            if len(f) >= 4:
                ids.add(f[2])
    return ids


def _text(el: ET.Element | None, path: str) -> str:
    x = el.find(path) if el is not None else None
    return re.sub(r"\s+", " ", "".join(x.itertext())).strip() if x is not None else ""


def trials_from_corpus(zips: list[str], wanted: set[str]) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for zp in zips:
        try:
            z = zipfile.ZipFile(zp)
        except zipfile.BadZipFile:
            print(f"  skipping {zp}: not a complete zip (still downloading?)")
            continue
        for n in z.namelist():
            m = re.search(r"(NCT\d{8})\.xml$", n)
            if not m or m.group(1) not in wanted or m.group(1) in found:
                continue
            try:
                root = ET.fromstring(z.read(n))
            except ET.ParseError:
                continue
            found[m.group(1)] = {"nct_id": m.group(1), "title": _text(root, "brief_title"),
                                 "conditions": [c.text for c in root.findall("condition") if c.text],
                                 "summary": _text(root, "brief_summary/textblock"),
                                 "eligibility": _text(root, "eligibility/criteria/textblock"),
                                 "min_age": _text(root, "eligibility/minimum_age"), "max_age": _text(root, "eligibility/maximum_age"),
                                 "gender": _text(root, "eligibility/gender"), "source": f"trec-corpus:{Path(zp).name}"}
        print(f"  {Path(zp).name}: {len(found)} judged trials found so far")
    return found


def trials_from_ctgov(ids: list[str]) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        url = f"{CTGOV}?filter.ids={','.join(chunk)}&pageSize=200&format=json"
        try:
            data = json.loads(get(url))
        except Exception as e:  # noqa: BLE001
            print(f"  ClinicalTrials.gov batch {i // 200} failed: {e}")
            continue
        for s in data.get("studies", []):
            ps = s.get("protocolSection", {})
            nct = ps.get("identificationModule", {}).get("nctId")
            el = ps.get("eligibilityModule", {})
            found[nct] = {"nct_id": nct, "title": ps.get("identificationModule", {}).get("briefTitle", ""),
                          "conditions": ps.get("conditionsModule", {}).get("conditions", []),
                          "summary": ps.get("descriptionModule", {}).get("briefSummary", ""),
                          "eligibility": el.get("eligibilityCriteria", ""), "min_age": el.get("minimumAge", ""),
                          "max_age": el.get("maximumAge", ""), "gender": el.get("sex", ""),
                          "source": "clinicaltrials.gov-api-v2 (CURRENT text, not the 2021 TREC snapshot)"}
        print(f"  ClinicalTrials.gov: {len(found)}/{len(ids)}")
    return found


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path.home() / "Downloads" / "cliniq_external")
    ap.add_argument("--chia-zip", help="path to a manually downloaded Chia figshare zip")
    ap.add_argument("--trec-corpus", default=str(Path.home() / "Downloads" / "ClinicalTrials*.zip"),
                    help="glob for TREC ClinicalTrials corpus zips (trec-cds.org 2021/2022 collection)")
    ap.add_argument("--skip-chia", action="store_true")
    ap.add_argument("--skip-trec", action="store_true")
    ap.add_argument("--ctgov-fallback", action="store_true", help="fetch judged trials missing from the corpus via the ClinicalTrials.gov API")
    a = ap.parse_args()
    out = a.out
    out.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"chia": {}, "trec": {}}

    if not a.skip_chia:
        print("Chia:")
        recs = None if a.chia_zip else chia_from_hf(out)
        if not recs:
            recs = chia_from_figshare(out, a.chia_zip)
        if recs:
            body = "".join(json.dumps(r) + "\n" for r in recs)
            (out / "chia.jsonl").write_text(body, encoding="utf-8")
            manifest["chia"] = {"records": len(recs), "trials": len({r["trial_id"] for r in recs}),
                                "entities": sum(len(r["entities"]) for r in recs), "source": recs[0]["source"], "sha256": sha(body.encode())}
            print(f"  wrote chia.jsonl: {manifest['chia']}")
        else:
            print("  Chia NOT fetched. Download the zip from https://doi.org/10.6084/m9.figshare.11855817.v2 and rerun with --chia-zip")

    if not a.skip_trec:
        print("TREC Clinical Trials:")
        got = trec_files(out, manifest)
        if got:
            wanted = judged_ids([q for _, q in got.values()])
            zips = sorted(glob.glob(a.trec_corpus))
            print(f"  {len(wanted)} judged trials; corpus zips found: {len(zips)}")
            trials = trials_from_corpus(zips, wanted) if zips else {}
            missing = sorted(wanted - set(trials))
            if missing and a.ctgov_fallback:
                trials.update(trials_from_ctgov(missing))
                missing = sorted(wanted - set(trials))
            with (out / "trec" / "trials_judged.jsonl").open("w", encoding="utf-8") as f:
                for t in trials.values():
                    f.write(json.dumps(t) + "\n")
            manifest["trec"]["trials_judged"] = len(trials)
            manifest["trec"]["trials_missing"] = len(missing)
            print(f"  wrote trials_judged.jsonl ({len(trials)} trials, {len(missing)} judged trials missing)")
            if missing and not a.ctgov_fallback:
                print("  tip: finish downloading the corpus zips from https://www.trec-cds.org/2022.html#documents, or add --ctgov-fallback")

    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nDone → {out}\nNext: tell Claude the folder is ready (or run: cliniq eval --chia {out / 'chia.jsonl'} --trec {out / 'trec'})")


if __name__ == "__main__":
    sys.exit(main())
