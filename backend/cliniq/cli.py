"""`cliniq` command-line interface.

  cliniq migrate                         apply database migrations
  cliniq generate [--seed N --patients N]   write the synthetic dataset to data/synthetic
  cliniq ingest PATH [PATH...]           ingest files/directories
  cliniq dbt                             run dbt build (recorded as a pipeline run)
  cliniq index                           (re)build the evidence corpus + index
  cliniq bootstrap                       migrate → generate → ingest → dbt → index (the full local pipeline)
  cliniq eval [--suite S] [--extractor E]  run benchmarks and write reports
  cliniq serve                           start the API on :8000
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import REPO_ROOT, get_settings
from .observability.logging import configure_logging


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cliniq")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    g = sub.add_parser("generate")
    g.add_argument("--seed", type=int, default=42)
    g.add_argument("--patients", type=int, default=1200)
    i = sub.add_parser("ingest")
    i.add_argument("paths", nargs="+", type=Path)
    sub.add_parser("dbt")
    sub.add_parser("index")
    b = sub.add_parser("bootstrap")
    b.add_argument("--skip-dbt", action="store_true")
    e = sub.add_parser("eval")
    e.add_argument("--suite", action="append", choices=["extraction", "retrieval", "evidence"])
    e.add_argument("--extractor", action="append", choices=["rules-v1", "rules-v2", "llm"])
    s = sub.add_parser("serve")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    sub.add_parser("openapi")
    a = ap.parse_args(argv)
    configure_logging("WARNING" if a.cmd in ("eval", "bootstrap") else "INFO")
    s_ = get_settings()

    def out(x: object) -> None:
        print(json.dumps(x, indent=2, default=str))

    if a.cmd in ("migrate", "bootstrap"):
        from .migrate import migrate
        out({"migrations_applied": migrate()})
    if a.cmd in ("generate", "bootstrap"):
        from .retrieval.corpus import build_trials
        from .synth.generator import Generator
        if a.cmd == "generate" or not (s_.data_dir / "synthetic" / "manifest.json").exists():
            m = Generator(getattr(a, "seed", 42), getattr(a, "patients", 1200)).generate(s_.data_dir / "synthetic")
            out({"generated": m["files"], "injected_faults": m["injected_faults"]})
        build_trials(s_.corpus_dir / "trials")
    if a.cmd in ("ingest", "bootstrap"):
        from .ingest.pipeline import ingest_paths
        paths = a.paths if a.cmd == "ingest" else [s_.data_dir / "synthetic"]
        r = ingest_paths(paths, triggered_by="cli")
        out({k: r[k] for k in ("run_id", "status", "duration_ms")} | {"metrics": r["metrics"]})
    if a.cmd == "dbt" or (a.cmd == "bootstrap" and not a.skip_dbt):
        from .ingest.dbt_runner import run_dbt
        out(run_dbt())
    if a.cmd in ("index", "bootstrap"):
        from .retrieval.index import index_corpus
        out(index_corpus(s_.corpus_dir))
    if a.cmd == "eval":
        from .evaluation.run import run_suites
        res = run_suites(a.suite or ["extraction", "retrieval", "evidence"], a.extractor or ["rules-v1", "rules-v2"])
        out([{k: r[k] for k in ("suite", "system_version", "report_path")} | {"metrics": r["metrics"]} for r in res])
    if a.cmd == "serve":
        import uvicorn
        uvicorn.run("cliniq.api.app:app", host=a.host, port=a.port)
    if a.cmd == "openapi":
        from .api.app import openapi_to_file
        print(openapi_to_file())
    return 0


if __name__ == "__main__":
    sys.exit(main())
_ = REPO_ROOT
