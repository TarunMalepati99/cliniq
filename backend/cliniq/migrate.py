"""Minimal forward-only migration runner.

Applies backend/migrations/NNN_*.sql in order inside a transaction each, recording
applied versions and checksums in public.schema_migrations. A changed checksum for an
already-applied migration is an error (migrations are immutable once applied).
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

from psycopg import sql as pgsql

from .db import connect

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def migrate(url: str | None = None) -> list[str]:
    applied: list[str] = []
    with connect(url) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS public.schema_migrations ("
            " version text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r["version"]: r["checksum"] for r in conn.execute("SELECT version, checksum FROM public.schema_migrations")}
        conn.commit()
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            sql = path.read_text()
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            version = path.stem
            if version in done:
                if done[version] != checksum:
                    raise RuntimeError(f"migration {version} was modified after being applied")
                continue
            with conn.transaction():
                conn.execute(sql)  # type: ignore[arg-type]
                conn.execute("INSERT INTO public.schema_migrations (version, checksum) VALUES (%s, %s)", (version, checksum))
            applied.append(version)
        # The read-only role's password never lives in SQL files: set it from the environment
        # (a non-secret default is only used in dev).
        pw = os.environ.get("CLINIQ_READER_PASSWORD") or ("cliniq_reader" if os.environ.get("CLINIQ_ENV", "dev") == "dev" else None)
        if pw:
            conn.execute(pgsql.SQL("ALTER ROLE cliniq_reader PASSWORD {}").format(pgsql.Literal(pw)))
    return applied


def reset(url: str | None = None) -> None:
    """Drop all CLINIQ schemas. Used by tests and `cliniq reset` (dev only)."""
    with connect(url) as conn:
        conn.execute("DROP SCHEMA IF EXISTS rag, cohort, clinical, raw, ops CASCADE")
        conn.execute("DROP TABLE IF EXISTS public.schema_migrations")


if __name__ == "__main__":
    print("applied:", migrate(sys.argv[1] if len(sys.argv) > 1 else None))
