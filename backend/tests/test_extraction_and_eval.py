import json
from types import SimpleNamespace

import pytest

from cliniq.config import REPO_ROOT
from cliniq.extraction.llm import LLMExtractor
from cliniq.extraction.rules import RuleExtractor

V2 = RuleExtractor("rules-v2")
THRESH = json.loads((REPO_ROOT / "eval" / "thresholds.json").read_text())


def _crit(text):
    r = V2.extract(text)
    assert r.valid, r.errors
    return r.definition["inclusion"], r.definition["exclusion"], r


def test_brief_example():
    inc, exc, _ = _crit("Find patients aged 40–70 diagnosed with non-small-cell lung cancer, with an EGFR exon 19 deletion, "
                        "and a documented history of prior treatment.")
    types = {c["type"] for c in inc}
    assert types == {"age", "diagnosis", "biomarker", "treatment"} and not exc


def test_negation_scope_and_reentry():
    inc, exc, _ = _crit("KRAS G12C NSCLC without CKD, creatinine < 1.5 mg/dL")
    assert {"type": "diagnosis", "condition": "ckd"} in exc
    assert any(c["type"] == "lab" and c["lab"] == "creatinine" for c in inc)


def test_unit_normalization():
    inc, _, _ = _crit("NSCLC, hemoglobin of at least 90 g/L, platelets >= 100,000/uL")
    labs = {c["lab"]: c["value"] for c in inc if c["type"] == "lab"}
    assert labs == {"hemoglobin": 9.0, "platelets": 100.0}


def test_negative_biomarker_and_naive():
    inc, exc, _ = _crit("Treatment-naive ALK-negative NSCLC")
    assert {"type": "biomarker", "markers": ["alk_fusion"], "status": "not_detected"} in inc
    assert exc == [{"type": "treatment", "drug_class": "any_systemic_therapy", "drug": None}]


def test_unsupported_criteria_are_reported_not_dropped():
    _, _, r = _crit("Colorectal cancer, life expectancy > 3 months, measurable disease per RECIST")
    reasons = " ".join(u["reason"] for u in r.unsupported)
    assert "life expectancy" in reasons and "RECIST" in reasons


def test_v1_baseline_lacks_negation():
    r = RuleExtractor("rules-v1").extract("NSCLC without brain metastases")
    assert r.definition["exclusion"] == []  # documented baseline weakness


# ---------------------------------------------------------------- LLM adapter with a fake client

class FakeClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.messages = self
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
        out = self.outputs.pop(0)
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=100, output_tokens=50),
                               content=[SimpleNamespace(type="tool_use", input=out)])


def test_llm_extractor_validates_and_repairs():
    bad = {"inclusion": [{"type": "diagnosis", "condition": "lung cancer"}]}      # not an allowlisted key
    good = {"inclusion": [{"type": "diagnosis", "condition": "lung_cancer"}]}
    fc = FakeClient([bad, good])
    r = LLMExtractor(client=fc, model="fake").extract("lung cancer patients")
    assert r.valid and fc.calls == 2 and r.usage["input_tokens"] == 200


def test_llm_extractor_never_accepts_invalid_output():
    bad = {"inclusion": [{"type": "sql", "query": "DROP TABLE clinical.person"}]}
    r = LLMExtractor(client=FakeClient([bad, bad]), model="fake").extract("anything")
    assert not r.valid and r.definition is None and r.errors


# ---------------------------------------------------------------- benchmark regression gates (no DB needed)

@pytest.fixture(scope="module")
def corpus_index():
    from cliniq.evaluation.run import build_corpus_index
    return build_corpus_index()


def test_benchmark_has_at_least_100_items():
    from cliniq.evaluation.run import BENCH, load_jsonl
    items = load_jsonl(BENCH / "extraction_gold.jsonl")
    assert len(items) >= 100 and len({i["id"] for i in items}) == len(items)


def test_extraction_regression_floor(db):
    from cliniq.evaluation.run import eval_extraction
    m = eval_extraction("rules-v2", with_db=False)["metrics"]
    t = THRESH["extraction"]["rules-v2"]
    assert m["structured_validity"] >= t["structured_validity"]
    assert m["concept"]["f1"] >= t["concept_f1"]
    assert m["criterion"]["f1"] >= t["criterion_f1"]
    assert m["hallucination_rate"] <= t["hallucination_rate_max"]
    assert m["by_subset"]["handwritten"]["exact_match"] >= t["exact_match_handwritten"]
    assert m["by_subset"]["templated"]["exact_match"] >= t["exact_match_templated"]


def test_retrieval_regression_floor(corpus_index):
    from cliniq.evaluation.run import eval_retrieval
    m = eval_retrieval(corpus_index)["metrics"]["hybrid"]
    assert m["recall@5"] >= THRESH["retrieval"]["hybrid"]["recall@5"]
    assert m["mrr"] >= THRESH["retrieval"]["hybrid"]["mrr"]


def test_evidence_regression_floor(corpus_index):
    from cliniq.evaluation.run import eval_evidence
    m = eval_evidence(corpus_index)["metrics"]
    t = THRESH["evidence"]
    assert m["citation_precision"] >= t["citation_precision"]
    assert m["insufficient_evidence"]["abstention_recall"] >= t["abstention_recall"]
    assert m["unsupported_claim_rate"] <= t["unsupported_claim_rate_max"]
