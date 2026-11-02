# ADR 0007: Conflicting same-day lab results are "unknown", not resolved by row order

**Status:** accepted (compiler 1.1.0)

## Context
Lab criteria use the most recent result in a lookback window. Compiler 1.0.0 broke ties between
results on the same date by `measurement_id`. That is deterministic but arbitrary, and it was not
documented. Synthea data contains 569 person-days where the same lab has different values on the
same date. A typical case is serum creatinine (LOINC 2160-0) of 1.84 alongside blood creatinine
(38483-4) of 1.10. An independent oracle disagreed with CLINIQ on one patient because of this.

## Decision
1. On the most recent date, prefer the lab's **primary code**: serum over blood, and structured ECOG over note-derived.
2. If several results of that priority still disagree about the threshold, the evidence is
   **unknown** (`detail.conflicting = true`). The patient becomes *potentially eligible* and the
   conflict is visible in the explanation.

## Consequences
- ➕ Ambiguity is never converted into a confident inclusion or exclusion. This is consistent with ADR 0002.
- ➕ The rule is documented, tested (`test_same_day_lab_conflict_is_unknown_and_primary_code_wins`) and replicated in the oracle.
- ➖ The compiler version changed, so definitions approved under 1.0.0 refuse to execute until they are re-created and re-approved. That is the intended behavior of hash-bound approval.
