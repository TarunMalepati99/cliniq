"""Structured cohort definition language (the contract between extraction and SQL).

Every value is validated against the vocabulary allowlist. A CohortDefinition that passes
validation can always be compiled into safe SQL; one that fails never reaches the compiler.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .. import vocabulary as V

SCHEMA_VERSION = "1.0"


class _C(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgeCriterion(_C):
    type: Literal["age"] = "age"
    min_years: int | None = Field(default=None, ge=0, le=120)
    max_years: int | None = Field(default=None, ge=0, le=120)

    @model_validator(mode="after")
    def _range(self) -> AgeCriterion:
        if self.min_years is None and self.max_years is None:
            raise ValueError("age criterion needs min_years and/or max_years")
        if self.min_years is not None and self.max_years is not None and self.min_years > self.max_years:
            raise ValueError("min_years > max_years")
        return self


class SexCriterion(_C):
    type: Literal["sex"] = "sex"
    sex: Literal["female", "male"]


class DiagnosisCriterion(_C):
    type: Literal["diagnosis"] = "diagnosis"
    condition: str

    @field_validator("condition")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in V.CONDITION_SETS:
            raise ValueError(f"unknown condition '{v}'; allowed: {sorted(V.CONDITION_SETS)}")
        return v


class BiomarkerCriterion(_C):
    """`markers` are OR-ed: detected = any marker detected; not_detected = all markers tested negative."""
    type: Literal["biomarker"] = "biomarker"
    markers: list[str] = Field(min_length=1, max_length=6)
    status: Literal["detected", "not_detected"] = "detected"

    @field_validator("markers")
    @classmethod
    def _known(cls, v: list[str]) -> list[str]:
        bad = [m for m in v if m not in V.BIOMARKERS]
        if bad:
            raise ValueError(f"unknown biomarker(s) {bad}; allowed: {sorted(V.BIOMARKERS)}")
        return sorted(set(v))


class LabCriterion(_C):
    type: Literal["lab"] = "lab"
    lab: str
    op: Literal[">", ">=", "<", "<=", "between"]
    value: float
    value_max: float | None = None
    lookback_days: int = Field(default=365, ge=1, le=3650)

    @field_validator("lab")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in V.LABS:
            raise ValueError(f"unknown lab '{v}'; allowed: {sorted(V.LABS)}")
        return v

    @model_validator(mode="after")
    def _between(self) -> LabCriterion:
        if (self.op == "between") != (self.value_max is not None):
            raise ValueError("value_max is required for 'between' and only allowed there")
        if self.value_max is not None and self.value_max < self.value:
            raise ValueError("value_max < value")
        return self


class TreatmentCriterion(_C):
    """History of a drug class (or specific drug), any time before the as-of date."""
    type: Literal["treatment"] = "treatment"
    drug_class: str | None = None
    drug: str | None = None

    @model_validator(mode="after")
    def _one(self) -> TreatmentCriterion:
        if (self.drug_class is None) == (self.drug is None):
            raise ValueError("exactly one of drug_class or drug is required")
        if self.drug_class and self.drug_class not in V.DRUG_CLASSES and self.drug_class not in V.VIRTUAL_DRUG_CLASSES:
            raise ValueError(f"unknown drug_class '{self.drug_class}'")
        if self.drug and self.drug not in V.DRUG_TO_CLASS:
            raise ValueError(f"unknown drug '{self.drug}'")
        return self


class ProcedureCriterion(_C):
    type: Literal["procedure"] = "procedure"
    procedure_class: str

    @field_validator("procedure_class")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in V.PROCEDURE_CLASSES:
            raise ValueError(f"unknown procedure_class '{v}'")
        return v


class GeographyCriterion(_C):
    type: Literal["geography"] = "geography"
    states: list[str] = Field(min_length=1)

    @field_validator("states")
    @classmethod
    def _known(cls, v: list[str]) -> list[str]:
        v = sorted({s.upper() for s in v})
        bad = [s for s in v if s not in V.US_STATES]
        if bad:
            raise ValueError(f"unknown US state code(s) {bad}")
        return v


Criterion = Annotated[AgeCriterion | SexCriterion | DiagnosisCriterion | BiomarkerCriterion | LabCriterion | TreatmentCriterion | ProcedureCriterion | GeographyCriterion, Field(discriminator="type")]


class CohortDefinition(_C):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    name: str = Field(default="Untitled cohort", max_length=200)
    inclusion: list[Criterion] = Field(default_factory=list, max_length=20)
    exclusion: list[Criterion] = Field(default_factory=list, max_length=20)
    as_of_date: date | None = None

    @model_validator(mode="after")
    def _nonempty(self) -> CohortDefinition:
        if not self.inclusion and not self.exclusion:
            raise ValueError("a cohort definition needs at least one criterion")
        return self

    def canonical_json(self) -> str:
        """Order-insensitive within inclusion/exclusion lists; used for hashing and comparison."""
        d = self.model_dump(mode="json", exclude={"name"})
        d["inclusion"] = sorted(d["inclusion"], key=lambda c: json.dumps(c, sort_keys=True))
        d["exclusion"] = sorted(d["exclusion"], key=lambda c: json.dumps(c, sort_keys=True))
        return json.dumps(d, sort_keys=True, separators=(",", ":"))

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


def describe(c: Criterion) -> str:
    """Human-readable, deterministic label for a criterion."""
    if isinstance(c, AgeCriterion):
        if c.min_years is not None and c.max_years is not None:
            return f"Age {c.min_years}–{c.max_years} years"
        return f"Age ≥ {c.min_years} years" if c.min_years is not None else f"Age ≤ {c.max_years} years"
    if isinstance(c, SexCriterion):
        return f"Sex: {c.sex}"
    if isinstance(c, DiagnosisCriterion):
        return f"Diagnosis: {V.CONDITION_SETS[c.condition].label}"
    if isinstance(c, BiomarkerCriterion):
        names = " or ".join(V.BIOMARKERS[m].label for m in c.markers)
        return f"{names}: {'detected' if c.status == 'detected' else 'tested negative'}"
    if isinstance(c, LabCriterion):
        lab = V.LABS[c.lab]
        rng = f"between {c.value:g} and {c.value_max:g}" if c.op == "between" else f"{c.op} {c.value:g}"
        unit = "" if lab.unit in ("{score}",) else f" {lab.unit}"
        return f"{lab.label} {rng}{unit} (most recent, last {c.lookback_days} days)"
    if isinstance(c, TreatmentCriterion):
        if c.drug:
            return f"Prior treatment with {c.drug}"
        label = (V.DRUG_CLASSES.get(c.drug_class or "") or V.VIRTUAL_DRUG_CLASSES[c.drug_class or ""])[0]
        return f"Prior {label.lower()}"
    if isinstance(c, ProcedureCriterion):
        return f"Prior {V.PROCEDURE_CLASSES[c.procedure_class][0].lower()}"
    if isinstance(c, GeographyCriterion):
        return "Resides in " + ", ".join(c.states)
    raise TypeError(type(c))  # pragma: no cover


def criterion_key(c: Criterion) -> tuple[str, str]:
    """Concept identity (type, key) ignoring values — used for concept-level evaluation."""
    if isinstance(c, AgeCriterion):
        return ("age", "age")
    if isinstance(c, SexCriterion):
        return ("sex", "sex")
    if isinstance(c, DiagnosisCriterion):
        return ("diagnosis", c.condition)
    if isinstance(c, BiomarkerCriterion):
        return ("biomarker", "|".join(c.markers))
    if isinstance(c, LabCriterion):
        return ("lab", c.lab)
    if isinstance(c, TreatmentCriterion):
        return ("treatment", c.drug_class or c.drug or "")
    if isinstance(c, ProcedureCriterion):
        return ("procedure", c.procedure_class)
    if isinstance(c, GeographyCriterion):
        return ("geography", "geography")
    raise TypeError(type(c))  # pragma: no cover
