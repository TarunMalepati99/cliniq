"""Gemini-backed criteria extractor (optional).

The API key is never read from code or config files: it comes from the GEMINI_API_KEY
environment variable of the running process, or is typed at a hidden terminal prompt
(`getpass`) and kept only in memory. It is sent in the `x-goog-api-key` header (never in
the URL, so it cannot leak into proxy or access logs) and is never logged or persisted.

Like every extractor, Gemini only emits a CohortDefinition (JSON). The output is validated
against the vocabulary allowlists; one repair round is attempted; there is no silent
fallback to another extractor. The model never writes or runs SQL.

Uses the REST endpoint directly (stdlib urllib) so no SDK version pinning is required.
"""
from __future__ import annotations

import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from ..cohort.models import CohortDefinition
from .llm import prompt_sha256, prompt_text
from .rules import ExtractionResult, normalize, required_fields

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = os.environ.get("CLINIQ_GEMINI_MODEL", "gemini-flash-lite-latest")
Transport = Callable[[str, dict[str, Any] | None, str], dict[str, Any]]


class GeminiError(RuntimeError):
    pass


class GeminiRetryable(GeminiError):
    """Per-minute rate limit (429) or transient server error (5xx): safe to retry after a pause."""

    def __init__(self, msg: str, retry_after: float | None = None) -> None:
        super().__init__(msg)
        self.retry_after = retry_after


class GeminiQuotaExhausted(GeminiError):
    """Daily quota used up, or the model has no free-tier quota (limit 0). Retrying cannot help,
    so this aborts the whole run instead of being scored as one failed item."""


FALLBACK_MODELS = ["gemini-flash-lite-latest", "gemini-2.5-flash-lite", "gemini-2.0-flash-lite", "gemini-flash-latest",
                   "gemini-2.5-flash", "gemini-2.0-flash"]


def _classify_429(detail: str) -> GeminiError:
    """Read Google's error details: QuotaFailure (which quota) and RetryInfo (how long to wait)."""
    retry_after, quota_ids, zero_limit = None, [], False
    try:
        err = json.loads(detail).get("error", {})
        for d in err.get("details", []):
            if d.get("@type", "").endswith("RetryInfo"):
                retry_after = float(str(d.get("retryDelay", "0s")).rstrip("s") or 0)
            if d.get("@type", "").endswith("QuotaFailure"):
                for v in d.get("violations", []):
                    quota_ids.append(v.get("quotaId", ""))
                    zero_limit |= str(v.get("quotaValue", "")) == "0"
        msg = err.get("message", "")[:300]
    except (ValueError, AttributeError):
        msg = detail[:300]
        zero_limit = "limit: 0" in detail
    if zero_limit or "limit: 0" in msg:
        return GeminiQuotaExhausted(f"this model has no quota for your key (limit 0): {msg}")
    if any("PerDay" in q for q in quota_ids):
        return GeminiQuotaExhausted(f"daily quota for this model is used up (resets at midnight Pacific): {msg}")
    return GeminiRetryable(f"rate limited: {msg}", retry_after)


