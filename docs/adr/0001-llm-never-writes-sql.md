# ADR 0001 — Language models never write or execute SQL

**Status:** accepted

## Context
Text-to-SQL is the obvious shortcut for a cohort-query product. But generated SQL can reference arbitrary
tables, inline hallucinated codes, return plausible-looking wrong cohorts, or, through prompt injection,
attempt writes. Reviewers cannot reliably audit free-form SQL at scale.

## Decision
Extractors (rules or LLM) produce a `CohortDefinition` from a closed vocabulary. A deterministic compiler
turns that definition into SQL from fixed templates, with every value passed as a bind parameter. A static
guard (`sqlglot` AST) and `EXPLAIN` under a SELECT-only role then validate the artifact. Humans approve
the SQL hash, and execution re-verifies it.

## Consequences
- ➕ The attack surface shrinks to the DSL; injection tests are simple and exhaustive per field.
- ➕ Results are reproducible: the same definition and compiler version always produce byte-identical SQL.
- ➕ Explanations come for free, because each criterion is a named CTE.
- ➖ Expressiveness is limited to what the DSL supports. Unsupported criteria are surfaced as `unsupported` rather than approximated.
- ➖ New criterion types require compiler work and tests, not prompt changes.
