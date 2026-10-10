"""Shared fixtures. All data is fictional."""

from __future__ import annotations

from typing import Any

import pytest

from invoiceiq.configuration import PROJECT_ROOT
from invoiceiq.core.models import Invoice
from invoiceiq.core.policy_retriever import BM25PolicyRetriever

DATA_DIR = PROJECT_ROOT / "data"


def make_invoice(**overrides: Any) -> Invoice:
    """A valid, below-threshold domestic invoice; override any field to break a rule."""
    data: dict[str, Any] = {
        "invoice_id": "test-001",
        "invoice_number": "TST-0001",
        "vendor": {"name": "Testleverandør ApS (fictional)", "country": "DK"},
        "buyer": {"name": "Aurora Nordic Trading ApS", "country": "DK"},
        "issue_date": "2026-09-01",
        "currency": "DKK",
        "line_items": [
            {
                "line_id": "1",
                "description": "Widget",
                "quantity": "10",
                "unit_price": "100.00",
                "line_total": "1000.00",
            },
            {
                "line_id": "2",
                "description": "Gadget",
                "quantity": "2",
                "unit_price": "250.00",
                "line_total": "500.00",
            },
        ],
        "subtotal": "1500.00",
        "vat_rate": "0.25",
        "vat_amount": "375.00",
        "total": "1875.00",
        "approval": None,
    }
    data.update(overrides)
    return Invoice.model_validate(data)


def single_line_invoice(net: str, **overrides: Any) -> Invoice:
    """An arithmetically correct one-line invoice with the given net amount."""
    from decimal import Decimal

    from invoiceiq.core.invoice_validator import round_money

    net_d = Decimal(net)
    vat = round_money(net_d * Decimal("0.25"))
    fields: dict[str, Any] = {
        "line_items": [
            {
                "line_id": "1",
                "description": "Service",
                "quantity": "1",
                "unit_price": net,
                "line_total": net,
            }
        ],
        "subtotal": net,
        "vat_amount": str(vat),
        "total": str(net_d + vat),
    }
    fields.update(overrides)
    return make_invoice(**fields)


@pytest.fixture(scope="session")
def retriever() -> BM25PolicyRetriever:
    return BM25PolicyRetriever.from_file(DATA_DIR / "policies" / "purchasing_policy.md")
