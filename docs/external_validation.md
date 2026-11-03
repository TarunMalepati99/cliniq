# External validation

CLINIQ's own synthetic generator and benchmarks were written by the same author as the
system, so passing them proves less than it seems. This document records tests against data
and ground truth that CLINIQ did **not** produce.

| Source | Status | What it tests |
|---|---|---|
| **Synthea** (MITRE, third-party FHIR R4) | ✅ done — results below | FHIR ingestion robustness; cohort correctness vs an independent oracle |
| **Chia** (Kury et al. 2020, annotated eligibility criteria) | ✅ done: 12,409 criterion lines, 2,534 with in-vocabulary concepts scored (§2b) | criteria extraction against independent human annotation |
| **TREC Clinical Trials 2021/2022** (NIST) | ✅ done: 125 topics, 51,460 judgments with text (results in §2) | patient → trial retrieval against human relevance judgments |
| **Gemini extractor** | ✅ done: `gemini-flash-lite-latest`, internal 125 items + Chia 150 lines (§3) | LLM extraction on the same benchmarks as the rule extractors |

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

## 2b. Chia (run on 2026-11-02, Windows, `scripts/windows/run_cliniq.ps1`)

Chia gold annotations were written by independent annotators, so these are the first extraction
numbers in this project that the author did not create the answers for.

- **Coverage first:** CLINIQ's vocabulary can represent only **9.0%** of Chia's annotated clinical
  entities (2,199 of 24,342). CLINIQ is a fixed-vocabulary system for a few oncology/cardiometabolic
  use cases, and most Chia concepts are outside it. Everything below is scored only on the 2,534
  criterion lines that contain at least one concept CLINIQ can express.

| Extractor (all 2,534 lines) | Concept P | Concept R | F1 | Polarity acc. |
|---|---|---|---|---|
| rules-v1 | 0.950 | 0.575 | 0.716 | 0.983 |
| rules-v2 | 0.924 | 0.732 | **0.817** | 0.977 |

- **rules-v2 by concept type (F1):** sex 0.99, diagnosis 0.96, procedure 0.82, age 0.73, lab 0.65,
  treatment 0.61. The weak spots are lab criteria (recall 0.49: many lab names and phrasings the lexicon
  lacks) and treatments (recall 0.45).
- **Internal vs external gap:** rules-v2 scores 96.0% exact match / 0.99 concept F1 on its own
  benchmark but 0.82 F1 on Chia, even within its vocabulary. The internal benchmark overstates real-world
  accuracy, which is the reason for running Chia.

**Reproduce**
```bash
# simplest: open https://doi.org/10.6084/m9.figshare.11855817.v2 in a browser → "Download all" → the zip lands in Downloads
python scripts/fetch_external.py --chia-zip ~/Downloads/<that zip> --skip-trec
cliniq eval --chia ~/Downloads/cliniq_external/chia.jsonl --extractor rules-v1 --extractor rules-v2
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

## 3. Gemini extractor (run on 2026-11-02, `gemini-flash-lite-latest`, free tier)

**Internal benchmark (125 requests: 35 handwritten, 90 templated)**

| Extractor | Exact match | Handwritten | Concept F1 | Polarity | Cohort P / R | Hallucination | p50 latency |
|---|---|---|---|---|---|---|---|
| rules-v1 | 24.8% | 28.6% | 0.863 | 0.909 | 0.507 / 0.758 | 0 | 1 ms |
| rules-v2 | 96.0% | 85.7% | 0.993 | 0.998 | 0.976 / 0.984 | 0 | 2 ms |
| Gemini | **98.4%** | **94.3%** | **0.995** | **1.000** | **0.991 / 0.992** | 0 | 6.1 s |

- Gemini is best on the handwritten requests (33 of 35 vs 30 of 35), which are the ones most like real
  user phrasing.
- All 125 Gemini outputs passed schema and allowlist validation.

**Chia, same 150 lines for every extractor** (`--limit 150`: the first 150 in-vocabulary lines)

| Extractor | Concept P | Concept R | F1 | Polarity acc. | No valid output |
|---|---|---|---|---|---|
| rules-v1 | 0.910 | 0.632 | 0.746 | 0.975 | 40 |
| rules-v2 | 0.890 | 0.751 | **0.815** | 0.966 | 29 |
| Gemini | 0.848 | **0.782** | **0.814** | 0.894 | **16** |

- **On independent annotations, Gemini and rules-v2 are tied on F1.** Gemini finds more concepts
  (higher recall, fewer empty outputs, much better on labs: F1 0.90). It is less precise and gets
  inclusion/exclusion polarity wrong more often (0.89 vs 0.97).
- Gemini's 2.4-point exact-match lead on the internal benchmark does not carry over to Chia.
  Reading: the LLM is better at unusual phrasing, the rules are more reliable about polarity. A
  sensible next step is a hybrid (rules first, LLM for lines the rules cannot parse); not built.
- Caveats: 150 lines is a small sample (roughly ±6 points on F1), one model, one prompt version, temperature 0.

```bash
cliniq eval --extractor rules-v2 --extractor gemini                         # internal benchmark
cliniq eval --chia …/chia.jsonl --extractor gemini --limit 150              # Chia subset (free-tier quota)
cliniq serve --gemini                                                       # enables "Gemini" in the UI
```

- The key is typed at a hidden prompt, or read from the process's `GEMINI_API_KEY`. It is kept in memory only, sent in the `x-goog-api-key` header (never in the URL), and never logged or written to disk.
- Before running, one probe request finds a model that actually has quota for the key, starting with Flash-Lite. Google's 429 details decide what happens next: a daily or zero quota stops the run, a per-minute limit waits the `retryDelay`.
- Output goes through the same allowlist validation, with one repair round and no silent fallback.
- **Data note:** Google may store `generateContent` requests depending on tier. That is acceptable for public Chia text and synthetic data. **Never send PhysioNet/MIMIC or other data-use-agreement data.**
