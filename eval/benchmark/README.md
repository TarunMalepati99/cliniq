# Benchmarks

| File | Items | Built by |
|---|---|---|
| `extraction_gold.jsonl` | 125 cohort requests: 35 hand-written (`H…`) + 90 templated (`T…`) | `build_benchmark.py` (deterministic) |
| `retrieval_gold.jsonl` | 36 answerable paraphrased queries + 6 unanswerable (`U…`) | `build_benchmark.py`, relevance derived from corpus concept tags |

Each extraction item contains `request`, a gold `inclusion` / `exclusion` definition, `notes` and `review_status`.

**Review status.** `author-reviewed` means the gold was checked by the person who also wrote the
extractor. `generated` means the gold is correct by construction from template parameters. Neither
counts as independent clinical review. Flip an item to `independently-reviewed` only after a
second person has checked it.

**Labeling conventions**, shared with the extractors:
- "over X" / "older than X" → `min_years` X+1; "X or older" / "at least X" / "≥ X" → X.
- "under X" → `max_years` X−1; "X or younger" / "up to X" / "≤ X" → X.
- "older adults" → ≥ 65; "adults" → ≥ 18.
- "EGFR mutation" / "EGFR-mutant" → markers `[egfr_exon19del, egfr_l858r]`.
- "without X" → exclusion of X; "X-negative" / "X wild-type" → inclusion of X `not_detected`.
- "treatment-naive" → exclusion of `any_systemic_therapy`; "chemotherapy" (unspecified) → `any_chemotherapy`.
- "ECOG 0–1" → `ecog <= 1`.
- Lab values are expressed in canonical units (hemoglobin g/dL, creatinine mg/dL, platelets 10³/µL).
- Criteria the schema cannot represent (stage, life expectancy, RECIST, pregnancy…) are **omitted** from the gold. The extractor is expected to report them as `unsupported`.

Regenerate with `python eval/benchmark/build_benchmark.py`. Its output is deterministic, so a diff in
`benchmark_sha256` between runs means the benchmark changed.
