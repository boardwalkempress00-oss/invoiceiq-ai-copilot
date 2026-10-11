"""Checks for submitted invoice JSON, shared by the API and the worker.

Error handling policy (one rule per kind of problem):

* Not UTF-8, not JSON, empty or larger than the limit → rejected **synchronously by the API**
  (HTTP 413/422). Nothing is stored and no event is produced.
* Valid JSON that is not a valid invoice (schema) → accepted by the API, then marked
  **Failed / invalid_schema by the worker** — final, never retried. This is the asynchronous
  failure path the demo shows.
* The worker re-checks syntax and size too (defence in depth). If that ever fails it is also a
  final `Failed`, never a retry: re-running cannot fix a bad file.

Error messages name the problem and the field, but never echo submitted values.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from invoiceiq.core.models import Invoice
from invoiceiq.processing.models import ErrorCode

MAX_SCHEMA_ERRORS = 10


@dataclass(frozen=True)
class InputProblem:
    code: ErrorCode
    message: str
    details: tuple[str, ...] = ()


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not allowed in invoice JSON")


def check_json_syntax(data: bytes, *, max_bytes: int) -> InputProblem | None:
    """Size, encoding and JSON syntax. Returns None when the payload is a JSON object."""
    if not data.strip():
        return InputProblem(ErrorCode.INVALID_JSON, "The file is empty.")
    if len(data) > max_bytes:
        return InputProblem(
            ErrorCode.TOO_LARGE, f"Invoices are limited to {max_bytes // 1024} KB of JSON."
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return InputProblem(ErrorCode.INVALID_JSON, "The file is not UTF-8 text.")
    try:
        value = json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        return InputProblem(
            ErrorCode.INVALID_JSON,
            f"The file is not valid JSON ({exc.msg} at line {exc.lineno}, column {exc.colno}).",
        )
    except (ValueError, RecursionError):
        return InputProblem(ErrorCode.INVALID_JSON, "The file is not valid JSON.")
    if not isinstance(value, dict):
        return InputProblem(ErrorCode.INVALID_JSON, "The top-level JSON value must be an object.")
    return None


def parse_invoice(data: bytes, *, max_bytes: int) -> Invoice | InputProblem:
    """Full check used by the worker: syntax first, then the invoice schema."""
    problem = check_json_syntax(data, max_bytes=max_bytes)
    if problem is not None:
        return problem
    try:
        return Invoice.model_validate_json(data)
    except ValidationError as exc:
        details = tuple(
            f"{'.'.join(str(p) for p in err.get('loc', ())) or '(root)'}: {err.get('msg', '')}"
            for err in exc.errors(include_input=False, include_url=False)[:MAX_SCHEMA_ERRORS]
        )
        return InputProblem(
            ErrorCode.INVALID_SCHEMA,
            f"The JSON is not a valid invoice ({exc.error_count()} problem(s)).",
            details,
        )


def invoice_summary(invoice: Invoice) -> dict[str, Any]:
    """The invoice details shown next to the findings (no line-level data)."""

    def text(value: object) -> str | None:
        return None if value is None else str(value)

    return {
        "invoice_id": invoice.invoice_id,
        "invoice_number": invoice.invoice_number,
        "vendor": invoice.vendor.name if invoice.vendor else None,
        "buyer": invoice.buyer.name if invoice.buyer else None,
        "issue_date": text(invoice.issue_date),
        "due_date": text(invoice.due_date),
        "currency": invoice.currency,
        "purchase_order": invoice.purchase_order,
        "subtotal": text(invoice.subtotal),
        "vat_amount": text(invoice.vat_amount),
        "total": text(invoice.total),
        "line_items": len(invoice.line_items),
    }
