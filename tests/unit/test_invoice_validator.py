from __future__ import annotations

from decimal import Decimal

import pytest

from invoiceiq.core.invoice_service import InvoiceService, JsonInvoiceRepository
from invoiceiq.core.invoice_validator import RULES, round_money, validate_invoice
from invoiceiq.core.models import CheckStatus, InvoiceStatus, Severity
from tests.conftest import DATA_DIR, make_invoice, single_line_invoice


def rule_ids(result) -> list[str]:
    return [f.rule_id for f in result.findings]


def check_status(result, rule_id: str) -> CheckStatus:
    return next(c.status for c in result.checks if c.rule_id == rule_id)


# --- Sample invoices -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def service() -> InvoiceService:
    return InvoiceService(JsonInvoiceRepository(DATA_DIR / "invoices"))


def test_sample_invoice_1_passes(service):
    result = service.validate("inv-001")
    assert result.status == InvoiceStatus.PASSED
    assert result.findings == []
    assert result.calculated.subtotal == Decimal("6362.00")
    assert result.calculated.vat_amount == Decimal("1590.50")
    assert result.calculated.total == Decimal("7952.50")
    assert result.required_approval_role is None


def test_sample_invoice_2_flagged_for_missing_approval(service):
    result = service.validate("inv-002")
    assert result.status == InvoiceStatus.FLAGGED
    assert rule_ids(result) == ["APR-001"]
    finding = result.findings[0]
    assert finding.policy_reference == "PP-3.1"
    assert finding.severity == Severity.HIGH
    assert result.required_approval_role == "manager"


def test_sample_invoice_3_requires_review_for_vat_and_total(service):
    result = service.validate("inv-003")
    assert result.status == InvoiceStatus.REQUIRES_REVIEW
    assert rule_ids(result) == ["CALC-003", "CALC-004"]
    vat, total = result.findings
    assert (vat.expected_value, vat.actual_value) == ("3112.50", "3312.50")
    assert (total.expected_value, total.actual_value) == ("15562.50", "15762.50")
    assert check_status(result, "CALC-001") == CheckStatus.PASSED
    assert check_status(result, "CALC-002") == CheckStatus.PASSED


def test_summaries_match_validation(service):
    summaries = {s.invoice_id: s for s in service.list_summaries()}
    assert summaries["inv-001"].status == InvoiceStatus.PASSED
    assert summaries["inv-002"].findings_count == 1
    assert summaries["inv-003"].findings_count == 2


# --- Arithmetic ----------------------------------------------------------------------------------


def test_correct_invoice_has_no_findings():
    result = validate_invoice(make_invoice())
    assert result.status == InvoiceStatus.PASSED
    assert all(c.status != CheckStatus.FAILED for c in result.checks)


def test_line_total_mismatch_detected_and_cascades_to_calculated_amounts():
    invoice = make_invoice(
        line_items=[
            {
                "line_id": "1",
                "description": "Widget",
                "quantity": "10",
                "unit_price": "100.00",
                "line_total": "1100.00",
            },
            {
                "line_id": "2",
                "description": "Gadget",
                "quantity": "2",
                "unit_price": "250.00",
                "line_total": "500.00",
            },
        ],
        subtotal="1600.00",
        vat_amount="400.00",
        total="2000.00",
    )
    result = validate_invoice(invoice)
    assert rule_ids(result) == ["CALC-001", "CALC-002", "CALC-003", "CALC-004"]
    line = result.findings[0]
    assert line.finding_id == "CALC-001-L1"
    assert (line.expected_value, line.actual_value) == ("1000.00", "1100.00")
    assert line.field == "line_items[0].line_total"
    # Expected amounts are always derived from quantity × unit price, never from stated values.
    assert result.calculated.total == Decimal("1875.00")
    assert result.status == InvoiceStatus.REQUIRES_REVIEW


def test_subtotal_mismatch_detected():
    result = validate_invoice(make_invoice(subtotal="1550.00"))
    assert rule_ids(result) == ["CALC-002"]
    assert result.findings[0].expected_value == "1500.00"


def test_vat_amount_mismatch_detected():
    result = validate_invoice(make_invoice(vat_amount="300.00", total="1800.00"))
    assert rule_ids(result) == ["CALC-003", "CALC-004"]


def test_total_mismatch_detected():
    result = validate_invoice(make_invoice(total="1885.00"))
    assert rule_ids(result) == ["CALC-004"]
    assert result.findings[0].policy_reference == "PP-4.2"


@pytest.mark.parametrize(
    ("stated_total", "should_flag"),
    [
        ("1875.00", False),
        ("1875.01", False),  # exactly at tolerance (DKK 0.01) → accepted
        ("1874.99", False),
        ("1875.02", True),  # just beyond tolerance → discrepancy
        ("1874.98", True),
    ],
)
def test_rounding_tolerance_boundary(stated_total, should_flag):
    result = validate_invoice(make_invoice(total=stated_total))
    assert ("CALC-004" in rule_ids(result)) is should_flag


