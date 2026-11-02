# ADR 0006 — Synthetic data with an exact fault ledger; author-reviewed gold

**Status:** accepted

## Context
The system must never touch real patient data, yet ingestion and cohort logic need realistic
complexity and verifiable correctness.

## Decision
A seeded generator produces clinically coherent synthetic patients, deliberately injects faults (orphan
references, invalid dates, implausible values, duplicates, corrupt files, unit variants…), and records
an exact ledger. Tests assert the pipeline's rejections equal the ledger. The extraction gold set mixes
hand-written requests with templated ones; both are labeled `author-reviewed` / `generated` and reported
separately.

## Consequences
- ➕ Ingestion correctness is testable with exact numbers, not "looks about right".
- ➖ Synthetic distributions are simplified (no visits, no temporal treatment lines), and the gold set was written by the same author as the extractor. Both limitations are stated in the report.