def get_api_key(interactive: bool = True) -> str:
    """Return the key from the environment, or prompt for it with hidden input."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    if not interactive or not sys.stdin.isatty():
        raise GeminiError("GEMINI_API_KEY is not set and no interactive terminal is available to prompt for it")
    key = getpass.getpass("Gemini API key (input hidden, kept in memory only): ").strip()
    if not key:
        raise GeminiError("no Gemini API key entered")
    os.environ["GEMINI_API_KEY"] = key  # this process only; never written to disk
    return key


def _http(url: str, body: dict[str, Any] | None, key: str) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:4000]
        if e.code == 429:
            raise _classify_429(detail) from None
        if e.code >= 500:
            raise GeminiRetryable(f"Gemini API HTTP {e.code}: {detail}") from None
        raise GeminiError(f"Gemini API HTTP {e.code}: {detail[:500]}") from None
    except urllib.error.URLError as e:
        raise GeminiError(f"cannot reach Gemini API: {e.reason}") from None


def list_models(key: str, transport: Transport = _http) -> list[str]:
    out = transport(f"{API_BASE}/models?pageSize=200", None, key)
    return sorted(m["name"].removeprefix("models/") for m in out.get("models", [])
                  if "generateContent" in m.get("supportedGenerationMethods", []))


def choose_model(key: str, requested: str | None = None, interactive: bool = True, transport: Transport = _http) -> str:
    """Validate the requested model against the live model list; prompt if it is unavailable."""
    model = requested or DEFAULT_MODEL
    available = list_models(key, transport)
    if model in available or not available:
        return model
    flash = [m for m in available if "flash" in m] or available
    if not interactive or not sys.stdin.isatty():
        raise GeminiError(f"model '{model}' not available for this key; choose one of: {', '.join(flash[:15])}")
    print(f"Model '{model}' is not available for this key. Available text models:")
    for i, m in enumerate(flash[:20], 1):
        print(f"  {i:2d}. {m}")
    pick = input("Choose a number or type a model id: ").strip()
    return flash[int(pick) - 1] if pick.isdigit() and 0 < int(pick) <= len(flash[:20]) else pick


def probe(key: str, model: str, transport: Transport = _http) -> None:
    """One tiny request to confirm the model actually has quota for this key."""
    body = {"contents": [{"role": "user", "parts": [{"text": 'Reply with the JSON {"ok": true}'}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 20, "responseMimeType": "application/json"}}
    transport(f"{API_BASE}/models/{model}:generateContent", body, key)


def pick_working_model(key: str, requested: str | None = None, transport: Transport = _http) -> str:
    """Try the requested model, then cheap Flash-Lite/Flash fallbacks the key can see; return the first with quota."""
    available = set(list_models(key, transport))
    order = [m for m in [requested, *FALLBACK_MODELS] if m and (not available or m in available)]
    order += sorted(m for m in available if "flash" in m and m not in order and "image" not in m and "tts" not in m
                    and "audio" not in m and "live" not in m)
    problems = []
    for m in dict.fromkeys(order):
        try:
            probe(key, m, transport)
            return m
        except GeminiQuotaExhausted as e:
            problems.append(f"{m}: {e}")
        except GeminiRetryable:
            return m  # per-minute limit only: usable, the extractor paces itself
        except GeminiError as e:
            problems.append(f"{m}: {str(e)[:160]}")
    raise GeminiQuotaExhausted("no Gemini model with available quota for this key:\n  " + "\n  ".join(problems[:8]))


SCHEMA_HINT = (
    "\n\nReturn ONLY a JSON object (no prose, no markdown fences) that validates against this JSON Schema "
    "for CohortDefinition:\n"
)


class GeminiExtractor:
    def __init__(self, model: str | None = None, api_key: str | None = None, transport: Transport | None = None,
                 interactive: bool = True, validate_model: bool = True) -> None:
        self._key = api_key or get_api_key(interactive)
        self._transport = transport or _http
        self.model = choose_model(self._key, model, interactive, self._transport) if validate_model else (model or DEFAULT_MODEL)
        self.version = f"gemini:{self.model}:prompt-{prompt_sha256()[:8]}"
        self._system = prompt_text() + SCHEMA_HINT + json.dumps(CohortDefinition.model_json_schema())

    def __repr__(self) -> str:  # never expose the key
        return f"GeminiExtractor(model={self.model!r})"

    def _call(self, contents: list[dict[str, Any]]) -> tuple[str, dict[str, int]]:
        body = {"systemInstruction": {"parts": [{"text": self._system}]}, "contents": contents,
                "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
        # pacing + exponential backoff so free-tier rate limits are not scored as extraction failures
        time.sleep(float(os.environ.get("CLINIQ_GEMINI_DELAY_S", "0")))
        for attempt in range(6):
            try:
                out = self._transport(f"{API_BASE}/models/{self.model}:generateContent", body, self._key)
                self._consecutive_failures = 0
                break
            except GeminiRetryable as e:
                if attempt == 5:
                    self._consecutive_failures = getattr(self, "_consecutive_failures", 0) + 1
                    if self._consecutive_failures >= 3:
                        raise GeminiQuotaExhausted(f"Gemini kept refusing requests for 3 items in a row; stopping. Last error: {e}") from None
                    raise
                wait = min(65.0, max(e.retry_after or 0, 2 ** (attempt + 2)))
                print(f"  Gemini {str(e)[:140]} -- retrying in {wait:.0f}s", file=sys.stderr)
                time.sleep(wait)
        um = out.get("usageMetadata", {})
        usage = {"input_tokens": int(um.get("promptTokenCount", 0)), "output_tokens": int(um.get("candidatesTokenCount", 0))}
        try:
            text = "".join(p.get("text", "") for p in out["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError):
            reason = (out.get("promptFeedback") or {}).get("blockReason") or (out.get("candidates") or [{}])[0].get("finishReason")
            raise GeminiError(f"Gemini returned no content (reason: {reason})") from None
        return text, usage

    def extract(self, text: str, name: str | None = None) -> ExtractionResult:
        t0 = time.perf_counter()
        norm = normalize(text)
        contents: list[dict[str, Any]] = [{"role": "user", "parts": [{"text": f"Research request:\n<request>{norm}</request>"}]}]
        usage_total = {"input_tokens": 0, "output_tokens": 0}
        definition, errors = None, []
        for _attempt in range(2):
            try:
                raw, usage = self._call(contents)
            except GeminiQuotaExhausted:
                raise  # abort the run: further items would all fail the same way
            except GeminiError as e:
                errors = [str(e)]
                break
            for k in usage_total:
                usage_total[k] += usage[k]
            try:
                payload = json.loads(raw.strip().removeprefix("```json").removesuffix("```"))
                payload.setdefault("name", name or norm[:80])
                definition = CohortDefinition.model_validate(payload).model_dump(mode="json")
                errors = []
                break
            except (json.JSONDecodeError, ValidationError, AttributeError) as e:
                errors = [f"{'.'.join(map(str, er['loc']))}: {er['msg']}" for er in e.errors()] if isinstance(e, ValidationError) else [str(e)[:300]]
                contents += [{"role": "model", "parts": [{"text": raw[:4000]}]},
                             {"role": "user", "parts": [{"text": "That output failed validation:\n" + "\n".join(errors) + "\nReturn a corrected JSON object only."}]}]
        pin, pout = os.environ.get("CLINIQ_LLM_PRICE_IN_PER_MTOK"), os.environ.get("CLINIQ_LLM_PRICE_OUT_PER_MTOK")
        cost = (usage_total["input_tokens"] * float(pin) + usage_total["output_tokens"] * float(pout)) / 1e6 if pin and pout else None
        crits = (definition or {}).get("inclusion", []) + (definition or {}).get("exclusion", [])
        return ExtractionResult(version=self.version, normalized_text=norm, definition=definition, valid=definition is not None,
                                errors=errors, required_fields=required_fields(crits),
                                latency_ms=round((time.perf_counter() - t0) * 1000, 1), usage={**usage_total, "cost_usd": cost})
