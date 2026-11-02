# ADR 0005 — Local LSA embeddings as the default dense retriever

**Status:** accepted (revisit with a real corpus)

## Context
The build environment could not download neural embedding models, and the project must run locally
without paid services.

## Decision
Use LSA (TF-IDF over word 1–2-grams plus char 3–5-grams, then truncated SVD to 128 dimensions) behind an
`Embedder` interface, fused with BM25 through reciprocal rank fusion. A sentence-transformers embedder
is available behind `CLINIQ_EMBEDDER`, but it is not exercised in CI.

## Consequences
- ➕ Fully local and deterministic; the index rebuilds in under a second.
- ➖ It is only a modestly "semantic" retriever. On the current benchmark it performs about the same as BM25 (see the evaluation report), so we do not claim a semantic-retrieval advantage. A neural embedder should be benchmarked on a real corpus before any such claim.
