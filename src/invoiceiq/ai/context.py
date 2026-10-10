"""The evidence given to the AI model, built entirely by Python from verified data.

Every evidence item has a stable ID. The model may only cite these IDs; anything else is removed
from its answer (see `ai_service.enforce_citations`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from invoiceiq.core.models import Invoice, ValidationResult
from invoiceiq.core.policy_retriever import PolicySection


class EvidenceKind(StrEnum):
    INVOICE = "invoice"
    VALIDATION = "validation"
    FINDING = "finding"
    POLICY = "policy"


@dataclass(frozen=True)
class EvidenceItem:
    evidence_id: str
    kind: EvidenceKind
    title: str
    content: str


@dataclass(frozen=True)
class ChatContext:
    invoice: Invoice
    validation: ValidationResult
    policy_sections: list[PolicySection]
    evidence: list[EvidenceItem] = field(default_factory=list)

    @property
    def allowed_ids(self) -> set[str]:
        return {item.evidence_id for item in self.evidence}

    def get(self, evidence_id: str) -> EvidenceItem | None:
        return next((e for e in self.evidence if e.evidence_id == evidence_id), None)


def money(amount: Decimal | None, currency: str | None) -> str:
    if amount is None:
        return "not available"
    return f"{currency or ''} {amount:,.2f}".strip()


def build_invoice_evidence(invoice: Invoice, validation: ValidationResult) -> list[EvidenceItem]:
    cur = invoice.currency
    vendor = invoice.vendor
    header = [
        f"Invoice number: {invoice.invoice_number or 'not stated'}",
        f"Vendor: {vendor.name if vendor and vendor.name else 'not stated'}"
        + (f" (country {vendor.country})" if vendor and vendor.country else ""),
        f"Issue date: {invoice.issue_date or 'not stated'}",
        f"Due date: {invoice.due_date or 'not stated'}",
        f"Currency: {cur or 'not stated'}",
        f"Purchase order: {invoice.purchase_order or 'not stated'}",
        f"Cost center: {(invoice.buyer.cost_center if invoice.buyer else None) or 'not stated'}",
    ]
    calc_by_line = {line.line_id: line for line in validation.calculated.lines}
    lines = []
    for item in invoice.line_items:
        calc = calc_by_line.get(item.line_id)
        lines.append(
            f"Line {item.line_id}: {item.description or 'no description'}; quantity "
            f"{item.quantity if item.quantity is not None else 'not stated'}; unit price "
            f"{money(item.unit_price, cur)}; stated line total {money(item.line_total, cur)}; "
            f"calculated line total {money(calc.calculated_total if calc else None, cur)}"
        )
    c = validation.calculated
    vat_rate = f"{invoice.vat_rate:.0%}" if invoice.vat_rate is not None else "not stated"
    totals = [
        f"Stated subtotal: {money(invoice.subtotal, cur)}; calculated: {money(c.subtotal, cur)}",
        f"VAT rate: {vat_rate}",
        f"Stated VAT: {money(invoice.vat_amount, cur)}; calculated: {money(c.vat_amount, cur)}",
        f"Stated total: {money(invoice.total, cur)}; calculated: {money(c.total, cur)}",
        f"Rounding tolerance: {money(validation.rounding_tolerance, cur)}",
    ]
    approval = invoice.approval
    approval_lines = [
        f"Approval required by policy: {validation.required_approval_role or 'none'}",
        (
            f"Approval recorded: {approval.approver_role or 'role not stated'} on "
            f"{approval.approved_on or 'date not stated'}"
            if approval
            else "Approval recorded: none"
        ),
    ]
    checks = [
        f"{check.rule_id} {check.name}: {check.status.value} — {check.detail}"
        for check in validation.checks
    ]
    items = [
        EvidenceItem("INV-HEADER", EvidenceKind.INVOICE, "Invoice header", "\n".join(header)),
        EvidenceItem("INV-LINES", EvidenceKind.INVOICE, "Invoice line items", "\n".join(lines)),
        EvidenceItem("INV-TOTALS", EvidenceKind.INVOICE, "Invoice totals", "\n".join(totals)),
        EvidenceItem(
            "INV-APPROVAL", EvidenceKind.INVOICE, "Approval record", "\n".join(approval_lines)
        ),
        EvidenceItem(
            "VAL-RESULT",
            EvidenceKind.VALIDATION,
            "Validation result",
            f"Overall status: {validation.status.value}\nFindings: {len(validation.findings)}\n"
            + "\n".join(checks),
        ),
    ]
    for finding in validation.findings:
        items.append(
            EvidenceItem(
                finding.finding_id,
                EvidenceKind.FINDING,
                finding.title,
                f"Severity: {finding.severity.value}\n{finding.description}\n"
                f"Expected: {finding.expected_value}; actual: {finding.actual_value}\n"
                f"Policy: {finding.policy_reference}\n"
                f"Recommended action: {finding.recommended_action}",
            )
        )
    return items


def build_context(
    invoice: Invoice,
    validation: ValidationResult,
    policy_sections: list[PolicySection],
    max_chars: int,
) -> ChatContext:
    """Assemble evidence within a character budget.

    Invoice and validation evidence always fit (it is small and essential). Policy sections are
    added in priority order until the budget is used up.
    """
    evidence = build_invoice_evidence(invoice, validation)
    used = sum(len(e.content) + len(e.title) for e in evidence)
    included: list[PolicySection] = []
    for section in policy_sections:
        cost = len(section.text) + len(section.title)
        if used + cost > max_chars:
            continue
        used += cost
        included.append(section)
        evidence.append(
            EvidenceItem(
                section.section_id,
                EvidenceKind.POLICY,
                f"Policy {section.number} {section.title}",
                section.text,
            )
        )
    return ChatContext(invoice, validation, included, evidence)
