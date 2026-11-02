"""In-process background jobs for long-running operations (ingest, dbt, index, benchmarks).

Jobs run one at a time on a single worker thread, so pipeline operations never overlap.
Job state is in memory (lost on restart) — durable state lives in ops.pipeline_runs and
ops.eval_runs, which every job writes. A production deployment would use a real queue
(see docs/design.md, GCP path: Pub/Sub + Cloud Run jobs).
"""
from __future__ import annotations

import contextvars
import logging
import threading
import traceback
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cliniq-job")
_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def submit(kind: str, user: str, fn: Callable[[], Any]) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    job = {"job_id": job_id, "kind": kind, "status": "queued", "submitted_by": user, "submitted_at": _now(),
           "started_at": None, "finished_at": None, "result": None, "error": None}
    with _lock:
        _jobs[job_id] = job
    ctx = contextvars.copy_context()  # keep request id / user in job logs

    def run() -> None:
        job.update(status="running", started_at=_now())
        try:
            job["result"] = ctx.run(fn)
            job["status"] = "succeeded"
        except Exception as exc:  # noqa: BLE001 - surfaced to the client via job status
            log.exception("background job failed", extra={"job_id": job_id, "kind": kind})
            job.update(status="failed", error=f"{type(exc).__name__}: {exc}", trace=traceback.format_exc()[-2000:])
        job["finished_at"] = _now()

    _pool.submit(run)
    return {k: v for k, v in job.items() if k != "result"}


def get(job_id: str) -> dict[str, Any] | None:
    return _jobs.get(job_id)


def recent(limit: int = 20) -> list[dict[str, Any]]:
    with _lock:
        items = sorted(_jobs.values(), key=lambda j: j["submitted_at"], reverse=True)[:limit]
    return [{k: v for k, v in j.items() if k not in ("result", "trace")} for j in items]
