"""Request middleware: correlation IDs, timing, access logging and safe error responses."""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from invoiceiq.logging_config import correlation_id_var

logger = logging.getLogger("invoiceiq.access")

CORRELATION_HEADER = "X-Correlation-ID"
RESPONSE_TIME_HEADER = "X-Response-Time-Ms"
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def sanitize_correlation_id(value: str | None) -> str:
    """Accept a caller-supplied ID only if it is short and harmless; otherwise generate one."""
    if value and _SAFE_ID_RE.match(value):
        return value
    return uuid.uuid4().hex


def error_response(status: int, code: str, message: str, **extra: object) -> JSONResponse:
    body: dict[str, object] = {
        "code": code,
        "message": message,
        "correlation_id": correlation_id_var.get(),
    }
    body.update({k: v for k, v in extra.items() if v is not None})
    return JSONResponse(status_code=status, content={"error": body})


async def correlation_and_timing(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    correlation_id = sanitize_correlation_id(request.headers.get(CORRELATION_HEADER))
    token = correlation_id_var.set(correlation_id)
    started = time.perf_counter()
    try:
        try:
            response = await call_next(request)
        except Exception:
            # Never return stack traces or exception text to the client.
            logger.exception("request.unhandled_error")
            response = error_response(
                500, "internal_error", "An unexpected error occurred. Please try again later."
            )
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers[CORRELATION_HEADER] = correlation_id
        response.headers[RESPONSE_TIME_HEADER] = f"{elapsed_ms:.1f}"
        logger.info(
            "request.completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round(elapsed_ms, 1),
            },
        )
        return response
    finally:
        correlation_id_var.reset(token)
