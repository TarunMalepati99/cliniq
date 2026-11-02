# External validation

CLINIQ's own synthetic generator and benchmarks were written by the same author as the
system, so passing them proves less than it seems. This document records tests against data
and ground truth that CLINIQ did **not** produce.

| Source | Status | What it tests |
|---|---|---|
| **Synthea** (MITRE, third-party FHIR R4) | ✅ done — results below | FHIR ingestion robustness; cohort correctness vs an independent oracle |
| **Chia** (Kury et al. 2020, annotated eligibility criteria) | ⏳ suite built and tested on fixtures; data only on figshare/Hugging Face (blocked here; the GitHub repo has code only) | criteria extraction against independent human annotation |
| **TREC Clinical Trials 2021/2022** (NIST) | ✅ done: 125 topics, 51,460 judgments with text (results in §2) | patient → trial retrieval against human relevance judgments |
| **Gemini extractor** | ⏳ adapter built and tested with a fake transport; needs your key at the terminal | LLM extraction on the same benchmarks as the rule extractors |

## 1. Synthea

**Setup.** `synthea-with-dependencies.jar`, 300 patients, seed 42, clinician seed 42, reference date
2026-01-01, state Indiana, living patients only. 295 FHIR R4 transaction bundles were exported (789 MB).

### 1.1 Ingestion: before and after

The **same 50 patients** (46,831 FHIR records) went through ingestion with the code as delivered,
and again after the fixes in §1.3.

| | Before | After |
|---|---:|---:|
| Loaded into the analytical model | 50 (Patient only) | 13,125 |
| Out of scope (valid FHIR CLINIQ does not model; counted per type) | — | 33,602 |
| Rejected to the dead-letter queue | 46,781 | 104 |

**Full run, all 295 patients.**

- **Volume.** 245,663 records in 225 s. 70,488 loaded, 174,698 out of scope, 477 rejected.
- **Loaded rows.** 295 persons, 8,737 conditions, 7,186 medication requests, 5,786 lab measurements, 35,686 procedures, 12,798 notes and 12,795 note facts.
- **Notes.** Smoking status was derived for all 295 patients.
- **Backfill.** A later `cliniq reprocess` loaded 1,327 more records after the vocabulary gained a code (§1.3).

**All 477 remaining rejections are genuine problems in the source data.**
- 372 eGFR results (LOINC 33914-3) have unit `mL/min`, but that code is defined per 1.73 m², so the unit contradicts the code.
- 97 HbA1c values fall around 2.8–3.0%, which is physiologically implausible.
- 8 ALT/AST values sit outside the plausibility window.

Every one of them is in `ops.dead_letter` with its payload.

**Out of scope, by type:** Observation codes not in the vocabulary (vital signs, surveys and so on)
80,696; DiagnosticReport 23,009; Claim 19,984; ExplanationOfBenefit 19,984; Encounter 12,798;
SupplyDelivery 6,519; Immunization 4,641; Device 1,421; Medication 1,153; MedicationAdministration 1,153;
ImagingStudy 1,033; CareTeam 870; CarePlan 870; Provenance 295; AllergyIntolerance 272.

### 1.2 Cohort correctness vs an independent oracle

`scripts/verify_synthea_cohorts.py` computes eligible patients **directly from the raw Synthea JSON**
using only the Python standard library, with no CLINIQ code. It then runs the same cohorts through
CLINIQ's full create → approve → execute workflow and compares the patient sets exactly.

| Cohort | Oracle | CLINIQ | Identical |
|---|---:|---:|:---:|
| Hypertension, age ≥ 40, no CKD | 27 | 27 | ✅ |
| Type 2 diabetes with creatinine < 1.5 mg/dL (365 days): eligible | 10 | 10 | ✅ |
| … same cohort, **potentially eligible** (creatinine unknown or conflicting) | 1 | 1 | ✅ |
| Type 2 diabetes with creatinine < 1.5 mg/dL (5-year lookback) | 10 | 10 | ✅ |
| Women with prior lisinopril | 8 | 8 | ✅ |

