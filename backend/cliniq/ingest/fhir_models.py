"""Pydantic models for the subset of FHIR R4 that CLINIQ ingests.

These models validate *structure and types* for the fields CLINIQ uses. They are not a
general FHIR validator: unknown elements are tolerated (extra='allow'), profiles and
invariants beyond those listed are not checked. See docs/limitations.md.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

_FHIR_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
_FHIR_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?$")


def parse_fhir_date(v: Any) -> date:
    """Accept FHIR date/dateTime; partial dates resolve to the first day of the period."""
    if isinstance(v, date):
        return v
    if not isinstance(v, str):
        raise ValueError("date must be a string")
    if _FHIR_DATE.match(v):
        parts = v.split("-")
        y, m, d = int(parts[0]), int(parts[1]) if len(parts) > 1 else 1, int(parts[2]) if len(parts) > 2 else 1
        return date(y, m, d)  # raises ValueError on e.g. month 13
    if _FHIR_DATETIME.match(v):
        return datetime.fromisoformat(v.replace("Z", "+00:00")).date()
    raise ValueError(f"not a FHIR date/dateTime: {v!r}")


FhirDate = Annotated[date, BeforeValidator(parse_fhir_date)]


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Coding(_Base):
    system: str | None = None
    code: str | None = None
    display: str | None = None


class CodeableConcept(_Base):
    coding: list[Coding] = Field(default_factory=list)
    text: str | None = None

    def first(self, systems: set[str] | None = None) -> Coding | None:
        for c in self.coding:
            if c.code and (systems is None or c.system in systems):
                return c
        return None


_URN_UUID = re.compile(r"^urn:uuid:[0-9a-fA-F\-]{8,64}$")


class Reference(_Base):
    """A subject reference: either a literal `Patient/<id>` or a bundle-local `urn:uuid:` that
    is resolved through the bundle's fullUrl map (stored as record context at landing)."""
    reference: str

    @field_validator("reference")
    @classmethod
    def _patient_ref(cls, v: str) -> str:
        if not (re.match(r"^Patient/[A-Za-z0-9\-.]{1,64}$", v) or _URN_UUID.match(v)):
            raise ValueError("subject must reference a Patient (Patient/<id> or a bundle urn:uuid)")
        return v

    @property
    def patient_id(self) -> str:
        if not self.reference.startswith("Patient/"):
            raise ValueError("urn:uuid reference must be resolved through bundle context")
        return self.reference.split("/", 1)[1]


class Quantity(_Base):
    value: float
    unit: str | None = None


class Period(_Base):
    start: FhirDate | None = None
    end: FhirDate | None = None


class Address(_Base):
    state: str | None = None


class HumanName(_Base):
    family: str | None = None
    given: list[str] = Field(default_factory=list)


class Extension(_Base):
    url: str
    valueCodeableConcept: CodeableConcept | None = None


class Component(_Base):
    code: CodeableConcept
    valueCodeableConcept: CodeableConcept | None = None


_ID = Annotated[str, Field(pattern=r"^[A-Za-z0-9\-.]{1,64}$")]


class Patient(_Base):
    resourceType: Literal["Patient"]
    id: _ID
    gender: Literal["male", "female", "other", "unknown"] | None = None
    birthDate: FhirDate | None = None
    address: list[Address] = Field(default_factory=list)
    name: list[HumanName] = Field(default_factory=list)
    deceasedBoolean: bool | None = None


class Condition(_Base):
    resourceType: Literal["Condition"]
    id: _ID
    subject: Reference
    code: CodeableConcept
    onsetDateTime: FhirDate | None = None
    clinicalStatus: CodeableConcept | None = None
    extension: list[Extension] = Field(default_factory=list)


class Observation(_Base):
    resourceType: Literal["Observation"]
    id: _ID
    status: str
    subject: Reference
    code: CodeableConcept
    effectiveDateTime: FhirDate
    valueQuantity: Quantity | None = None
    valueCodeableConcept: CodeableConcept | None = None
    component: list[Component] = Field(default_factory=list)


class MedicationStatement(_Base):
    resourceType: Literal["MedicationStatement"]
    id: _ID
    status: str
    subject: Reference
    medicationCodeableConcept: CodeableConcept
    effectivePeriod: Period | None = None


class Procedure(_Base):
    resourceType: Literal["Procedure"]
    id: _ID
    status: str
    subject: Reference
    code: CodeableConcept
    performedDateTime: FhirDate | None = None
    performedPeriod: Period | None = None


class MedicationRequest(_Base):
    resourceType: Literal["MedicationRequest"]
    id: _ID
    status: str
    subject: Reference
    medicationCodeableConcept: CodeableConcept | None = None
    medicationReference: Reference | dict | None = None  # resolved via bundle context
    authoredOn: FhirDate | None = None


class Attachment(_Base):
    contentType: str | None = None
    data: str | None = None


class DocContent(_Base):
    attachment: Attachment


class DocumentReference(_Base):
    resourceType: Literal["DocumentReference"]
    id: _ID
    status: str | None = None
    subject: Reference
    type: CodeableConcept | None = None
    date: FhirDate | None = None
    content: list[DocContent]


RESOURCE_MODELS: dict[str, type[_Base]] = {
    "Patient": Patient, "Condition": Condition, "Observation": Observation,
    "MedicationStatement": MedicationStatement, "Procedure": Procedure, "MedicationRequest": MedicationRequest,
    "DocumentReference": DocumentReference,
}

# Valid FHIR R4 resource types that CLINIQ deliberately does not model. Records of these types
# are landed for lineage and marked 'out_of_scope' (counted per type), not dead-lettered.
OUT_OF_SCOPE_TYPES = {
    "Encounter", "Claim", "ExplanationOfBenefit", "DiagnosticReport", "SupplyDelivery", "Immunization", "Device",
    "Medication", "MedicationAdministration", "MedicationDispense", "ImagingStudy", "CareTeam", "CarePlan", "Provenance",
    "AllergyIntolerance", "Organization", "Practitioner", "PractitionerRole", "Location", "Coverage", "Goal",
    "ServiceRequest", "Specimen", "FamilyMemberHistory", "RiskAssessment", "QuestionnaireResponse", "Composition",
    "Media", "Communication", "Appointment", "EpisodeOfCare", "RelatedPerson", "Group", "List", "Basic",
}
