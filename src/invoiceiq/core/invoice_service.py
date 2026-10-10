"""Invoice access and processing.

`InvoiceRepository` hides where invoices come from. Milestone 1 reads JSON files bundled with the
app; Milestone 2 can add a Blob Storage implementation without changing `InvoiceService`.
`InvoiceService.process` is the entry point a future Service Bus worker would call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from invoiceiq.core.invoice_validator import DEFAULT_POLICY, ValidationPolicy, validate_invoice
from invoiceiq.core.models import Invoice, InvoiceSummary, ValidationResult


class InvoiceNotFoundError(LookupError):
    def __init__(self, invoice_id: str) -> None:
        super().__init__(f"Invoice '{invoice_id}' was not found.")
        self.invoice_id = invoice_id


class InvoiceRepository(Protocol):
    def list_invoices(self) -> list[Invoice]: ...

    def get_invoice(self, invoice_id: str) -> Invoice | None: ...


class JsonInvoiceRepository:
    """Read-only repository backed by `*.json` files, loaded once at start-up."""

    def __init__(self, directory: Path) -> None:
        invoices = [
            Invoice.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(directory.glob("*.json"))
        ]
        self._invoices = {invoice.invoice_id: invoice for invoice in invoices}
        if len(self._invoices) != len(invoices):
            raise ValueError(f"Duplicate invoice_id values in {directory}")

    def list_invoices(self) -> list[Invoice]:
        return list(self._invoices.values())

    def get_invoice(self, invoice_id: str) -> Invoice | None:
        return self._invoices.get(invoice_id)


class InvoiceService:
    def __init__(
        self, repository: InvoiceRepository, policy: ValidationPolicy = DEFAULT_POLICY
    ) -> None:
        self._repository = repository
        self._policy = policy

    def get_invoice(self, invoice_id: str) -> Invoice:
        invoice = self._repository.get_invoice(invoice_id)
        if invoice is None:
            raise InvoiceNotFoundError(invoice_id)
        return invoice

    def process(self, invoice: Invoice) -> ValidationResult:
        """Validate an invoice object. Independent of how the invoice arrived."""
        return validate_invoice(invoice, self._policy)

    def validate(self, invoice_id: str) -> ValidationResult:
        return self.process(self.get_invoice(invoice_id))

    def list_summaries(self) -> list[InvoiceSummary]:
        summaries = []
        for invoice in self._repository.list_invoices():
            result = self.process(invoice)
            summaries.append(
                InvoiceSummary(
                    invoice_id=invoice.invoice_id,
                    invoice_number=invoice.invoice_number,
                    vendor_name=invoice.vendor.name if invoice.vendor else None,
                    issue_date=invoice.issue_date,
                    currency=invoice.currency,
                    total=invoice.total,
                    status=result.status,
                    findings_count=len(result.findings),
                )
            )
        return summaries
