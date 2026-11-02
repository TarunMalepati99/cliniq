"""Structured JSON logging with request / pipeline-run correlation IDs."""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import traceback
from datetime import UTC, datetime

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("run_id", default=None)
user_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("user", default=None)

_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
            "run_id": run_id_var.get(),
            "user": user_var.get(),
        }
        out.update({k: v for k, v in record.__dict__.items() if k not in _RESERVED})
        if record.exc_info:
            out["error_trace"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps({k: v for k, v in out.items() if v is not None}, default=str)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        return
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(JsonFormatter())
    root.handlers = [h]
    root.setLevel(level)
    for noisy in ("uvicorn.access", "httpx"):
        logging.getLogger(noisy).setLevel("WARNING")
