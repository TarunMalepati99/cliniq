"""Environment-based configuration.

All settings come from environment variables (optionally a `.env` file loaded by the
process manager). Nothing secret has a hard-coded production default: the dev tokens
below are only used when CLINIQ_ENV=dev and are documented as non-secret.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    env: str
    database_url: str
    reader_database_url: str
    # Fixed analytical "as of" date so age/lookback windows are reproducible.
    as_of_date: date
    statement_timeout_ms: int
    require_distinct_approver: bool
    anthropic_api_key: str | None
    llm_model: str
    data_dir: Path
    corpus_dir: Path
    eval_dir: Path
    # token sha256 -> (user, role)
    api_tokens: dict[str, tuple[str, str]] = field(default_factory=dict)

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"


DEV_TOKENS = {
    # Non-secret development tokens, only honored when CLINIQ_ENV=dev.
    "dev-admin-token": ("admin@cliniq.local", "admin"),
    "dev-researcher-token": ("researcher@cliniq.local", "researcher"),
    "dev-reviewer-token": ("reviewer@cliniq.local", "researcher"),
    "dev-viewer-token": ("viewer@cliniq.local", "viewer"),
}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _parse_tokens(raw: str | None) -> dict[str, tuple[str, str]]:
    """CLINIQ_API_TOKENS format: 'sha256hex:user:role,sha256hex:user:role'."""
    out: dict[str, tuple[str, str]] = {}
    if not raw:
        return out
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        digest, user, role = item.split(":", 2)
        if role not in {"admin", "researcher", "viewer"}:
            raise ValueError(f"invalid role in CLINIQ_API_TOKENS: {role}")
        out[digest] = (user, role)
    return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    env = _env("CLINIQ_ENV", "dev") or "dev"
    db = _env("CLINIQ_DATABASE_URL", "postgresql://cliniq:cliniq@localhost:5432/cliniq")
    reader = _env("CLINIQ_READER_DATABASE_URL", db)
    tokens = _parse_tokens(_env("CLINIQ_API_TOKENS"))
    if env == "dev":
        for tok, (user, role) in DEV_TOKENS.items():
            tokens.setdefault(hash_token(tok), (user, role))
    if not tokens:
        raise RuntimeError("No API tokens configured; set CLINIQ_API_TOKENS outside dev.")
    return Settings(
        env=env,
        database_url=db,  # type: ignore[arg-type]
        reader_database_url=reader,  # type: ignore[arg-type]
        as_of_date=date.fromisoformat(_env("CLINIQ_AS_OF_DATE", "2026-01-01")),  # type: ignore[arg-type]
        statement_timeout_ms=int(_env("CLINIQ_STATEMENT_TIMEOUT_MS", "15000")),  # type: ignore[arg-type]
        require_distinct_approver=_env("CLINIQ_REQUIRE_DISTINCT_APPROVER", "false") == "true",
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        llm_model=_env("CLINIQ_LLM_MODEL", "claude-sonnet-5-5") or "claude-sonnet-5-5",
        data_dir=Path(_env("CLINIQ_DATA_DIR", str(REPO_ROOT / "data"))),  # type: ignore[arg-type]
        corpus_dir=Path(_env("CLINIQ_CORPUS_DIR", str(REPO_ROOT / "data" / "corpus"))),  # type: ignore[arg-type]
        eval_dir=Path(_env("CLINIQ_EVAL_DIR", str(REPO_ROOT / "eval"))),  # type: ignore[arg-type]
        api_tokens=tokens,
    )
