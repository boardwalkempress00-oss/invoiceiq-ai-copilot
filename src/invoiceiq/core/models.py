"""Domain models for invoices and validation results.

Monetary values are `Decimal` throughout. Pydantic serialises `Decimal` as a JSON string, so no
precision is lost between the backend and the frontend.

Invoice fields are optional on purpose: the validator must be able to *detect* a missing invoice
number or date, so a missing field cannot be a parsing error.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Vendor(_Model):
    name: str | None = None
    vat_number: str | None = None
    country: str | None = None
    email: str | None = None


class Buyer(_Model):
    name: str | None = None
    country: str | None = None
    cost_center: str | None = None


class LineItem(_Model):
    line_id: str
    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    line_total: Decimal | None = None


class Approval(_Model):
    approved_by: str | None = None
    approver_role: str | None = None
    approved_on: date | None = None


class Invoice(_Model):
    invoice_id: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    invoice_number: str | None = None
    vendor: Vendor | None = None
    buyer: Buyer | None = None
    issue_date: date | None = None
    due_date: date | None = None
    currency: str | None = None
    purchase_order: str | None = None
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: Decimal | None = None
    vat_rate: Decimal | None = None
    vat_amount: Decimal | None = None
    total: Decimal | None = None
    approval: Approval | None = None
    notes: str | None = None


class Severity(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class InvoiceStatus(StrEnum):
    PASSED = "Passed"
    FLAGGED = "Flagged"
    REQUIRES_REVIEW = "Requires Review"


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class Finding(_Model):
    finding_id: str = Field(description="Unique within one validation result, e.g. CALC-001-L2.")
    rule_id: str
    severity: Severity
    title: str
    description: str
    expected_value: str | None = None
    actual_value: str | None = None
    policy_reference: str = Field(description="Stable policy section id, e.g. PP-3.1.")
    recommended_action: str
    field: str | None = Field(default=None, description="Invoice field the finding refers to.")


class CheckResult(_Model):
    rule_id: str
    name: str
    status: CheckStatus
    policy_reference: str
    detail: str


class LineCalculation(_Model):
    line_id: str
    calculated_total: Decimal | None
    stated_total: Decimal | None


class CalculatedAmounts(_Model):
    """Amounts recalculated by the engine. `None` means the inputs were insufficient."""

    lines: list[LineCalculation]
    subtotal: Decimal | None
    vat_amount: Decimal | None
    total: Decimal | None


class ValidationResult(_Model):
    invoice_id: str
    status: InvoiceStatus
    findings: list[Finding]
    checks: list[CheckResult]
    calculated: CalculatedAmounts
    required_approval_role: str | None = Field(
        description="Approval level required by policy, or None if no approval is required."
    )
    rounding_tolerance: Decimal
    engine_version: str


class InvoiceSummary(_Model):
    invoice_id: str
    invoice_number: str | None
    vendor_name: str | None
    issue_date: date | None
    currency: str | None
    total: Decimal | None
    status: InvoiceStatus
    findings_count: int
