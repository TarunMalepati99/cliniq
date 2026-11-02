"""Database access helpers (psycopg 3) with retry for transient failures."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

import psycopg
from psycopg.rows import dict_row

from .config import get_settings

log = logging.getLogger(__name__)
T = TypeVar("T")

TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    psycopg.OperationalError,
    psycopg.errors.SerializationFailure,
    psycopg.errors.DeadlockDetected,
)


def retry(fn: Callable[[], T], *, attempts: int = 3, base_delay: float = 0.2, what: str = "db op") -> T:
    """Run fn with exponential backoff on transient DB errors. Non-transient errors propagate."""
    last: BaseException | None = None
    for i in range(attempts):
        try:
            return fn()
        except TRANSIENT_ERRORS as exc:  # pragma: no cover - exercised in tests via fake
            last = exc
            delay = base_delay * (2**i)
            log.warning("transient error, retrying", extra={"what": what, "attempt": i + 1, "delay_s": delay, "error": str(exc)})
            time.sleep(delay)
    assert last is not None
    raise last


@contextmanager
def connect(url: str | None = None, *, autocommit: bool = False) -> Iterator[psycopg.Connection[dict[str, Any]]]:
    conn = psycopg.connect(url or get_settings().database_url, row_factory=dict_row, autocommit=autocommit)
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except BaseException:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def read_only(url: str | None = None, *, timeout_ms: int | None = None) -> Iterator[psycopg.Connection[dict[str, Any]]]:
    """Connection for executing compiled cohort SQL: read-only transaction + statement timeout."""
    s = get_settings()
    conn = psycopg.connect(url or s.reader_database_url, row_factory=dict_row)
    try:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute(f"SET LOCAL statement_timeout = {int(timeout_ms or s.statement_timeout_ms)}")
        yield conn
    finally:
        conn.rollback()
        conn.close()


def fetch_all(sql: str, params: Any = None) -> list[dict[str, Any]]:
    with connect() as c:
        return list(c.execute(sql, params).fetchall())


def fetch_one(sql: str, params: Any = None) -> dict[str, Any] | None:
    with connect() as c:
        return c.execute(sql, params).fetchone()
