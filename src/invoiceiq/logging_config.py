"""Structured JSON logging with a per-request correlation ID.

Logs never contain secrets, prompts or user questions — only metadata such as lengths, timings
and token counts. In Azure Container Apps, stdout is collected by Log Analytics.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")

_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}

# Structured fields that must never be printed, whatever their value.
_REDACTED_FIELDS = frozenset(
    {"authorization", "access_token", "api_key", "apikey", "password", "secret",
     "client_secret", "connection_string", "sas_token", "account_key"}
)  # fmt: skip

# Third-party loggers and the level they are capped at. The Azure SDK logs every HTTP request
# (URL and headers) at INFO; routine traffic is noise and adds Log Analytics volume. Warnings and
# errors (e.g. failed token acquisition, throttling) still come through.
DEFAULT_AZURE_SDK_LOG_LEVEL = "WARNING"


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
                payload[key] = "[REDACTED]" if key.lower() in _REDACTED_FIELDS else value
        if record.exc_info:
            # Exception type only; full tracebacks stay out of logs shipped from the public app.
            payload["exception_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", azure_sdk_level: str | None = None) -> None:
    """Configure JSON logs on stdout.

    `azure_sdk_level` defaults to AZURE_SDK_LOG_LEVEL, then WARNING.
    """
    azure_sdk_level = (
        azure_sdk_level or os.getenv("AZURE_SDK_LOG_LEVEL") or DEFAULT_AZURE_SDK_LOG_LEVEL
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Avoid duplicate, unstructured access logs; our middleware logs each request.
    logging.getLogger("uvicorn.access").disabled = True
    # The HTTP client logs full request URLs at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Azure SDK: per-request HTTP logging and credential-chain chatter (covers azure.core and
    # azure.identity). Our own code logs usage-limiter failures and AI timings separately.
    logging.getLogger("azure").setLevel(azure_sdk_level.upper())
    # OpenAI SDK retries are logged at INFO and are useful diagnostics: keep them. Never go below
    # INFO: at DEBUG the SDK logs full request bodies, which contain the user's question.
    root_level = logging.getLevelName(level.upper())
    openai_level = max(root_level, logging.INFO) if isinstance(root_level, int) else logging.INFO
    logging.getLogger("openai").setLevel(openai_level)
