"""Run `dbt build` and record the outcome as an observable pipeline run."""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from psycopg.types.json import Jsonb

from ..config import REPO_ROOT, get_settings
from ..db import connect

DBT_DIR = REPO_ROOT / "dbt"


def _dbt_env() -> dict[str, str]:
    u = urlparse(get_settings().database_url)
    env = dict(os.environ)
    env.update(CLINIQ_PGHOST=u.hostname or "localhost", CLINIQ_PGPORT=str(u.port or 5432), CLINIQ_PGUSER=u.username or "",
               CLINIQ_PGPASSWORD=u.password or "", CLINIQ_PGDATABASE=(u.path or "/").lstrip("/"))
    return env


def run_dbt(triggered_by: str = "cli", dbt_dir: Path = DBT_DIR) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    with connect() as conn:
        conn.execute("INSERT INTO ops.pipeline_runs (run_id, pipeline, status, triggered_by) VALUES (%s,'dbt','running',%s)", (run_id, triggered_by))
    proc = subprocess.run(["dbt", "build", "--profiles-dir", str(dbt_dir), "--project-dir", str(dbt_dir),
                           "--vars", json.dumps({"as_of_date": get_settings().as_of_date.isoformat()})],
                          capture_output=True, text=True, env=_dbt_env(), timeout=900)
    results_path = dbt_dir / "target" / "run_results.json"
    metrics: dict[str, Any] = {"exit_code": proc.returncode}
    failures: list[dict[str, Any]] = []
    if results_path.exists():
        rr = json.loads(results_path.read_text())
        statuses: dict[str, int] = {}
        for r in rr.get("results", []):
            statuses[r["status"]] = statuses.get(r["status"], 0) + 1
            if r["status"] in ("fail", "error"):
                failures.append({"node": r["unique_id"], "status": r["status"], "message": (r.get("message") or "")[:300]})
        metrics.update(statuses=statuses, elapsed_s=rr.get("elapsed_time"), failures=failures)
    status = "succeeded" if proc.returncode == 0 else "failed"
    err = None if status == "succeeded" else (proc.stdout[-1500:] + proc.stderr[-500:])
    with connect() as conn:
        conn.execute("UPDATE ops.pipeline_runs SET status=%s, finished_at=now(), duration_ms=(extract(epoch from now()-started_at)*1000)::int,"
                     " metrics=%s, error=%s WHERE run_id=%s", (status, Jsonb(metrics), err, run_id))
    return {"run_id": run_id, "status": status, "metrics": metrics}