**Scope of this check.** It verifies ingestion, reference resolution, unit and plausibility rules,
date handling, the compiler, tri-state classification and execution against the declared semantics.
It does **not** verify that the SNOMED → ICD-10 crosswalk is clinically right: the oracle writes out
the same declared code lists independently.

### 1.3 Defects the external data exposed (all fixed, with regression tests)

1. **`urn:uuid` references were rejected.** FHIR transaction bundles reference resources by `fullUrl`. Bundle context (fullUrl → resource id, referenced Medication codings) is now captured at landing in `raw.records.context`, without altering the immutable payload.
2. **Valid but unmodeled FHIR was dead-lettered.** Out-of-scope records are now landed for lineage, marked `out_of_scope` and counted per type. They are never silently dropped, and they don't flood the dead-letter queue.
3. **SNOMED-only conditions were rejected.** A curated, documented SNOMED CT → ICD-10-CM crosswalk now applies, OMOP-style: the source vocabulary and code are kept and the standard code drives the logic. Ambiguous concepts are deliberately left unmapped (suspected cancer, carcinoma in situ, unspecified neoplasm, prediabetes).
4. **MedicationRequest, RxNorm codes, `medicationReference`, `performedPeriod` and DocumentReference notes were unsupported.** All are added now, and note facts are extracted from Synthea's note phrasing ("has never smoked").
5. **UCUM unit spellings were unrecognized,** for example `mL/min/{1.73_m2}`. They are now accepted as equivalents.
6. **Lab concepts had a single code.** Synthea reports creatinine as LOINC 38483-4 (blood), not 2160-0 (serum/plasma). Labs now carry equivalent-code sets, and `cliniq reprocess` backfills already-landed records when the vocabulary grows.
7. **Same-day conflicting lab results were resolved by row order** (an undocumented tie-break on measurement id). The oracle caught this as an 11 vs 10 mismatch. Synthea emits same-day serum and blood creatinine with different values; this happened 569 times across 8 labs. The new rule (compiler 1.1.0):
   - prefer the lab's primary code (serum over blood; structured ECOG over note-derived);
   - if results of that priority still disagree about the threshold, the evidence is **unknown (conflicting)** and the patient becomes *potentially eligible*, never silently eligible or ineligible.

   See [ADR 0007](adr/0007-conflicting-same-day-results.md).

Reproduce with:

```bash
java -jar synthea-with-dependencies.jar -p 300 -s 42 -cs 42 -r 20260101 --exporter.fhir.export true \
     --exporter.hospital.fhir.export false --exporter.practitioner.fhir.export false --generate.only_alive_patients true Indiana
createdb cliniq_synthea && CLINIQ_DATABASE_URL=postgresql://…/cliniq_synthea cliniq migrate
CLINIQ_DATABASE_URL=… cliniq ingest output/fhir
CLINIQ_DATABASE_URL=… CLINIQ_READER_DATABASE_URL=… python scripts/verify_synthea_cohorts.py output/fhir
```

## 2. TREC Clinical Trials 2021 + 2022

**Data provenance.** NIST was unreachable from the build sandbox, so this run used other sources:
- **Topics and judgments:** the copies in NCBI's TrialGPT repository (github.com/ncbi-nlp/TrialGPT, commit `1b3242c`; Jin et al., *Nature Communications* 2024). These are 75 + 50 topics and 35,832 + 35,394 judgments, which matches the official track sizes.
- **Trial text:** taken from the official 2021-04-27 corpus where available (your parts 1 and 3: 20,952 judged trials), otherwise from TrialGPT's trial records (13,725 more).
- **Coverage:** 34,677 of 48,714 judged trials have text, which is 51,460 of 71,226 judgments (72%).

`scripts/build_trec_benchmark.py` rebuilds this folder, and `MANIFEST.json` records the hashes and counts.

**Setting.** Each topic's judged pool (the trials with text) is re-ranked; topics with no eligible trial are skipped. Re-ranking a pool is easier than full-corpus retrieval, so these numbers **are not comparable with official TREC submissions**. A random ordering of the same pools gives the baseline.

