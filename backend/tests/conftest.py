"""Test fixtures.

Integration tests run against a dedicated PostgreSQL database (CLINIQ_TEST_DATABASE_URL,
default postgresql://cliniq:cliniq@localhost:5432/cliniq_test). The schema is dropped and
re-migrated once per session, then the synthetic dataset (smaller, seeded) is ingested.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

TEST_DB = os.environ.get("CLINIQ_TEST_DATABASE_URL", "postgresql://cliniq:cliniq@localhost:5432/cliniq_test")
os.environ["CLINIQ_DATABASE_URL"] = TEST_DB
# Always point the read-only role at the *test* database. Using setdefault here let an inherited
# CLINIQ_READER_DATABASE_URL (as CI sets for the app DB) send cohort queries to a different,
# not-yet-built database. Override explicitly with CLINIQ_TEST_READER_DATABASE_URL if needed.
os.environ["CLINIQ_READER_DATABASE_URL"] = os.environ.get(
    "CLINIQ_TEST_READER_DATABASE_URL", TEST_DB.replace("cliniq:cliniq@", "cliniq_reader:cliniq_reader@"))
os.environ["CLINIQ_ENV"] = "dev"
logging.disable(logging.WARNING)


@pytest.fixture(scope="session")
def synth_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from cliniq.synth.generator import Generator

    out = tmp_path_factory.mktemp("synth")
    Generator(seed=7, n_patients=300, bundle_size=60).generate(out)
    return out


@pytest.fixture(scope="session")
def db(synth_dir: Path) -> dict:
    """Fresh schema + one ingestion of the seeded synthetic dataset."""
    from cliniq.config import get_settings
    from cliniq.ingest.pipeline import ingest_paths
    from cliniq.migrate import migrate, reset

    get_settings.cache_clear()
    reset(TEST_DB)
    migrate(TEST_DB)
    summary = ingest_paths([synth_dir], triggered_by="pytest")
    return summary
