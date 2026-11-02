# Limitations and responsible use

## Intended use

CLINIQ is a **research and engineering prototype**. Its purpose is to demonstrate how clinical-data
ingestion, cohort feasibility analysis, and evaluated AI components can be engineered. It is **not**:

- a clinically validated product, medical device or decision-support tool;
- suitable for screening, enrolling or treating patients;
- approved, certified or assessed for HIPAA, GDPR, 21 CFR Part 11 or any other regulatory regime.

The same warning appears on every page of the UI, in the API description, and in `/api/health`.

## Data

- **All patient data is synthetic.** It comes from a seeded generator, and names are invented from short fictional lists. Prevalences (for example, EGFR mutation rates and testing rates) are rough approximations chosen for realism. They are **not** epidemiological estimates, and no conclusion about real populations should be drawn from cohort counts.
- **The trial documents are synthetic.** They describe hypothetical therapies and do not reflect real studies. The reference notes are short summaries written from general knowledge; they are not peer-reviewed and their figures are approximate.
- **Do not load real PHI.** The system has no de-identification, encryption-at-rest configuration, audit export, row-level security or access-review features.

## Standards conformance (what is *not* claimed)

- **FHIR R4:** only a subset of Patient, Condition, Observation, MedicationStatement and Procedure is validated. Profiles, invariants, terminology bindings, Bundle semantics (transactions, references by fullUrl) and other resource types are not supported. The genomic Observation shape follows the HL7 Genomics Reporting IG loosely and has not been validated against it.
- **OMOP CDM:** the analytical tables are *inspired by* OMOP v5.4. They are not conformant: there are no standard concept ids, Athena vocabularies, visits or observation periods. See `design.md` §3.2.
- **Vocabularies:** ICD-10-CM, LOINC and ICD-O-3 codes are a small hand-curated subset. Drug, procedure, ECOG and PD-L1 codes are **local** (`CLINIQ` vocabulary), not RxNorm, CPT or LOINC.

## Cohort logic

- Criteria are combined with AND. OR across different criterion types, temporal relations ("within 6 months of"), line-of-therapy derivation, disease stage, progression events and ULN-relative lab thresholds are **not supported**. The extractor reports these as `unsupported` instead of approximating them.
- "Prior treatment" means *any recorded exposure before the as-of date*. CLINIQ does not distinguish adjuvant from metastatic settings.
- *Absent* evidence (records exist in a domain, none match) counts as not meeting the criterion. This is a modeling choice. The UI reports how many ineligible patients rest only on absent evidence.
- The as-of date is fixed (default 2026-01-01) for reproducibility. Ages and lookback windows are computed relative to it, not to today.

## AI components

- **Extraction accuracy:** see the [evaluation report](evaluation_report.md). The templated subset overlaps with the rule lexicon and is optimistic. The hand-written subset (35 items) is small, so its scores carry wide uncertainty. Gold labels were written by the system's author and have **not** been independently reviewed by clinicians.
- **The LLM extractor was not benchmarked** in the build environment (no API key or network). It is implemented and unit-tested with a fake client only.
- **Retrieval** uses a local LSA embedder. On the current small synthetic corpus it performs about the same as BM25. No claim of semantic-retrieval quality is made.
- **Evidence composition** is extractive and template-based. It cannot summarize or reason across documents; this is deliberate, so that every statement stays traceable. An anchored citation shows that a concept is *mentioned*, not that the passage *supports* a clinical claim.

## Engineering gaps (prototype)

- **Authentication** uses static bearer tokens (SHA-256-hashed in configuration): no SSO/OIDC, expiry, rotation or rate limiting. The frontend keeps the token in `localStorage`.
- **Background jobs** run in-process on a single thread, and their state is lost on restart. Durable outcomes are still written to `ops.pipeline_runs` and `ops.eval_runs`.
- **The evidence index** is rebuilt in memory per API process, which is fine for the corpus size here.
- **Docker / Compose files were written but not executed**, because the build environment had no Docker daemon. The GitHub Actions workflow was likewise written but not run on GitHub; its steps mirror commands that were run locally.
- **`scripts/fetch_ctgov.py`** was not executed, because outbound network access was restricted during the build.
- **Performance** was measured only at about 1.2k patients and about 28k source records. Cohort SQL uses per-person lateral subqueries and has not been tested at millions of patients.
- **The UI** is light-mode only; there is no dark theme.

## Responsible-use checklist for anyone extending CLINIQ

1. Keep the "LLM never writes SQL" invariant ([ADR 0001](adr/0001-llm-never-writes-sql.md)). Add new criteria to the DSL and compiler, with tests.
2. Never convert *unknown* into *negative* to make cohort counts look cleaner.
3. Report extraction metrics per subset, and on held-out, independently reviewed requests before relying on them.
4. Before using real data: complete a privacy and security review, de-identify, add audit logging and access controls, and obtain the necessary approvals (IRB or equivalent, data-use agreements).
