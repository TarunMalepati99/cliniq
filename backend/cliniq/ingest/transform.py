"""Validation, standardization and mapping of source records to normalized rows.

Each mapper takes one landed raw record and returns the rows to upsert, or raises
`RecordRejected` with a stage + machine-readable error code. Mappers are pure functions
(no DB access) so they are easy to unit test; reference integrity is checked by the
pipeline, which knows which persons exist.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pydantic import ValidationError

from .. import vocabulary as V
from .fhir_models import RESOURCE_MODELS, Condition, MedicationStatement, Observation, Patient, Procedure, parse_fhir_date

TRANSFORM_VERSION = "transform-1.1.0"
NOTE_EXTRACTOR_VERSION = "notefacts-1.0.0"
LOINC_GENETIC = "69548-6"
GENETIC_RESULT = {"LA9633-4": "detected", "LA9634-2": "not_detected", "indeterminate": "indeterminate"}


class RecordRejected(Exception):
    def __init__(self, stage: str, code: str, detail: Any) -> None:
        super().__init__(f"{stage}:{code}")
        self.stage, self.code, self.detail = stage, code, detail


@dataclass
class Mapped:
    table: str
    key: str
    row: dict[str, Any]
    person_id: str
    warnings: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    children: list[Mapped] = field(default_factory=list)


def _today() -> date:
    return date.today()


def _not_future(d: date | None, what: str) -> None:
    if d and d > _today():
        raise RecordRejected("business_rule", "future_date", {"field": what, "value": d.isoformat()})


def _validate(payload: dict[str, Any]) -> Any:
    rtype = payload.get("resourceType")
    model = RESOURCE_MODELS.get(str(rtype))
    if model is None:
        raise RecordRejected("schema", "unsupported_resource_type", {"resourceType": rtype})
    try:
        return model.model_validate(payload)
    except ValidationError as e:
        errs = [{"loc": ".".join(str(x) for x in er["loc"]), "msg": er["msg"]} for er in e.errors()[:5]]
        code = "missing_required_field" if any(er["type"] == "missing" for er in e.errors()) else "invalid_field"
        raise RecordRejected("schema", code, {"errors": errs}) from None


def match_key(family: str | None, given: str | None, birth: date | None, sex: str | None) -> str | None:
    if not (family and given and birth):
        return None
    norm = f"{family.strip().lower()}|{given.strip().lower()}|{birth.isoformat()}|{sex or ''}"
    return hashlib.sha256(norm.encode()).hexdigest()


def standardize_lab(lab: V.Lab, value: float, unit: str | None) -> tuple[float, list[tuple[str, dict[str, Any]]]]:
    warnings: list[tuple[str, dict[str, Any]]] = []
    u = (unit or "").strip()
    if u == lab.unit or (not u and lab.unit in ("{score}", "%")):
        v = value
    elif u in lab.conversions:
        v = value * lab.conversions[u]
        warnings.append(("unit_converted", {"lab": lab.key, "from": u, "to": lab.unit}))
    else:
        raise RecordRejected("standardization", "unknown_unit", {"lab": lab.key, "unit": unit, "expected": [lab.unit, *lab.conversions]})
    lo, hi = lab.plausible
    if not (lo <= v <= hi):
        raise RecordRejected("business_rule", "implausible_value", {"lab": lab.key, "value": v, "unit": lab.unit, "range": [lo, hi]})
    return round(v, 4), warnings


# ------------------------------------------------------------------ FHIR mappers

def map_fhir(payload: dict[str, Any], raw_id: int, run_id: str) -> Mapped:
    res = _validate(payload)
    base = {"source_record_id": raw_id, "run_id": run_id}
    if isinstance(res, Patient):
        birth = res.birthDate
        _not_future(birth, "birthDate")
        if birth and (_today().year - birth.year) > 120:
            raise RecordRejected("business_rule", "implausible_birthdate", {"birthDate": birth.isoformat()})
        state = (res.address[0].state or "").upper() if res.address else None
        warnings = []
        if state and state not in V.US_STATES:
            warnings.append(("unknown_state", {"state": state}))
            state = None
        name = res.name[0] if res.name else None
        mk = match_key(name.family if name else None, name.given[0] if name and name.given else None, birth, res.gender)
        row = {"person_id": res.id, "birth_date": birth, "sex": res.gender or "unknown", "state": state or None,
               "deceased": res.deceasedBoolean, "match_key": mk, **base}
        if birth is None:
            warnings.append(("missing_birth_date", {}))
        return Mapped("person", res.id, row, res.id, warnings)

    if isinstance(res, Condition):
        coding = res.code.first({V.SYSTEM_URI["ICD10CM"]})
        if coding is None or not coding.code:
            raise RecordRejected("standardization", "no_icd10_coding", {"codings": [c.model_dump() for c in res.code.coding]})
        _not_future(res.onsetDateTime, "onsetDateTime")
        histology = None
        for ext in res.extension:
            if ext.url.endswith("/histology") and ext.valueCodeableConcept:
                h = ext.valueCodeableConcept.first()
                histology = h.code if h else None
        warnings = []
        if coding.code not in V.ICD10_CODES:
            warnings.append(("unmapped_condition_code", {"code": coding.code}))
        status = res.clinicalStatus.first().code if res.clinicalStatus and res.clinicalStatus.first() else None  # type: ignore[union-attr]
        row = {"condition_id": res.id, "person_id": res.subject.patient_id, "vocabulary": "ICD10CM", "code": coding.code,
               "histology_code": histology, "onset_date": res.onsetDateTime, "clinical_status": status, **base}
        return Mapped("condition_occurrence", res.id, row, res.subject.patient_id, warnings)

    if isinstance(res, Observation):
        _not_future(res.effectiveDateTime, "effectiveDateTime")
        loinc = res.code.first({"http://loinc.org"})
        if loinc and loinc.code == LOINC_GENETIC:
            return _map_genomic_obs(res, base)
        coding = res.code.first()
        vocab = V.FHIR_SYSTEMS.get(coding.system or "") if coding else None
        lab = V.LAB_BY_CODE.get((vocab, coding.code)) if coding and vocab else None  # type: ignore[arg-type]
        if lab is None:
            raise RecordRejected("standardization", "unmapped_observation_code",
                                 {"coding": coding.model_dump() if coding else None})
        if res.valueQuantity is None:
            raise RecordRejected("schema", "missing_value", {"lab": lab.key})
        value, warnings = standardize_lab(lab, res.valueQuantity.value, res.valueQuantity.unit)
        row = {"measurement_id": res.id, "person_id": res.subject.patient_id, "vocabulary": lab.vocabulary, "code": lab.code,
               "lab_key": lab.key, "value_numeric": value, "unit": lab.unit, "source_value": res.valueQuantity.value,
               "source_unit": res.valueQuantity.unit, "measured_at": res.effectiveDateTime, **base}
        return Mapped("measurement", res.id, row, res.subject.patient_id, warnings)

    if isinstance(res, MedicationStatement):
        coding = res.medicationCodeableConcept.first()
        if coding is None:
            raise RecordRejected("standardization", "no_medication_coding", {})
        name = (coding.display or "").lower().strip()
        warnings = []
        drug_class = V.DRUG_TO_CLASS.get(name)
        if drug_class is None:
            warnings.append(("unmapped_drug", {"code": coding.code, "display": coding.display}))
        p = res.effectivePeriod
        _not_future(p.start if p else None, "effectivePeriod.start")
        row = {"drug_exposure_id": res.id, "person_id": res.subject.patient_id, "vocabulary": "CLINIQ", "code": coding.code,
               "drug_name": name or coding.code, "drug_class": drug_class, "start_date": p.start if p else None,
               "end_date": p.end if p else None, "status": res.status, **base}
        return Mapped("drug_exposure", res.id, row, res.subject.patient_id, warnings)

    if isinstance(res, Procedure):
        coding = res.code.first()
        if coding is None or not coding.code:
            raise RecordRejected("standardization", "no_procedure_coding", {})
        _not_future(res.performedDateTime, "performedDateTime")
        row = {"procedure_id": res.id, "person_id": res.subject.patient_id, "vocabulary": "CLINIQ", "code": coding.code,
               "procedure_class": V.PROCEDURE_TO_CLASS.get(coding.code), "performed_date": res.performedDateTime, **base}
        return Mapped("procedure_occurrence", res.id, row, res.subject.patient_id)

    raise RecordRejected("schema", "unsupported_resource_type", {})  # pragma: no cover


def _map_genomic_obs(res: Observation, base: dict[str, Any]) -> Mapped:
    if res.valueCodeableConcept is None or res.valueCodeableConcept.first() is None:
        raise RecordRejected("schema", "missing_value", {"what": "genetic result"})
    result = GENETIC_RESULT.get(res.valueCodeableConcept.first().code or "")  # type: ignore[union-attr]
    if result is None:
        raise RecordRejected("standardization", "unknown_genetic_result", {"value": res.valueCodeableConcept.model_dump()})
    key = gene = hgvs = None
    for comp in res.component:
        cc = comp.code.first()
        if not cc or comp.valueCodeableConcept is None:
            continue
        if cc.code == "biomarker-key":
            k = comp.valueCodeableConcept.first()
            key = k.code if k else None
        elif cc.code == "48018-6":
            gene = comp.valueCodeableConcept.text
        elif cc.code == "48005-3":
            hgvs = comp.valueCodeableConcept.text
    if key not in V.BIOMARKERS:
        raise RecordRejected("standardization", "unmapped_biomarker", {"biomarker_key": key, "gene": gene})
    bm = V.BIOMARKERS[key]  # type: ignore[index]
    if gene and gene != bm.gene:
        raise RecordRejected("business_rule", "gene_biomarker_mismatch", {"gene": gene, "biomarker": key})
    row = {"variant_result_id": res.id, "person_id": res.subject.patient_id, "gene": bm.gene, "biomarker_key": bm.key,
           "hgvs_p": hgvs, "result": result, "assay": None, "reported_date": res.effectiveDateTime, **base}
    return Mapped("genomic_variant", res.id, row, res.subject.patient_id)


# ------------------------------------------------------------------ CSV / notes mappers
LOINC_TO_LAB = {lab.code: lab for lab in V.LABS.values() if lab.vocabulary == "LOINC"}
_PID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")


def _row_id(prefix: str, payload: dict[str, Any]) -> str:
    return f"{prefix}-" + hashlib.sha256(repr(sorted(payload.items())).encode()).hexdigest()[:20]


def _pid(payload: dict[str, Any]) -> str:
    pid = (payload.get("patient_id") or "").strip()
    if not _PID.match(pid):
        raise RecordRejected("schema", "missing_required_field", {"field": "patient_id"})
    return pid


def _date_field(payload: dict[str, Any], name: str) -> date:
    try:
        d = parse_fhir_date(payload.get(name))
    except (ValueError, TypeError):
        raise RecordRejected("schema", "invalid_field", {"field": name, "value": payload.get(name)}) from None
    _not_future(d, name)
    return d


def map_lab_row(payload: dict[str, Any], raw_id: int, run_id: str) -> Mapped:
    pid = _pid(payload)
    lab = LOINC_TO_LAB.get((payload.get("loinc") or "").strip())
    if lab is None:
        raise RecordRejected("standardization", "unmapped_observation_code", {"loinc": payload.get("loinc")})
    try:
        raw_value = float(payload.get("value", ""))
    except ValueError:
        raise RecordRejected("schema", "invalid_field", {"field": "value", "value": payload.get("value")}) from None
    when = _date_field(payload, "collected_date")
    value, warnings = standardize_lab(lab, raw_value, payload.get("unit"))
    mid = _row_id("lis", payload)
    row = {"measurement_id": mid, "person_id": pid, "vocabulary": lab.vocabulary, "code": lab.code, "lab_key": lab.key,
           "value_numeric": value, "unit": lab.unit, "source_value": raw_value, "source_unit": payload.get("unit"),
           "measured_at": when, "source_record_id": raw_id, "run_id": run_id}
    return Mapped("measurement", mid, row, pid, warnings)


def map_genomic_row(payload: dict[str, Any], raw_id: int, run_id: str) -> Mapped:
    pid = _pid(payload)
    key = (payload.get("biomarker") or "").strip()
    bm = V.BIOMARKERS.get(key)
    if bm is None:
        raise RecordRejected("standardization", "unmapped_biomarker", {"biomarker": key})
    result = (payload.get("result") or "").strip().lower()
    if result not in ("detected", "not_detected", "indeterminate"):
        raise RecordRejected("schema", "invalid_field", {"field": "result", "value": payload.get("result")})
    if (payload.get("gene") or bm.gene) != bm.gene:
        raise RecordRejected("business_rule", "gene_biomarker_mismatch", {"gene": payload.get("gene"), "biomarker": key})
    vid = _row_id("gx", payload)
    row = {"variant_result_id": vid, "person_id": pid, "gene": bm.gene, "biomarker_key": key, "hgvs_p": payload.get("hgvs_p") or None,
           "result": result, "assay": payload.get("assay") or None, "reported_date": _date_field(payload, "reported_date"),
           "source_record_id": raw_id, "run_id": run_id}
    return Mapped("genomic_variant", vid, row, pid)


_ECOG = re.compile(r"\bECOG(?:\s+performance\s+status|\s+PS)?\s*:?\s*([0-4])\b|\bperformance\s+status\s+ECOG\s*([0-4])\b", re.I)
_SMOKE = re.compile(r"\b(never|former|current)\s+smoker\b", re.I)


def extract_note_facts(text: str) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for m in _ECOG.finditer(text):
        facts.append({"fact_key": "ecog", "fact_value": m.group(1) or m.group(2), "span_start": m.start(), "span_end": m.end()})
    for m in _SMOKE.finditer(text):
        facts.append({"fact_key": "smoking_status", "fact_value": m.group(1).lower(), "span_start": m.start(), "span_end": m.end()})
    return facts


def map_note(payload: dict[str, Any], raw_id: int, run_id: str) -> Mapped:
    pid = _pid(payload)
    text = (payload.get("text") or "").strip()
    if not text:
        raise RecordRejected("schema", "empty_note_text", {"note_id": payload.get("note_id")})
    nid = (payload.get("note_id") or "").strip() or _row_id("note", payload)
    row = {"note_id": nid, "person_id": pid, "note_date": _date_field(payload, "note_date"), "note_type": payload.get("note_type"),
           "note_text": text, "source_record_id": raw_id, "run_id": run_id}
    children = [Mapped("note_fact", f"{nid}:{f['fact_key']}:{f['span_start']}",
                       {"note_id": nid, "person_id": pid, **f, "extractor_version": NOTE_EXTRACTOR_VERSION}, pid)
                for f in extract_note_facts(text)]
    return Mapped("note", nid, row, pid, children=children)


MAPPERS = {"fhir": map_fhir, "LabRow": map_lab_row, "GenomicRow": map_genomic_row, "Note": map_note}
