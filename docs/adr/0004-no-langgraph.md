# ADR 0004 — Evidence orchestration without LangGraph

**Status:** accepted

## Context
The brief suggested LangGraph "if it adds genuine value". The evidence workflow is: for each criterion,
build a query → retrieve → anchor-check → compose. This is a linear, stateless pipeline with no
branching tool use, no loops and no long-lived agent state.

## Decision
Implement it as plain, typed Python functions. Revisit if the workflow gains conditional multi-step
tool use, for example iterative query reformulation, or an LLM verifier that can request more evidence.

## Consequences
- ➕ Fewer dependencies; deterministic; easy to test and evaluate.
- ➖ If agentic retrieval is added later, orchestration code will need restructuring. Keeping the functions pure keeps that cheap.