def test_line_total_uses_round_half_up():
    # 3 × 0.125 = 0.375 → 0.38 with half-up (banker's rounding would give 0.38 too, so also
    # check a value where they differ: 1 × 0.125 = 0.125 → 0.13 half-up, 0.12 half-even).
    invoice = make_invoice(
        line_items=[
            {
                "line_id": "1",
                "description": "Bolt",
                "quantity": "1",
                "unit_price": "0.125",
                "line_total": "0.13",
            }
        ],
        subtotal="0.13",
        vat_amount="0.03",
        total="0.16",
    )
    result = validate_invoice(invoice)
    assert result.findings == []
    assert result.calculated.lines[0].calculated_total == Decimal("0.13")


def test_vat_rounding_half_up():
    # 0.10 × 25% = 0.025 → 0.03 (half-up)
    assert round_money(Decimal("0.10") * Decimal("0.25")) == Decimal("0.03")
    invoice = single_line_invoice("0.10")
    assert invoice.vat_amount == Decimal("0.03")
    assert validate_invoice(invoice).findings == []


def test_decimal_avoids_float_error():
    # 0.1 + 0.2 style errors must not create false discrepancies.
    invoice = make_invoice(
        line_items=[
            {
                "line_id": "1",
                "description": "A",
                "quantity": "3",
                "unit_price": "0.10",
                "line_total": "0.30",
            },
            {
                "line_id": "2",
                "description": "B",
                "quantity": "1",
                "unit_price": "0.20",
                "line_total": "0.20",
            },
        ],
        subtotal="0.50",
        vat_amount="0.13",
        total="0.63",
    )
    assert validate_invoice(invoice).findings == []


def test_fractional_quantity():
    invoice = make_invoice(
        line_items=[
            {
                "line_id": "1",
                "description": "Consulting hours",
                "quantity": "7.5",
                "unit_price": "1133.33",
                "line_total": "8499.98",
            }
        ],
        subtotal="8499.98",
        vat_amount="2125.00",
        total="10624.98",
    )
    # 7.5 × 1133.33 = 8499.975 → 8499.98; VAT 2124.995 → 2125.00
    assert validate_invoice(invoice).findings == []


# --- VAT rate ------------------------------------------------------------------------------------


def test_non_standard_domestic_vat_rate_flagged_for_review():
    invoice = make_invoice(vat_rate="0.20", vat_amount="300.00", total="1800.00")
    result = validate_invoice(invoice)
    # Arithmetic is consistent with the stated 20%, so only the rate itself is a finding.
    assert rule_ids(result) == ["VAT-001"]
    assert result.findings[0].policy_reference == "PP-4.1"
    assert result.status == InvoiceStatus.REQUIRES_REVIEW


def test_foreign_supplier_routed_to_manual_vat_review():
    result = validate_invoice(make_invoice(vendor={"name": "Fictional GmbH", "country": "DE"}))
    assert rule_ids(result) == ["VAT-002"]
    assert result.findings[0].severity == Severity.LOW
    assert result.status == InvoiceStatus.REQUIRES_REVIEW


# --- Approval thresholds -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("net", "required_role"),
    [
        ("19999.99", None),
        ("20000.00", None),  # exactly at threshold: no approval required (PP-3.1)
        ("20000.01", "manager"),
        ("100000.00", "manager"),
        ("100000.01", "director"),
    ],
)
def test_approval_threshold_boundaries(net, required_role):
    result = validate_invoice(single_line_invoice(net))
    assert result.required_approval_role == required_role
    assert ("APR-001" in rule_ids(result)) is (required_role is not None)


def test_missing_manager_approval_is_flagged():
    result = validate_invoice(single_line_invoice("25000.00"))
    assert result.status == InvoiceStatus.FLAGGED
    finding = result.findings[0]
    assert finding.rule_id == "APR-001"
    assert finding.policy_reference == "PP-3.1"
    assert "PP-3.4" in finding.recommended_action


MANAGER_APPROVAL = {
    "approved_by": "Fictional Manager",
    "approver_role": "Manager",
    "approved_on": "2026-09-02",
}


def test_manager_approval_satisfies_manager_threshold():
    result = validate_invoice(single_line_invoice("25000.00", approval=MANAGER_APPROVAL))
    assert result.findings == []
    assert check_status(result, "APR-001") == CheckStatus.PASSED


def test_manager_approval_insufficient_above_director_threshold():
    result = validate_invoice(single_line_invoice("150000.00", approval=MANAGER_APPROVAL))
    assert rule_ids(result) == ["APR-002"]
    assert result.findings[0].policy_reference == "PP-3.2"
    assert result.status == InvoiceStatus.FLAGGED


@pytest.mark.parametrize("role", ["director", "CFO"])
def test_director_or_cfo_approval_satisfies_director_threshold(role):
    approval = {**MANAGER_APPROVAL, "approver_role": role}
    assert validate_invoice(single_line_invoice("150000.00", approval=approval)).findings == []


