"""Deterministic natural-language → CohortDefinition extractor.

Two versions share one lexicon:
  rules-v1  baseline: keyword lexicon, symbolic comparators, 'exclud*' as the only
            exclusion cue, simple age ranges.
  rules-v2  adds negation/exclusion scoping (without, no prior, treatment-naive, …),
            word comparators (at least, below, over…), integer-age conventions,
            unit normalization (g/L, µmol/L, platelets/µL), negative biomarker status
            (ALK-negative, EGFR wild-type), class generalizations (chemotherapy,
            diabetes), geography, and explicit detection of criteria the schema cannot
            represent (reported as `unsupported`, never silently dropped).

Conventions (shared with the gold benchmark, see eval/benchmark/README.md):
  "over/older than X" → min X+1; "X or older / at least X / ≥X" → min X
  "under/younger than X" → max X-1; "X or younger / up to X / ≤X" → max X
  "EGFR-mutant / EGFR mutation" → markers [egfr_exon19del, egfr_l858r]
  "without X" → exclusion of X; "X-negative / X wild-type" → inclusion X not_detected
  "ECOG 0-1" → ecog <= 1
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from .. import vocabulary as V
from ..cohort.models import CohortDefinition

FLAGS = re.I


@dataclass
class Mention:
    start: int
    end: int
    kind: str
    payload: dict[str, Any]
    flip: bool = False      # mention itself implies exclusion (e.g. 'treatment-naive')
    priority: int = 0

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass
class ExtractionResult:
    version: str
    normalized_text: str
    definition: dict[str, Any] | None
    valid: bool
    errors: list[str] = field(default_factory=list)
    mentions: list[dict[str, Any]] = field(default_factory=list)
    unsupported: list[dict[str, Any]] = field(default_factory=list)
    required_fields: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- lexicon
DIAGNOSIS = [
    ("nsclc", r"non[- ]?small[- ]?cell lung (?:cancer|carcinoma)|\bnsclc\b|lung adenocarcinoma|adenocarcinoma of the lung|squamous(?: cell)? (?:nsclc|lung cancer|carcinoma of the lung)"),
    ("sclc", r"\bsclc\b|small[- ]cell lung (?:cancer|carcinoma)|small[- ]cell carcinoma of the lung"),
    ("lung_cancer", r"lung (?:cancer|carcinoma|malignancy)"),
    ("breast_cancer", r"breast (?:cancer|carcinoma)"),
    ("colorectal_cancer", r"colorectal (?:cancer|carcinoma|adenocarcinoma)|colon cancer|rectal cancer|\bcrc\b"),
    ("pancreatic_cancer", r"pancreatic (?:cancer|adenocarcinoma|carcinoma)|\bpdac\b"),
    ("melanoma", r"(?:cutaneous )?melanoma"),
    ("prostate_cancer", r"prostate (?:cancer|carcinoma|adenocarcinoma)"),
    ("brain_metastases", r"(?:brain|cns|intracranial) (?:metastases|metastasis|mets|lesions)"),
    ("heart_failure", r"(?:congestive )?heart failure|\bchf\b"),
    ("ckd", r"chronic kidney disease|\bckd\b"),
    ("cirrhosis", r"(?:liver |hepatic )?cirrhosis"),
    ("interstitial_lung_disease", r"interstitial lung disease|\bild\b"),
    ("hiv", r"\bhiv\b(?: infection| disease)?"),
    ("hypertension", r"hypertension"),
    ("type2_diabetes", r"type (?:2|ii) diabetes(?: mellitus)?|\bt2dm\b"),
]
DIAGNOSIS_V2 = [("type2_diabetes", r"\bdiabet(?:es|ic)\b"), ("ckd", r"renal insufficiency|chronic renal (?:disease|failure)"),
                ("hypertension", r"high blood pressure")]

BIOMARKER = [
    (["egfr_exon19del"], r"egfr (?:exon[- ]?19 (?:deletions?|del)|ex19del|exon[- ]?19)|exon[- ]?19 deletions?|\bex19del\b"),
    (["egfr_l858r"], r"(?:egfr )?\bl858r\b(?: mutation| substitution)?"),
    (["egfr_t790m"], r"(?:egfr )?\bt790m\b(?: mutation)?"),
    (["kras_g12c"], r"kras g12c(?: mutation)?|kras\^?g12c"),
    (["alk_fusion"], r"alk[- ](?:positive|rearrange(?:d|ment)|fusion|translocation)|\balk\+|eml4[- ]alk"),
    (["ros1_fusion"], r"ros1[- ](?:positive|rearrange(?:d|ment)|fusion)"),
    (["braf_v600e"], r"braf v600e?(?: mutation)?|braf[- ](?:mutant|mutated|mutation)"),
    (["erbb2_amplification"], r"her2[- ](?:positive|amplified|amplification|overexpress(?:ing|ion))|\bher2\+|erbb2 amplification"),
    (["brca1_pathogenic", "brca2_pathogenic"], r"brca ?1/2|brca1 or brca2|(?:germline )?brca (?:mutation|mutations|mutated|mutant|variant)s?|brca-mutated"),
    (["brca1_pathogenic"], r"\bbrca1\b(?: mutation| variant| pathogenic variant)?"),
    (["brca2_pathogenic"], r"\bbrca2\b(?: mutation| variant| pathogenic variant)?"),
    (["msi_high"], r"msi[- ]h(?:igh)?\b|microsatellite instability[- ]high|microsatellite[- ]unstable|\bdmmr\b|mismatch repair[- ]deficien\w*"),
]
BIOMARKER_V2 = [
    (["egfr_exon19del", "egfr_l858r"], r"egfr[- ](?:mutant|mutated|mutations?|positive)|egfr\+|(?:sensitizing|activating) egfr mutations?|egfr[- ]sensitizing mutations?"),
]
BIOMARKER_NEG_V2 = [
    (["egfr_exon19del", "egfr_l858r"], r"egfr[- ](?:wild[- ]type|negative|wt)|negative for egfr(?: mutations?)?"),
    (["alk_fusion"], r"alk[- ]negative|negative for alk(?: rearrangement| fusion)?"),
    (["ros1_fusion"], r"ros1[- ]negative"),
    (["erbb2_amplification"], r"her2[- ]negative|her2-"),
    (["kras_g12c"], r"kras[- ](?:wild[- ]type|negative)"),
    (["braf_v600e"], r"braf[- ](?:wild[- ]type|negative)"),
    (["msi_high"], r"microsatellite[- ]stable|\bmss\b|mismatch repair[- ]proficient|\bpmmr\b"),
]

DRUG_CLASS = [
    ("egfr_tki", r"egfr[- ]?(?:tkis?|tyrosine kinase inhibitors?)"),
    ("alk_inhibitor", r"alk (?:inhibitors?|tkis?)"),
    ("kras_g12c_inhibitor", r"kras g12c inhibitors?"),
    ("braf_inhibitor", r"braf inhibitors?"),
    ("platinum_chemotherapy", r"platinum(?:[- ]based)?(?: chemotherapy| doublet| regimen)?"),
    ("taxane", r"taxanes?(?:[- ]based)?(?: chemotherapy)?"),
    ("immunotherapy", r"(?:immune )?checkpoint inhibitors?|immunotherapy|anti[- ]pd-?l?1(?: therapy| antibody| agents?)?|pd-1 inhibitors?|\bici\b"),
    ("her2_targeted", r"her2[- ]targeted therapy|anti[- ]her2(?: therapy)?"),
    ("endocrine_therapy", r"endocrine therapy|hormonal therapy|hormone therapy|aromatase inhibitors?"),
    ("parp_inhibitor", r"parp inhibitors?"),
    ("antiandrogen", r"androgen[- ]receptor inhibitors?|anti[- ]?androgens?|\barpi\b"),
]
DRUG_CLASS_V2 = [("any_chemotherapy", r"(?:cytotoxic )?chemotherapy")]
ANY_TX = r"prior (?:systemic |anticancer |anti-cancer )?(?:treatment|therapy|therapies)|previously treated|pre-?treated|treatment[- ]experienced|at least one (?:prior )?(?:line|regimen)s?(?: of (?:systemic )?(?:therapy|treatment))?|history of (?:prior )?(?:systemic )?(?:treatment|therapy)|prior lines? of (?:therapy|treatment)"
NAIVE = r"treatment[- ]na[iï]ve|therapy[- ]na[iï]ve|(?:previously )?untreated|not previously treated|no prior (?:systemic |anticancer )?(?:treatment|therapy|therapies)"
PROCEDURE = [("radiation_therapy", r"radiation(?: therapy)?|radiotherapy|stereotactic radiosurgery|\bsrs\b"),
             ("surgical_resection", r"surgical resection|resection|lobectomy|mastectomy|colectomy|(?:prior )?surgery")]

LAB_NAMES = [
    ("hba1c", r"hba1c|hemoglobin a1c|haemoglobin a1c|\ba1c\b|glycated h(?:a)?emoglobin"),
    ("hemoglobin", r"h(?:a)?emoglobin|\bhgb\b|\bhb\b"),
    ("creatinine", r"(?:serum )?creatinine"),
    ("egfr_renal", r"estimated gfr|estimated glomerular filtration rate|\begfr\b|\bgfr\b"),
    ("alt", r"\balt\b|alanine aminotransferase|\bsgpt\b"),
    ("ast", r"\bast\b|aspartate aminotransferase|\bsgot\b"),
    ("bilirubin", r"(?:total )?bilirubin"),
    ("platelets", r"platelet count|platelets?"),
    ("wbc", r"white blood cell count|white blood cells|\bwbc\b"),
    ("pdl1_tps", r"pd-?l1(?: tps| expression| tumou?r proportion score| score)?"),
]
CMP_SYM = r">=|<=|>|<|="
CMP_WORDS = (r"greater than or equal to|less than or equal to|at least|no less than|no more than|greater than|more than|"
             r"less than|at most|up to|above|over|below|under|exceeding|of at least|of at most|>=|<=|>|<|=")
WORD_OP = {"greater than or equal to": ">=", "at least": ">=", "no less than": ">=", "of at least": ">=", ">=": ">=",
           "less than or equal to": "<=", "no more than": "<=", "at most": "<=", "up to": "<=", "of at most": "<=", "<=": "<=",
           "greater than": ">", "more than": ">", "above": ">", "over": ">", "exceeding": ">", ">": ">",
           "less than": "<", "below": "<", "under": "<", "<": "<", "=": "="}
UNIT = r"mg/dl|g/dl|g/l|umol/l|mmol/mol|%|u/l|iu/l|x ?10\^?9/l|x ?10\^?3/ul|/ul|/mm3|k/ul|ml/min(?:/1\.73 ?m2)?"

UNSUPPORTED = [
    (r"life expectancy[^.;,]*", "life expectancy is not recorded in the analytical model"),
    (r"pregnan\w*|breast[- ]?feeding|lactating", "pregnancy/lactation status is not modeled"),
    (r"measurable disease[^.;,]*|recist[^.;,]*", "tumor measurability (RECIST) is not modeled"),
    (r"\d+(?:\.\d+)?\s*(?:x|×|times)\s*(?:the )?(?:uln|upper limit of normal)", "ULN-relative thresholds need lab reference ranges, which are not stored"),
    (r"informed consent", "consent is an operational, not a data, criterion"),
    (r"qtc[^.;,]*", "ECG intervals are not modeled"),
    (r"hepatitis [bc]\b[^.;,]*|active infection[^.;,]*", "infection status is not modeled"),
    (r"autoimmune disease[^.;,]*", "autoimmune history is not modeled"),
    (r"(?:organ )?transplant\w*", "transplant history is not modeled"),
    (r"(?:never|current|former|ex-)[- ]?smokers?|smoking history", "smoking status exists only as a note-derived field; not exposed as a criterion"),
    (r"progress(?:ed|ion) (?:on|after|following)", "progression events are not modeled; mapped (if possible) to treatment history only"),
    (r"within (?:the )?(?:last |past )?\d+ (?:days|weeks|months)", "time windows relative to treatment are not modeled"),
    (r"stage (?:i{1,3}v?|iv|[1-4])[abc]?\b|metastatic|advanced|unresectable|locally advanced", "disease stage is not modeled"),
]

_REENTRY_KEYS = (r"with\b|who\b|have\b|has\b|having\b|had\b|must\b|requir|aged?\b|ecog|patients\b|adequate|documented|a history|"
                 r"at least|(?:serum )?creatinine|h(?:a)?emoglobin|hba1c|platelets?|pd-?l1|bilirubin|alt\b|ast\b|wbc|e?gfr\s*(?:>|<)|"
                 r"(?:\d{1,3}\s*(?:years|or older|\+))")
INCL_REENTRY = rf"(?:,|;)?\s+(?:and|but)\s+(?=(?:{_REENTRY_KEYS}))|,\s+(?=(?:{_REENTRY_KEYS}))"
EXCL_CUE_V2 = (r"\bexclud(?:e|es|ed|ing)\b(?:\s+(?:patients|those|anyone|people|subjects))?(?:\s+(?:with|who have|who had|having))?"
               r"|\bexcept(?: for)?(?: those)?(?: with)?|\bwithout\b(?: any| a)?(?: history of| prior| known)?"
               r"|\bno\b(?! (?:more|less) than)(?: (?:prior|previous|history of|evidence of|known|active|documented))*"
               r"|\bnot (?:have|had|received|be)\b|\bnever (?:received|treated with|had)\b|\bwho (?:have|has) not\b(?: received| had)?"
               r"|\bbut not\b|\babsence of\b|\bfree of\b|\bmust not have\b|\bineligible if\b|\bnot previously treated with\b|\bnaive to\b")
EXCL_HEADER = r"\bexclusions?(?: criteria)?\s*:"
INCL_HEADER = r"\binclusions?(?: criteria)?\s*:"

STATE_RE = "|".join(sorted(map(re.escape, V.STATE_NAMES), key=len, reverse=True))


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def normalize(text: str) -> str:
    t = text.replace("–", "-").replace("—", "-").replace("≥", ">=").replace("≤", "<=").replace("×", "x").replace("µ", "u").replace("μ", "u")
    t = re.sub(r"\s+", " ", t).strip()
    return t


class RuleExtractor:
    def __init__(self, version: str = "rules-v2") -> None:
        if version not in ("rules-v1", "rules-v2"):
            raise ValueError(version)
        self.version = version
        self.v2 = version == "rules-v2"

    # -------------------------------------------------------------- mentions
    def _find(self, text: str) -> list[Mention]:
        low = text.lower()
        out: list[Mention] = []

        def add(pattern: str, kind: str, payload: dict[str, Any] | None = None, prio: int = 0, flip: bool = False,
                fn: Any = None) -> None:
            for m in re.finditer(pattern, low, FLAGS):
                p = fn(m) if fn else dict(payload or {})
                if p is not None:
                    out.append(Mention(m.start(), m.end(), kind, p, flip=flip, priority=prio))

        for key, pat in DIAGNOSIS + (DIAGNOSIS_V2 if self.v2 else []):
            add(pat, "diagnosis", {"type": "diagnosis", "condition": key})
        for markers, pat in BIOMARKER + (BIOMARKER_V2 if self.v2 else []):
            add(pat, "biomarker", {"type": "biomarker", "markers": markers, "status": "detected"}, prio=1)
        if self.v2:
            for markers, pat in BIOMARKER_NEG_V2:
                add(pat, "biomarker", {"type": "biomarker", "markers": markers, "status": "not_detected"}, prio=2)
        for key, pat in DRUG_CLASS + (DRUG_CLASS_V2 if self.v2 else []):
            add(pat, "treatment", {"type": "treatment", "drug_class": key})
        for drug in V.DRUG_TO_CLASS:
            add(rf"\b{re.escape(drug)}\b", "treatment", {"type": "treatment", "drug": drug})
        add(ANY_TX, "treatment", {"type": "treatment", "drug_class": "any_systemic_therapy"}, prio=-1)
        if self.v2:
            add(NAIVE, "treatment", {"type": "treatment", "drug_class": "any_systemic_therapy"}, prio=3, flip=True)
        for key, pat in PROCEDURE:
            add(pat, "procedure", {"type": "procedure", "procedure_class": key})
        add(r"\b(?:women|woman|females?)\b|\bpost-?menopausal\b", "sex", {"type": "sex", "sex": "female"})
        add(r"\b(?:men|man|males?)\b", "sex", {"type": "sex", "sex": "male"})
        self._ages(low, add)
        self._labs(low, add)
        if self.v2:
            add(rf"\b(?:in|from|residing in|living in|located in|resident of|residents of)\s+((?:{STATE_RE})(?:\s*(?:,|or|and|, or)\s*(?:{STATE_RE}))*)\b",
                "geography", fn=lambda m: {"type": "geography", "states": [V.STATE_NAMES[s] for s in re.findall(STATE_RE, m.group(1))]})
        return self._resolve(out)

    def _ages(self, low: str, add: Any) -> None:
        def rng(m: re.Match[str]) -> dict[str, Any] | None:
            a, b = int(m.group("a")), int(m.group("b"))
            return {"type": "age", "min_years": min(a, b), "max_years": max(a, b)} if min(a, b) >= 0 and max(a, b) <= 120 else None
        add(r"\b(?:aged?|ages?)\s*(?:between\s*)?(?P<a>\d{1,3})\s*(?:-|to|and)\s*(?P<b>\d{1,3})(?:\s*(?:years?|yrs?)(?: old| of age)?)?",
            "age", fn=rng, prio=2)
        add(r"\b(?P<a>\d{1,3})\s*(?:-|to)\s*(?P<b>\d{1,3})[- ](?:years?|yrs?)[- ]old\b", "age", fn=rng, prio=2)
        add(r"\bbetween (?:the ages of )?(?P<a>\d{1,3}) and (?P<b>\d{1,3})(?: years)?(?: of age| old)?", "age", fn=rng, prio=2)
        add(r"\b(?:aged?|ages?)\s*(?P<op>>=|>)\s*(?P<n>\d{1,3})", "age",
            fn=lambda m: {"type": "age", "min_years": int(m.group("n")) + (1 if m.group("op") == ">" else 0)}, prio=1)
        add(r"\b(?:aged?|ages?)\s*(?P<op><=|<)\s*(?P<n>\d{1,3})", "age",
            fn=lambda m: {"type": "age", "max_years": int(m.group("n")) - (1 if m.group("op") == "<" else 0)}, prio=1)
        if not self.v2:
            return
        add(r"\b(?:aged?\s+|age\s+)?(?P<n>\d{1,3})\s*(?:years?|yrs?)?(?: of age| old)?\s*(?:or|and) (?:older|over|above)\b|\b(?:aged?\s+)?(?P<n2>\d{1,3})\+",
            "age", fn=lambda m: {"type": "age", "min_years": int(m.group("n") or m.group("n2"))}, prio=1)
        add(r"\b(?:aged?\s+|age\s+)?(?P<n>\d{1,3})\s*(?:years?|yrs?)?(?: of age| old)?\s*(?:or|and) (?:younger|under|below)\b",
            "age", fn=lambda m: {"type": "age", "max_years": int(m.group("n"))}, prio=1)
        add(r"\b(?:aged?\s+|age\s+)?(?:at least|no younger than)\s*(?P<n>\d{1,3})(?:\s*(?:years?|yrs?))?", "age",
            fn=lambda m: {"type": "age", "min_years": int(m.group("n"))}, prio=1)
        add(r"\b(?:aged?\s+|age\s+)?(?:over|older than|above)\s*(?:the age of\s*)?(?P<n>\d{1,3})(?!\s*(?:%|mg|g/|u/|x|ml))", "age",
            fn=lambda m: {"type": "age", "min_years": int(m.group("n")) + 1}, prio=0)
        add(r"\b(?:aged?\s+|age\s+)?(?:under|younger than|below)\s*(?:the age of\s*)?(?P<n>\d{1,3})(?!\s*(?:%|mg|g/|u/|x|ml))", "age",
            fn=lambda m: {"type": "age", "max_years": int(m.group("n")) - 1}, prio=0)
        add(r"\b(?:aged?\s+|age\s+)?(?:up to|no older than)\s*(?P<n>\d{1,3})(?:\s*(?:years?|yrs?))", "age",
            fn=lambda m: {"type": "age", "max_years": int(m.group("n"))}, prio=1)
        add(r"\badults?\b", "age", {"type": "age", "min_years": 18}, prio=-2)
        add(r"\b(?:elderly|older adults|geriatric)\b", "age", {"type": "age", "min_years": 65}, prio=-2)

    def _labs(self, low: str, add: Any) -> None:
        cmp_re = CMP_WORDS if self.v2 else CMP_SYM
        filler = r"(?:\s+(?:levels?|counts?|values?|of|is|must be|should be|score|expression|tps))*"
        for key, name in LAB_NAMES:
            if key == "egfr_renal" and not self.v2:
                name = r"estimated gfr|\bgfr\b"

            def build(m: re.Match[str], key: str = key) -> dict[str, Any] | None:
                op = WORD_OP.get(m.group("op").strip(), m.group("op").strip())
                v1, v2 = _num(m.group("n")), m.group("n2")
                unit = (m.group("unit") or "").replace(" ", "")
                if self.v2:
                    v1 = self._convert(key, v1, unit)
                if op == "=":
                    return None
                if v2 is not None and m.group("op").strip() in ("between",):
                    return {"type": "lab", "lab": key, "op": "between", "value": v1, "value_max": self._convert(key, _num(v2), unit)}
                return {"type": "lab", "lab": key, "op": op, "value": round(v1, 2)}
            add(rf"(?:{name}){filler}\s*(?P<op>{cmp_re})\s*(?P<n>\d+(?:,\d{{3}})*(?:\.\d+)?)(?:\s*(?:-|and)\s*(?P<n2>\d+(?:\.\d+)?))?\s*(?P<unit>{UNIT})?",
                "lab", fn=build, prio=3)
            if self.v2:
                add(rf"(?:{name}){filler}\s*between\s*(?P<n>\d+(?:\.\d+)?)\s*(?:and|-)\s*(?P<n2>\d+(?:\.\d+)?)\s*(?P<unit>{UNIT})?", "lab",
                    fn=lambda m, key=key: {"type": "lab", "lab": key, "op": "between", "value": _num(m.group("n")), "value_max": _num(m.group("n2"))},
                    prio=3)

        def ecog(m: re.Match[str]) -> dict[str, Any] | None:
            g = m.groupdict()
            if g.get("a") is not None:
                a, b = sorted((int(g["a"]), int(g["b"])))
                return {"type": "lab", "lab": "ecog", "op": "<=", "value": float(b)} if a == 0 else \
                    {"type": "lab", "lab": "ecog", "op": "between", "value": float(a), "value_max": float(b)}
            if g.get("op"):
                return {"type": "lab", "lab": "ecog", "op": WORD_OP.get(g["op"], g["op"]), "value": float(g["c"])}
            if g.get("e") is not None:
                return {"type": "lab", "lab": "ecog", "op": ">=", "value": float(g["e"])}
            if g.get("d") is not None:
                return {"type": "lab", "lab": "ecog", "op": "between", "value": float(g["d"]), "value_max": float(g["d"])}
            return None
        ops = CMP_WORDS if self.v2 else CMP_SYM
        add(rf"\becog(?:\s+performance status|\s+ps|\s+score)?(?:\s+(?:of|is|must be))?\s*(?::\s*)?"
            rf"(?:(?P<a>[0-4])\s*(?:-|to|or)\s*(?P<b>[0-4])|(?P<op>{ops})\s*(?P<c>[0-4])|(?P<e>[0-4])\s*or (?:higher|greater|more|above)|(?P<d>[0-4]))",
            "lab", fn=ecog, prio=3)
        if self.v2:
            add(r"pd-?l1[- ](?:high|strongly positive)", "lab", {"type": "lab", "lab": "pdl1_tps", "op": ">=", "value": 50.0}, prio=2)
            add(r"pd-?l1[- ]positive|pd-?l1\+", "lab", {"type": "lab", "lab": "pdl1_tps", "op": ">=", "value": 1.0}, prio=2)
            add(r"pd-?l1[- ]negative", "lab", {"type": "lab", "lab": "pdl1_tps", "op": "<", "value": 1.0}, prio=2)

    @staticmethod
    def _convert(key: str, v: float, unit: str) -> float:
        if key == "hemoglobin" and unit == "g/l":
            return round(v / 10, 2)
        if key == "creatinine" and unit == "umol/l":
            return round(v / 88.42, 2)
        if key == "bilirubin" and unit == "umol/l":
            return round(v / 17.1, 2)
        if key in ("platelets", "wbc") and v >= 1000:
            return round(v / 1000, 2)
        return v

    @staticmethod
    def _resolve(ms: list[Mention]) -> list[Mention]:
        ms = sorted(ms, key=lambda m: (-m.length, -m.priority, m.start))
        taken: list[Mention] = []
        for m in ms:
            if all(m.end <= t.start or m.start >= t.end for t in taken):
                taken.append(m)
        return sorted(taken, key=lambda m: m.start)

    # -------------------------------------------------------------- polarity
    def _exclusion_scopes(self, low: str) -> list[tuple[int, int]]:
        scopes: list[tuple[int, int]] = []
        if not self.v2:
            for m in re.finditer(r"\bexclud\w*", low):
                end = re.search(r"[.;\n]", low[m.end():])
                scopes.append((m.start(), m.end() + end.start() if end else len(low)))
            return scopes
        for m in re.finditer(EXCL_HEADER, low):
            nxt = re.search(INCL_HEADER, low[m.end():])
            scopes.append((m.start(), m.end() + nxt.start() if nxt else len(low)))
        for m in re.finditer(EXCL_CUE_V2, low):
            rest = low[m.end():]
            stops = [x.start() for x in (re.search(r"[.;\n]|\bbut\b(?! not)", rest), re.search(INCL_REENTRY, rest), re.search(INCL_HEADER, rest)) if x]
            scopes.append((m.start(), m.end() + (min(stops) if stops else len(rest))))
        return scopes

    # -------------------------------------------------------------- main
    def extract(self, text: str, name: str | None = None) -> ExtractionResult:
        t0 = time.perf_counter()
        norm = normalize(text)
        low = norm.lower()
        mentions = self._find(norm)
        scopes = self._exclusion_scopes(low)
        incl: list[dict[str, Any]] = []
        excl: list[dict[str, Any]] = []
        out_mentions = []
        for m in mentions:
            in_excl = any(s <= m.start < e for s, e in scopes)
            exclusion = in_excl != m.flip
            target = excl if exclusion else incl
            out_mentions.append({"text": norm[m.start:m.end], "start": m.start, "end": m.end, "kind": m.kind,
                                 "polarity": "exclusion" if exclusion else "inclusion", "criterion": m.payload})
            target.append(dict(m.payload))
        incl, excl = _merge(incl), _merge(excl)
        unsupported = []
        if self.v2:
            covered = [(m.start, m.end) for m in mentions]
            for pat, reason in UNSUPPORTED:
                for u in re.finditer(pat, low, FLAGS):
                    if not any(a <= u.start() < b for a, b in covered) or "progress" in u.group(0):
                        unsupported.append({"text": norm[u.start():u.end()], "start": u.start(), "end": u.end(), "reason": reason})
        payload = {"name": name or (norm[:80] + ("…" if len(norm) > 80 else "")), "inclusion": incl, "exclusion": excl}
        errors: list[str] = []
        definition = None
        try:
            definition = CohortDefinition.model_validate(payload).model_dump(mode="json")
        except ValidationError as e:
            errors = [f"{'.'.join(map(str, er['loc']))}: {er['msg']}" for er in e.errors()]
        return ExtractionResult(
            version=self.version, normalized_text=norm, definition=definition, valid=definition is not None, errors=errors,
            mentions=out_mentions, unsupported=unsupported, required_fields=required_fields(incl + excl),
            latency_ms=round((time.perf_counter() - t0) * 1000, 3), usage={"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})


def _merge(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Combine split age bounds, union geography, drop exact duplicates and generic duplicates."""
    out: list[dict[str, Any]] = []
    age: dict[str, Any] = {}
    states: list[str] = []
    for c in items:
        if c["type"] == "age":
            for k in ("min_years", "max_years"):
                if k in c and c[k] is not None:
                    age.setdefault(k, c[k])
        elif c["type"] == "geography":
            states += c["states"]
        elif c not in out:
            out.append(c)
    # a specific diagnosis subsumes the generic lung_cancer mention
    conds = {c["condition"] for c in out if c["type"] == "diagnosis"}
    if conds & {"nsclc", "sclc"}:
        out = [c for c in out if not (c["type"] == "diagnosis" and c["condition"] == "lung_cancer")]
    # specific prior-therapy classes subsume the generic 'prior treatment'
    if any(c["type"] == "treatment" and c.get("drug_class") != "any_systemic_therapy" for c in out):
        out = [c for c in out if not (c["type"] == "treatment" and c.get("drug_class") == "any_systemic_therapy")]
    if age:
        out.insert(0, {"type": "age", **age})
    if states:
        out.append({"type": "geography", "states": sorted(set(states))})
    return out


FIELD_MAP = {
    "age": ["clinical.person.birth_date"], "sex": ["clinical.person.sex"], "geography": ["clinical.person.state"],
    "diagnosis": ["clinical.condition_occurrence.code", "clinical.condition_occurrence.histology_code"],
    "biomarker": ["clinical.genomic_variant.biomarker_key", "clinical.genomic_variant.result"],
    "lab": ["clinical.measurement.value_numeric", "clinical.measurement.measured_at"],
    "treatment": ["clinical.drug_exposure.drug_class", "clinical.drug_exposure.drug_name"],
    "procedure": ["clinical.procedure_occurrence.procedure_class"],
}


def required_fields(criteria: list[dict[str, Any]]) -> list[str]:
    fields: list[str] = []
    for c in criteria:
        for f in FIELD_MAP.get(c["type"], []):
            if f not in fields:
                fields.append(f)
        if c["type"] == "lab" and c.get("lab") == "ecog" and "clinical.note_fact.fact_value (ecog)" not in fields:
            fields.append("clinical.note_fact.fact_value (ecog)")
    return fields
