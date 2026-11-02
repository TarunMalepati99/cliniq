"""External benchmark plumbing: Gemini adapter (fake transport), Chia mapping, TREC scoring."""
import importlib.util
import io
import json
import zipfile
from pathlib import Path

import pytest

from cliniq.config import REPO_ROOT
from cliniq.evaluation.external import chia as C
from cliniq.evaluation.external import trec as T
from cliniq.extraction.gemini import GeminiError, GeminiExtractor, choose_model
from cliniq.extraction.rules import RuleExtractor


# ---------------------------------------------------------------- Gemini (no network)
class FakeGemini:
    def __init__(self, replies, models=("gemini-test-flash",)):
        self.replies, self.models, self.calls, self.keys = list(replies), models, [], set()

    def __call__(self, url, body, key):
        self.keys.add(key)
        if body is None:
            return {"models": [{"name": f"models/{m}", "supportedGenerationMethods": ["generateContent"]} for m in self.models]}
        self.calls.append((url, body))
        text = self.replies.pop(0)
        return {"candidates": [{"content": {"parts": [{"text": text}]}}], "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}}


def test_gemini_validates_repairs_and_never_exposes_key():
    bad = json.dumps({"inclusion": [{"type": "diagnosis", "condition": "lung cancer"}]})
    good = json.dumps({"inclusion": [{"type": "diagnosis", "condition": "lung_cancer"}]})
    fake = FakeGemini([bad, good])
    ex = GeminiExtractor(model="gemini-test-flash", api_key="secret-key-123", transport=fake, interactive=False)
    r = ex.extract("lung cancer patients")
    assert r.valid and len(fake.calls) == 2 and r.usage["input_tokens"] == 20
    assert "secret-key-123" not in repr(ex) and "secret-key-123" not in json.dumps(r.__dict__, default=str)
    assert all("key=" not in url for url, _ in fake.calls)  # key travels in a header, never the URL


def test_gemini_rejects_invalid_output_without_fallback():
    fake = FakeGemini(["not json", json.dumps({"inclusion": [{"type": "sql", "q": "DROP TABLE x"}]})])
    r = GeminiExtractor(model="gemini-test-flash", api_key="k", transport=fake, interactive=False).extract("x")
    assert not r.valid and r.definition is None


def test_gemini_model_must_exist_for_key():
    with pytest.raises(GeminiError, match="not available"):
        choose_model("k", "gemini-imaginary", interactive=False, transport=FakeGemini([], models=("gemini-a-flash",)))


def test_gemini_requires_key_noninteractive(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(GeminiError):
        GeminiExtractor(transport=FakeGemini([]), interactive=False)


# ---------------------------------------------------------------- Chia (BRAT → normalized → concept eval)
def _fetch_module():
    spec = importlib.util.spec_from_file_location("fetch_external", REPO_ROOT / "scripts" / "fetch_external.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def chia_file(tmp_path: Path) -> Path:
    inc_txt = "Adults with type 2 diabetes\nHbA1c above 7%\n"
    inc_ann = ("T1\tPerson 0 6\tAdults\nT2\tCondition 12 27\ttype 2 diabetes\nT3\tMeasurement 28 33\tHbA1c\nT4\tValue 34 42\tabove 7%\n"
               "R1\tHas_value Arg1:T3 Arg2:T4\n")
    exc_txt = "Heart failure\nNo history of hypertension\nPregnancy\n"
    exc_ann = ("T1\tCondition 0 13\tHeart failure\nT2\tNegation 14 16\tNo\nT3\tCondition 28 40\thypertension\n"
               "T4\tCondition 41 50\tPregnancy\nR1\tHas_negation Arg1:T3 Arg2:T2\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, b in [("NCT1_inc.txt", inc_txt), ("NCT1_inc.ann", inc_ann), ("NCT1_exc.txt", exc_txt), ("NCT1_exc.ann", exc_ann)]:
            z.writestr(f"chia_with_scope/{n}", b)
    recs = _fetch_module().chia_from_zip_bytes(buf.getvalue(), "fixture")
    p = tmp_path / "chia.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in recs))
    return p


def test_chia_gold_mapping_polarity_and_coverage(chia_file):
    units = {u["text"]: u for u in C.load_units(chia_file)}
    assert units["Adults with type 2 diabetes"]["gold"] == {("age", "age"): {"inclusion"}, ("diagnosis", "type2_diabetes"): {"inclusion"}}
    assert units["Heart failure"]["gold"] == {("diagnosis", "heart_failure"): {"exclusion"}}
    # negation inside the exclusion section flips back to inclusion
    assert units["No history of hypertension"]["gold"] == {("diagnosis", "hypertension"): {"inclusion"}}
    assert units["Pregnancy"]["gold"] == {} and units["Pregnancy"]["unmapped_conditions"] == ["pregnancy"]


def test_chia_eval_runs_end_to_end(chia_file):
    r = C.eval_chia(RuleExtractor("rules-v2"), chia_file)
    m = r["metrics"]
    assert m["n_units_evaluated"] == 4 and m["concept"]["recall"] > 0.5
    assert m["vocabulary_coverage"]["clinical_entities"] == 5


# ---------------------------------------------------------------- TREC (scoring math on a toy pool)
def test_trec_scoring(tmp_path: Path):
    (tmp_path / "topics2022.xml").write_text('<topics task="2022 TREC Clinical Trials"><topic number="1">58-year-old woman with '
                                             'non-small cell lung cancer, EGFR exon 19 deletion, previously treated</topic></topics>')
    (tmp_path / "qrels2022.txt").write_text("1 0 NCT01 2\n1 0 NCT02 1\n1 0 NCT03 0\n")
    trials = [{"nct_id": "NCT01", "title": "EGFR NSCLC", "conditions": ["Non-small Cell Lung Cancer"], "summary": "",
               "eligibility": "Inclusion: NSCLC with EGFR exon 19 deletion, prior treatment."},
              {"nct_id": "NCT02", "title": "Lung", "conditions": ["Lung Cancer"], "summary": "", "eligibility": "Exclusion: EGFR mutation"},
              {"nct_id": "NCT03", "title": "Asthma", "conditions": ["Asthma"], "summary": "", "eligibility": "Inclusion: asthma, children"}]
    (tmp_path / "trials_judged.jsonl").write_text("".join(json.dumps(t) + "\n" for t in trials))
    r = T.eval_trec(tmp_path, years=(2022,), modes=("bm25",))
    m = r["metrics"]["bm25"]
    assert m["topics_scored"] == 1 and m["mrr"] == 1.0 and 0 < m["ndcg@10"] <= 1
    assert r["metrics"]["judgment_coverage"] == 1.0
