"""Deterministic compiler: CohortDefinition → parameterized PostgreSQL.

Design rules
- Every identifier (table, column, function) comes from templates in this file. User- or
  model-supplied values only ever appear as bind parameters.
- Each criterion compiles to a CTE yielding exactly one row per person:
      (person_id, evidence, detail)
  where evidence ∈ {'positive','negative','absent','unknown'}:
      positive  record(s) satisfy the criterion
      negative  explicit contradicting evidence (test negative, value out of range, other histology)
      absent    the patient has data in this domain but no matching record (weak evidence)
      unknown   no data in this domain for this patient (never treated as "not satisfied")
- The same definition + compiler version always yields byte-identical SQL (hash-checked).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .. import vocabulary as V
from .models import (
    AgeCriterion,
    BiomarkerCriterion,
    CohortDefinition,
    Criterion,
    DiagnosisCriterion,
    GeographyCriterion,
    LabCriterion,
    ProcedureCriterion,
    SexCriterion,
    TreatmentCriterion,
    describe,
)

COMPILER_VERSION = "compiler-1.0.0"


@dataclass
class CriterionMeta:
    cid: str
    role: str  # 'inclusion' | 'exclusion'
    type: str
    label: str
    spec: dict[str, Any]


@dataclass
class CompiledQuery:
    sql: str
    params: dict[str, Any]
    criteria: list[CriterionMeta]
    as_of_date: date
    compiler_version: str = COMPILER_VERSION

    @property
    def sha256(self) -> str:
        h = hashlib.sha256()
        h.update(self.compiler_version.encode())
        h.update(self.sql.encode())
        h.update(repr(sorted((k, str(v)) for k, v in self.params.items())).encode())
        return h.hexdigest()


class _P:
    """Bind-parameter allocator producing %(pN)s placeholders."""

    def __init__(self) -> None:
        self.params: dict[str, Any] = {}

    def __call__(self, value: Any) -> str:
        name = f"p{len(self.params)}"
        self.params[name] = value
        return f"%({name})s"


def _age(c: AgeCriterion, p: _P, asof: str) -> str:
    age = f"date_part('year', age({asof}::date, pe.birth_date))"
    conds = []
    if c.min_years is not None:
        conds.append(f"{age} >= {p(c.min_years)}")
    if c.max_years is not None:
        conds.append(f"{age} <= {p(c.max_years)}")
    return (f"SELECT pe.person_id,\n"
            f"  CASE WHEN pe.birth_date IS NULL THEN 'unknown' WHEN {' AND '.join(conds)} THEN 'positive' ELSE 'negative' END AS evidence,\n"
            f"  jsonb_build_object('age_years', {age}) AS detail\n"
            f"FROM clinical.person pe")


def _sex(c: SexCriterion, p: _P, asof: str) -> str:
    return (f"SELECT pe.person_id,\n"
            f"  CASE WHEN pe.sex IS NULL OR pe.sex = 'unknown' THEN 'unknown' WHEN pe.sex = {p(c.sex)} THEN 'positive' ELSE 'negative' END AS evidence,\n"
            f"  jsonb_build_object('sex', pe.sex) AS detail\n"
            f"FROM clinical.person pe")


def _diagnosis(c: DiagnosisCriterion, p: _P, asof: str) -> str:
    cs = V.CONDITION_SETS[c.condition]
    site = " OR ".join(f"co.code LIKE {p(prefix + '%')}" for prefix in cs.icd10_prefixes)
    if cs.histology_in:
        hist = p(list(cs.histology_in))
        pos = f"({site}) AND co.histology_code = ANY({hist})"
        unk = f"({site}) AND co.histology_code IS NULL"
        neg = f"({site}) AND co.histology_code IS NOT NULL AND NOT (co.histology_code = ANY({hist}))"
    else:
        pos, unk, neg = f"({site})", "false", "false"
    return (f"SELECT pe.person_id,\n"
            f"  CASE WHEN m.n_pos > 0 THEN 'positive' WHEN m.n_unk > 0 THEN 'unknown' WHEN m.n_neg > 0 THEN 'negative'\n"
            f"       WHEN m.n_any > 0 THEN 'absent' ELSE 'unknown' END AS evidence,\n"
            f"  jsonb_build_object('matching_conditions', m.ev, 'conditions_on_record', m.n_any,\n"
            f"                     'site_match_histology_undocumented', m.n_unk, 'site_match_other_histology', m.n_neg) AS detail\n"
            f"FROM clinical.person pe LEFT JOIN LATERAL (\n"
            f"  SELECT count(*) AS n_any,\n"
            f"         count(*) FILTER (WHERE {pos}) AS n_pos,\n"
            f"         count(*) FILTER (WHERE {unk}) AS n_unk,\n"
            f"         count(*) FILTER (WHERE {neg}) AS n_neg,\n"
            f"         jsonb_agg(jsonb_build_object('condition_id', co.condition_id, 'code', co.code, 'histology', co.histology_code,\n"
            f"                   'source_record_id', co.source_record_id)) FILTER (WHERE {site}) AS ev\n"
            f"  FROM clinical.condition_occurrence co WHERE co.person_id = pe.person_id\n"
            f"    AND (co.onset_date IS NULL OR co.onset_date <= {asof}::date)\n"
            f") m ON true")


def _biomarker(c: BiomarkerCriterion, p: _P, asof: str) -> str:
    k = p(len(c.markers))
    if c.status == "detected":
        case = f"CASE WHEN g.n_det > 0 THEN 'positive' WHEN g.n_neg = {k} THEN 'negative' ELSE 'unknown' END"
    else:
        case = f"CASE WHEN g.n_det > 0 THEN 'negative' WHEN g.n_neg = {k} THEN 'positive' ELSE 'unknown' END"
    return (f"SELECT pe.person_id,\n  {case} AS evidence,\n"
            f"  jsonb_build_object('results', g.ev, 'markers_tested', g.n_tested, 'indeterminate', g.n_ind) AS detail\n"
            f"FROM clinical.person pe LEFT JOIN LATERAL (\n"
            f"  SELECT count(*) FILTER (WHERE gv.result = 'detected') AS n_det,\n"
            f"         count(*) FILTER (WHERE gv.result = 'not_detected') AS n_neg,\n"
            f"         count(*) FILTER (WHERE gv.result = 'indeterminate') AS n_ind,\n"
            f"         count(*) AS n_tested,\n"
            f"         jsonb_agg(jsonb_build_object('variant_result_id', gv.variant_result_id, 'biomarker', gv.biomarker_key,\n"
            f"                   'result', gv.result, 'reported_date', gv.reported_date, 'source_record_id', gv.source_record_id)) AS ev\n"
            f"  FROM (SELECT DISTINCT ON (g0.biomarker_key) g0.* FROM clinical.genomic_variant g0\n"
            f"        WHERE g0.person_id = pe.person_id AND g0.biomarker_key = ANY({p(c.markers)})\n"
            f"          AND (g0.reported_date IS NULL OR g0.reported_date <= {asof}::date)\n"
            f"        ORDER BY g0.biomarker_key, g0.reported_date DESC NULLS LAST, g0.variant_result_id) gv\n"
            f") g ON true")


def _lab(c: LabCriterion, p: _P, asof: str, as_of: date) -> str:
    since = p(as_of - timedelta(days=c.lookback_days))
    if c.op == "between":
        cmp = f"l.value BETWEEN {p(c.value)} AND {p(c.value_max)}"
    else:
        cmp = f"l.value {c.op} {p(c.value)}"  # op is a validated Literal, not user text
    lab = p(c.lab)
    src = (f"SELECT m.value_numeric AS value, m.measured_at AS observed, m.measurement_id AS ref, 'measurement' AS provenance,\n"
           f"          m.source_record_id\n"
           f"   FROM clinical.measurement m WHERE m.person_id = pe.person_id AND m.lab_key = {lab}")
    if c.lab == "ecog":
        src += ("\n   UNION ALL\n"
                "   SELECT nf.fact_value::double precision, n.note_date, nf.note_id, 'note_text', n.source_record_id\n"
                "   FROM clinical.note_fact nf JOIN clinical.note n ON n.note_id = nf.note_id\n"
                "   WHERE nf.person_id = pe.person_id AND nf.fact_key = 'ecog'")
    return (f"SELECT pe.person_id,\n"
            f"  CASE WHEN l.value IS NULL THEN 'unknown' WHEN {cmp} THEN 'positive' ELSE 'negative' END AS evidence,\n"
            f"  jsonb_build_object('value', l.value, 'observed', l.observed, 'ref', l.ref, 'provenance', l.provenance,\n"
            f"                     'source_record_id', l.source_record_id) AS detail\n"
            f"FROM clinical.person pe LEFT JOIN LATERAL (\n"
            f"  SELECT s.* FROM (\n   {src}\n  ) s\n"
            f"  WHERE s.observed <= {asof}::date AND s.observed > {since}::date\n"
            f"  ORDER BY s.observed DESC, s.ref LIMIT 1\n"
            f") l ON true")


def _treatment(c: TreatmentCriterion, p: _P, asof: str) -> str:
    if c.drug:
        match = f"d.drug_name = {p(c.drug)}"
    else:
        classes = list(V.VIRTUAL_DRUG_CLASSES[c.drug_class][1]) if c.drug_class in V.VIRTUAL_DRUG_CLASSES else [c.drug_class]
        match = f"d.drug_class = ANY({p(classes)})"
    return (f"SELECT pe.person_id,\n"
            f"  CASE WHEN t.n_match > 0 THEN 'positive' WHEN t.n_any > 0 THEN 'absent' ELSE 'unknown' END AS evidence,\n"
            f"  jsonb_build_object('matching_exposures', t.ev, 'medication_records', t.n_any) AS detail\n"
            f"FROM clinical.person pe LEFT JOIN LATERAL (\n"
            f"  SELECT count(*) AS n_any, count(*) FILTER (WHERE {match}) AS n_match,\n"
            f"         jsonb_agg(jsonb_build_object('drug_exposure_id', d.drug_exposure_id, 'drug', d.drug_name, 'class', d.drug_class,\n"
            f"                   'start_date', d.start_date, 'source_record_id', d.source_record_id)) FILTER (WHERE {match}) AS ev\n"
            f"  FROM clinical.drug_exposure d WHERE d.person_id = pe.person_id AND (d.start_date IS NULL OR d.start_date <= {asof}::date)\n"
            f") t ON true")


def _procedure(c: ProcedureCriterion, p: _P, asof: str) -> str:
    cls = p(c.procedure_class)
    return (f"SELECT pe.person_id,\n"
            f"  CASE WHEN t.n_match > 0 THEN 'positive' WHEN t.n_any > 0 THEN 'absent' ELSE 'unknown' END AS evidence,\n"
            f"  jsonb_build_object('matching_procedures', t.ev, 'procedure_records', t.n_any) AS detail\n"
            f"FROM clinical.person pe LEFT JOIN LATERAL (\n"
            f"  SELECT count(*) AS n_any, count(*) FILTER (WHERE pr.procedure_class = {cls}) AS n_match,\n"
            f"         jsonb_agg(jsonb_build_object('procedure_id', pr.procedure_id, 'code', pr.code, 'date', pr.performed_date,\n"
            f"                   'source_record_id', pr.source_record_id)) FILTER (WHERE pr.procedure_class = {cls}) AS ev\n"
            f"  FROM clinical.procedure_occurrence pr WHERE pr.person_id = pe.person_id\n"
            f"    AND (pr.performed_date IS NULL OR pr.performed_date <= {asof}::date)\n"
            f") t ON true")


def _geography(c: GeographyCriterion, p: _P, asof: str) -> str:
    return (f"SELECT pe.person_id,\n"
            f"  CASE WHEN pe.state IS NULL THEN 'unknown' WHEN pe.state = ANY({p(c.states)}) THEN 'positive' ELSE 'negative' END AS evidence,\n"
            f"  jsonb_build_object('state', pe.state) AS detail\n"
            f"FROM clinical.person pe")


def compile_definition(defn: CohortDefinition, default_as_of: date) -> CompiledQuery:
    as_of = defn.as_of_date or default_as_of
    p = _P()
    asof = p(as_of)
    ctes, metas, cols, joins = [], [], [], []
    for role, prefix, items in (("inclusion", "i", defn.inclusion), ("exclusion", "e", defn.exclusion)):
        for n, c in enumerate(items, start=1):
            cid = f"{prefix}{n}"
            body = _lab(c, p, asof, as_of) if isinstance(c, LabCriterion) else _BUILDERS[type(c)](c, p, asof)
            ctes.append(f"c_{cid} AS (\n{body}\n)")
            cols.append(f"c_{cid}.evidence AS {cid}_evidence, c_{cid}.detail AS {cid}_detail")
            joins.append(f"JOIN c_{cid} ON c_{cid}.person_id = b.person_id")
            metas.append(CriterionMeta(cid, role, c.type, describe(c), c.model_dump(mode="json")))
    sql = ("-- CLINIQ " + COMPILER_VERSION + " (generated; do not edit)\n"
           "WITH " + ",\n".join(ctes) + ",\n"
           "b AS (SELECT person_id FROM clinical.person)\n"
           "SELECT b.person_id,\n  " + ",\n  ".join(cols) + "\nFROM b\n" + "\n".join(joins) + "\nORDER BY b.person_id")
    return CompiledQuery(sql=sql, params=p.params, criteria=metas, as_of_date=as_of)


_BUILDERS: dict[type, Any] = {
    AgeCriterion: _age, SexCriterion: _sex, DiagnosisCriterion: _diagnosis, BiomarkerCriterion: _biomarker,
    TreatmentCriterion: _treatment, ProcedureCriterion: _procedure, GeographyCriterion: _geography,
}


def evidence_to_status(evidence: str) -> str:
    return {"positive": "met", "unknown": "unknown"}.get(evidence, "not_met")


def classify(row: dict[str, Any], criteria: list[CriterionMeta]) -> str:
    """eligible: all inclusion met and no exclusion met or unknown.
    potentially_eligible: nothing disqualifying, but ≥1 unknown that blocks certainty.
    ineligible: any inclusion not met, or any exclusion met."""
    unknown = False
    for m in criteria:
        status = evidence_to_status(row[f"{m.cid}_evidence"])
        if m.role == "inclusion":
            if status == "not_met":
                return "ineligible"
            unknown |= status == "unknown"
        else:
            if status == "met":
                return "ineligible"
            unknown |= status == "unknown"
    return "potentially_eligible" if unknown else "eligible"


Criteria = list[Criterion]
