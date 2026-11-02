"""LLM-backed extractor (optional; requires ANTHROPIC_API_KEY and the `anthropic` package).

The model never writes SQL. It is constrained to emit a CohortDefinition through a tool
whose input schema is generated from the Pydantic model; the output is then validated
against the same allowlists as every other path. One repair round is attempted with the
validation errors; if that fails the result is marked invalid — there is no silent fallback.

Cost: computed only if CLINIQ_LLM_PRICE_IN_PER_MTOK / CLINIQ_LLM_PRICE_OUT_PER_MTOK are set,
otherwise reported as null (we do not hard-code prices that may be wrong).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .. import vocabulary as V
from ..cohort.models import CohortDefinition
from ..config import get_settings
from .rules import ExtractionResult, normalize, required_fields

PROMPT_PATH = Path(__file__).with_name("prompts") / "extract_v1.md"


def prompt_text() -> str:
    allowed = {
        "conditions": sorted(V.CONDITION_SETS), "biomarkers": sorted(V.BIOMARKERS), "labs": {k: lab.unit for k, lab in V.LABS.items()},
        "drug_classes": sorted([*V.DRUG_CLASSES, *V.VIRTUAL_DRUG_CLASSES]), "drugs": sorted(V.DRUG_TO_CLASS),
        "procedure_classes": sorted(V.PROCEDURE_CLASSES),
    }
    return PROMPT_PATH.read_text().replace("{{ALLOWED_VALUES}}", json.dumps(allowed, indent=1))


def prompt_sha256() -> str:
    return hashlib.sha256(prompt_text().encode()).hexdigest()


class LLMExtractor:
    def __init__(self, client: Any | None = None, model: str | None = None) -> None:
        s = get_settings()
        self.model = model or s.llm_model
        self.version = f"llm:{self.model}:prompt-{prompt_sha256()[:8]}"
        if client is None:
            if not s.anthropic_api_key:
                raise RuntimeError("ANTHROPIC_API_KEY is not set; the LLM extractor is unavailable")
            import anthropic  # local import keeps the dependency optional
            client = anthropic.Anthropic(api_key=s.anthropic_api_key)
        self.client = client

    def _call(self, messages: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, int]]:
        tool = {"name": "emit_cohort_definition", "description": "Emit the structured cohort definition.",
                "input_schema": CohortDefinition.model_json_schema()}
        resp = self.client.messages.create(model=self.model, max_tokens=2000, system=prompt_text(), tools=[tool],
                                           tool_choice={"type": "tool", "name": "emit_cohort_definition"}, messages=messages)
        usage = {"input_tokens": getattr(resp.usage, "input_tokens", 0), "output_tokens": getattr(resp.usage, "output_tokens", 0)}
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                return dict(block.input), usage
        return None, usage

    def extract(self, text: str, name: str | None = None) -> ExtractionResult:
        t0 = time.perf_counter()
        norm = normalize(text)
        messages: list[dict[str, Any]] = [{"role": "user", "content": f"Research request:\n<request>{norm}</request>"}]
        usage_total = {"input_tokens": 0, "output_tokens": 0}
        errors: list[str] = []
        definition = None
        for _attempt in range(2):
            payload, usage = self._call(messages)
            for k in usage_total:
                usage_total[k] += usage[k]
            if payload is None:
                errors = ["model did not return a tool call"]
                break
            payload.setdefault("name", name or norm[:80])
            try:
                definition = CohortDefinition.model_validate(payload).model_dump(mode="json")
                errors = []
                break
            except ValidationError as e:
                errors = [f"{'.'.join(map(str, er['loc']))}: {er['msg']}" for er in e.errors()]
                messages += [{"role": "assistant", "content": json.dumps(payload)},
                             {"role": "user", "content": "That output failed validation:\n" + "\n".join(errors) + "\nEmit a corrected definition."}]
        pin, pout = os.environ.get("CLINIQ_LLM_PRICE_IN_PER_MTOK"), os.environ.get("CLINIQ_LLM_PRICE_OUT_PER_MTOK")
        cost = (usage_total["input_tokens"] * float(pin) + usage_total["output_tokens"] * float(pout)) / 1e6 if pin and pout else None
        crits = (definition or {}).get("inclusion", []) + (definition or {}).get("exclusion", [])
        return ExtractionResult(version=self.version, normalized_text=norm, definition=definition, valid=definition is not None,
                                errors=errors, required_fields=required_fields(crits),
                                latency_ms=round((time.perf_counter() - t0) * 1000, 1), usage={**usage_total, "cost_usd": cost})


def get_extractor(name: str) -> Any:
    from .rules import RuleExtractor
    if name in ("rules-v1", "rules-v2"):
        return RuleExtractor(name)
    if name == "llm":
        return LLMExtractor()
    if name == "gemini" or name.startswith("gemini:"):
        from .gemini import GeminiExtractor
        return GeminiExtractor(model=name.split(":", 1)[1] if ":" in name else None)
    raise ValueError(f"unknown extractor {name}")