| Retriever | nDCG@10 | P@10 (eligible) | MRR (eligible) | Recall@100 (eligible) |
|---|---:|---:|---:|---:|
| Random ordering (mean of 20 shuffles) | 0.254 | 0.171 | — | — |
| BM25 | 0.396 | 0.243 | 0.452 | 0.337 |
| Dense (LSA) | 0.423 | 0.262 | 0.445 | 0.407 |
| **Hybrid (BM25 + LSA, RRF)** | **0.457** | **0.288** | **0.497** | 0.377 |

**Reading the results**
- On real trials, hybrid fusion helps clearly: +0.06 nDCG@10 over BM25 and +0.20 over random. On the small synthetic corpus, the retrievers were indistinguishable.
- The absolute level is modest. CLINIQ's retriever was built for criterion → passage evidence lookup, not for matching a whole patient note to trials, and it does no criterion-level reasoning. This is the clearest signal of where an LLM matching stage would add value.
- **Defect found and fixed:** the dense embedder ran out of memory on 34,677 trials (killed at 6.3 GB). The LSA embedder now fits on a bounded, seeded sample (8,000 documents) and embeds in float32 batches. Results on the internal benchmark are unchanged (recall@5 0.756, MRR 0.940).

## 2b. Chia (pending data)

The sandboxes CLINIQ was built in cannot reach Hugging Face, figshare or NIST. Run this on a machine
with normal internet access:

```bash
# simplest: open https://doi.org/10.6084/m9.figshare.11855817.v2 in a browser → "Download all" → the zip lands in Downloads
python scripts/fetch_external.py --chia-zip ~/Downloads/<that zip> --skip-trec
# or via Hugging Face:
pip install "datasets<4"      # bigbio/chia uses a dataset script; datasets 4.x removed support
python scripts/fetch_external.py   # → ~/Downloads/cliniq_external/
cliniq eval --chia ~/Downloads/cliniq_external/chia.jsonl --trec ~/Downloads/cliniq_external/trec
```

- **Chia → CLINIQ mapping** (`backend/cliniq/evaluation/external/chia.py`):
  - The evaluation unit is one criterion line.
  - Annotated Condition, Drug, Measurement, Procedure and Person entities map to CLINIQ concepts through an alias table written separately from the extractor lexicon.
  - Polarity comes from the inclusion/exclusion section, flipped by `Has_negation` relations.
  - Biomarkers are compared at gene level.
  - Reported: concept P/R/F1, polarity accuracy, per-type scores, and **vocabulary coverage** (the share of annotated clinical entities CLINIQ can represent at all), plus the most frequent unmapped conditions.
- **TREC** (`…/external/trec.py`):
  - Each topic's judged pool is re-ranked with BM25, dense and hybrid retrieval.
  - Reported: nDCG@10 (gains 0/1/2), P@10 and MRR on eligible trials, Recall@100, and how many judgments had trial text available. Your corpus has parts 1 and 3 of 5.
  - Re-ranking a judged pool is easier than full-corpus retrieval, so the numbers are not comparable with official TREC runs.

## 3. Gemini extractor

```bash
cliniq eval --extractor rules-v2 --extractor gemini                         # internal benchmark
cliniq eval --chia …/chia.jsonl --extractor gemini --limit 300              # Chia subset (rate limits)
cliniq serve --gemini                                                       # enables "Gemini" in the UI
```

- The key is typed at a hidden `getpass` prompt, or read from the process's `GEMINI_API_KEY`. It is kept in memory only, sent in the `x-goog-api-key` header (never in the URL), and never logged or written to disk.
- The requested model is checked against the live model list for your key; if it is unavailable, you are asked to choose one.
- Output goes through the same allowlist validation, with one repair round and no silent fallback.
- **Data note:** Google's changelog says new paid-tier projects store `generateContent` requests by default. That is acceptable for public Chia text and synthetic data. **Never send PhysioNet/MIMIC or other data-use-agreement data.**
