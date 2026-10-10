"""HTTP client for the InvoiceIQ FastAPI backend.

The frontend never validates invoices or talks to the AI model itself: everything goes through
this client. Each call records a real measurement (round-trip time, server time, correlation ID,
token usage) for the Observability page. Nothing here is simulated.
"""

from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

DEFAULT_API_URL = "http://127.0.0.1:8000"
MAX_MEASUREMENTS = 50
# Longer than the backend's worst-case AI duration (timeout × attempts + backoff).
CHAT_TIMEOUT_SECONDS = 75.0
DEFAULT_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class Measurement:
    timestamp: datetime
    method: str
    path: str
    status_code: int | None
    round_trip_ms: float
    server_ms: float | None
    correlation_id: str | None
    mode: str | None = None
    provider: str | None = None
    ai_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    token_source: str | None = None
    estimated_cost: str | None = None
    cost_currency: str | None = None


class ApiError(Exception):
    """A failure with a message that is safe and useful to show to users."""

    def __init__(
        self, message: str, *, status_code: int | None = None, correlation_id: str | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.correlation_id = correlation_id


class ApiClient:
    def __init__(self, base_url: str | None = None, http: httpx.Client | None = None) -> None:
        self.base_url = (base_url or os.getenv("INVOICEIQ_API_URL") or DEFAULT_API_URL).rstrip("/")
        self._http = http or httpx.Client(base_url=self.base_url)
        self.measurements: deque[Measurement] = deque(maxlen=MAX_MEASUREMENTS)
        self._cache: dict[str, Any] = {}

    # -- transport -------------------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> Any:
        started = time.perf_counter()
        try:
            response = self._http.request(method, path, json=json, timeout=timeout)
        except httpx.TimeoutException as exc:
            self._record(method, path, None, started, None)
            raise ApiError("The InvoiceIQ service took too long to respond.") from exc
        except httpx.HTTPError as exc:
            self._record(method, path, None, started, None)
            raise ApiError(
                "The InvoiceIQ backend is not reachable. If you are running locally, start it "
                "with: uv run uvicorn invoiceiq.api.main:app_factory --factory"
            ) from exc

        try:
            body = response.json()
        except ValueError:
            body = None
        self._record(method, path, response, started, body if path == "/chat" else None)

        if response.status_code >= 400:
            error = body.get("error", {}) if isinstance(body, dict) else {}
            raise ApiError(
                error.get("message") or "The request could not be completed.",
                status_code=response.status_code,
                correlation_id=error.get("correlation_id")
                or response.headers.get("X-Correlation-ID"),
            )
        return body

    def _record(
        self,
        method: str,
        path: str,
        response: httpx.Response | None,
        started: float,
        chat_body: dict[str, Any] | None,
    ) -> None:
        server_ms: float | None = None
        if response is not None:
            try:
                server_ms = float(response.headers.get("X-Response-Time-Ms", ""))
            except ValueError:
                server_ms = None
        usage = (chat_body or {}).get("usage") or {}
        self.measurements.append(
            Measurement(
                timestamp=datetime.now(UTC),
                method=method,
                path=path,
                status_code=response.status_code if response is not None else None,
                round_trip_ms=round((time.perf_counter() - started) * 1000, 1),
                server_ms=server_ms,
                correlation_id=response.headers.get("X-Correlation-ID")
                if response is not None
                else None,
                mode=(chat_body or {}).get("mode"),
                provider=(chat_body or {}).get("provider"),
                ai_ms=((chat_body or {}).get("timings") or {}).get("ai_ms"),
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                token_source=usage.get("source"),
                estimated_cost=(chat_body or {}).get("estimated_cost"),
                cost_currency=(chat_body or {}).get("cost_currency"),
            )
        )

    def _cached(self, key: str, method: str, path: str) -> Any:
        if key not in self._cache:
            self._cache[key] = self._request(method, path)
        return self._cache[key]

    def clear_cache(self) -> None:
        self._cache.clear()

    # -- endpoints -------------------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")  # type: ignore[no-any-return]

    def list_invoices(self) -> list[dict[str, Any]]:
        return self._cached("invoices", "GET", "/invoices")  # type: ignore[no-any-return]

    def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        return self._cached(f"invoice:{invoice_id}", "GET", f"/invoices/{invoice_id}")  # type: ignore[no-any-return]

    def validate(self, invoice_id: str) -> dict[str, Any]:
        return self._cached(  # type: ignore[no-any-return]
            f"validation:{invoice_id}", "POST", f"/invoices/{invoice_id}/validate"
        )

    def policy_sections(self) -> dict[str, dict[str, Any]]:
        sections = self._cached("policy", "GET", "/policy/sections")
        return {s["section_id"]: s for s in sections}

    def chat(
        self,
        *,
        invoice_id: str,
        question: str,
        session_id: str,
        history: list[dict[str, str]],
    ) -> dict[str, Any]:
        return self._request(  # type: ignore[no-any-return]
            "POST",
            "/chat",
            json={
                "invoice_id": invoice_id,
                "question": question,
                "session_id": session_id,
                "history": history,
            },
            timeout=CHAT_TIMEOUT_SECONDS,
        )