def test_unknown_role_is_insufficient():
    approval = {**MANAGER_APPROVAL, "approver_role": "intern"}
    assert rule_ids(validate_invoice(single_line_invoice("25000.00", approval=approval))) == [
        "APR-002"
    ]


@pytest.mark.parametrize(
    "approval",
    [
        {"approved_by": "", "approver_role": "manager", "approved_on": "2026-09-02"},
        {"approved_by": "Fictional Manager", "approver_role": "manager", "approved_on": None},
    ],
)
def test_incomplete_approval_record_counts_as_missing(approval):
    assert rule_ids(validate_invoice(single_line_invoice("25000.00", approval=approval))) == [
        "APR-001"
    ]


def test_threshold_uses_calculated_not_stated_net_amount():
    # Supplier understates the subtotal to stay below the threshold; lines add up to 21,000.
    invoice = make_invoice(
        line_items=[
            {
                "line_id": "1",
                "description": "Service",
                "quantity": "1",
                "unit_price": "21000.00",
                "line_total": "21000.00",
            }
        ],
        subtotal="19000.00",
        vat_amount="4750.00",
        total="23750.00",
    )
    result = validate_invoice(invoice)
    assert "APR-001" in rule_ids(result)
    assert result.required_approval_role == "manager"


def test_foreign_currency_threshold_not_evaluated():
    result = validate_invoice(single_line_invoice("25000.00", currency="EUR"))
    assert rule_ids(result) == ["CUR-001"]
    assert check_status(result, "APR-001") == CheckStatus.SKIPPED
    assert result.required_approval_role is None


# --- Required fields -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"invoice_number": None}, "invoice_number"),
        ({"invoice_number": "   "}, "invoice_number"),
        ({"vendor": None}, "vendor.name"),
        ({"vendor": {"name": "", "country": "DK"}}, "vendor.name"),
        ({"issue_date": None}, "issue_date"),
        ({"currency": None}, "currency"),
        ({"subtotal": None}, "subtotal"),
        ({"vat_rate": None}, "vat_rate"),
        ({"vat_amount": None}, "vat_amount"),
        ({"total": None}, "total"),
    ],
)
def test_missing_required_field(override, field):
    result = validate_invoice(make_invoice(**override))
    req = [f for f in result.findings if f.rule_id == "REQ-001"]
    assert [f.field for f in req] == [field]
    assert req[0].policy_reference == "PP-2.1"
    assert result.status == InvoiceStatus.FLAGGED


def test_missing_line_items_skips_arithmetic_and_threshold():
    result = validate_invoice(make_invoice(line_items=[], subtotal=None))
    assert {f.field for f in result.findings if f.rule_id == "REQ-001"} == {
        "line_items",
        "subtotal",
    }
    assert "APR-003" in rule_ids(result)  # threshold cannot be evaluated: no net amount
    assert check_status(result, "CALC-004") == CheckStatus.SKIPPED
    assert result.calculated.total is None


def test_missing_line_quantity_is_reported_and_not_invented():
    invoice = make_invoice(
        line_items=[
            {
                "line_id": "1",
                "description": "Widget",
                "quantity": None,
                "unit_price": "100.00",
                "line_total": "1500.00",
            }
        ],
    )
    result = validate_invoice(invoice)
    assert rule_ids(result)[0] == "REQ-002"
    assert result.findings[0].finding_id == "REQ-002-L1"
    assert result.calculated.subtotal is None
    # Falls back to the stated subtotal for the threshold rather than guessing.
    assert check_status(result, "APR-001") == CheckStatus.PASSED


def test_missing_vat_rate_skips_vat_checks():
    result = validate_invoice(make_invoice(vat_rate=None))
    assert check_status(result, "CALC-003") == CheckStatus.SKIPPED
    assert check_status(result, "VAT-001") == CheckStatus.SKIPPED


# --- Result structure ----------------------------------------------------------------------------


def test_every_finding_has_complete_metadata():
    invoice = make_invoice(invoice_number=None, total="9.99", vat_rate="0.20")
    for finding in validate_invoice(invoice).findings:
        assert finding.rule_id in RULES
        assert finding.policy_reference.startswith("PP-")
        assert finding.description and finding.recommended_action
        assert finding.expected_value is not None and finding.actual_value is not None


def test_finding_ids_are_unique():
    invoice = make_invoice(
        line_items=[
            {
                "line_id": "1",
                "description": "A",
                "quantity": "1",
                "unit_price": "1.00",
                "line_total": "2.00",
            },
            {
                "line_id": "2",
                "description": "B",
                "quantity": "1",
                "unit_price": "1.00",
                "line_total": "2.00",
            },
        ],
    )
    ids = [f.finding_id for f in validate_invoice(invoice).findings]
    assert len(ids) == len(set(ids))


def test_validation_is_deterministic():
    invoice = make_invoice(total="2000.00")
    assert validate_invoice(invoice) == validate_invoice(invoice)
