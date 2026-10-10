"""Structured JSON logging with a per-request correlation ID.

Logs never contain secrets, prompts or user questions — only metadata such as lengths, timings
and token counts. In Azure Container Apps, stdout is collected by Log Analytics.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")

_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": correlation_id_var.get(),
        }
        # Anything passed via `extra=` is included as structured fields.
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            # Exception type only; full tracebacks stay out of logs shipped from the public app.
            payload["exception_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Avoid duplicate, unstructured access logs; our middleware logs each request.
    logging.getLogger("uvicorn.access").disabled = True
    # The HTTP client logs full request URLs at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
