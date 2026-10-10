"""Deterministic invoice validation engine.

All arithmetic uses `Decimal` and the rounding method defined in the (fictional) purchasing policy,
section PP-4.2: round half up to two decimal places. The AI model never performs these
calculations; it only explains the results produced here.

`validate_invoice` is a pure function (invoice in, result out) so the API, the UI and a future
queue-triggered worker can all call it without any shared state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from invoiceiq.core.models import (
    CalculatedAmounts,
    CheckResult,
    CheckStatus,
    Finding,
    Invoice,
    InvoiceStatus,
    LineCalculation,
    Severity,
    ValidationResult,
)

ENGINE_VERSION = "1.0.0"
TWO_PLACES = Decimal("0.01")


def round_money(value: Decimal) -> Decimal:
    """Round to two decimal places using round-half-up (PP-4.2)."""
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class ApprovalTier:
    threshold: Decimal  # approval required when net amount is strictly above this value
    required_role: str
    policy_reference: str


@dataclass(frozen=True)
class ValidationPolicy:
    """Fictional demo rules. Values mirror data/policies/purchasing_policy.md."""

    base_currency: str = "DKK"
    domestic_country: str = "DK"
    standard_vat_rate: Decimal = Decimal("0.25")
    rounding_tolerance: Decimal = Decimal("0.01")
    approval_tiers: tuple[ApprovalTier, ...] = (
        ApprovalTier(Decimal("20000.00"), "manager", "PP-3.1"),
        ApprovalTier(Decimal("100000.00"), "director", "PP-3.2"),
    )
    # Higher rank = more authority. A role satisfies a requirement if its rank is >= required rank.
    role_rank: Mapping[str, int] = field(
        default_factory=lambda: {"manager": 1, "director": 2, "cfo": 3}
    )


DEFAULT_POLICY = ValidationPolicy()


@dataclass(frozen=True)
class RuleDefinition:
    rule_id: str
    name: str  # name of the check, e.g. "Total matches subtotal + VAT"
    finding_title: str  # title when the check fails, e.g. "Total mismatch"
    severity: Severity
    policy_reference: str
    recommended_action: str


RULES: dict[str, RuleDefinition] = {
    rule.rule_id: rule
    for rule in (
        RuleDefinition(
            "REQ-001",
            "Mandatory invoice fields present",
            "Mandatory field missing",
            Severity.HIGH,
            "PP-2.1",
            "Do not pay. Return the invoice to the supplier and request a corrected invoice "
            "that includes the missing field.",
        ),
        RuleDefinition(
            "REQ-002",
            "Line items complete",
            "Incomplete line item",
            Severity.HIGH,
            "PP-2.2",
            "Clarify the incomplete line with the supplier before payment.",
        ),
        RuleDefinition(
            "CALC-001",
            "Line totals match quantity × unit price",
            "Line total mismatch",
            Severity.MEDIUM,
            "PP-4.2",
            "Place the invoice on payment hold and request a corrected invoice or credit note "
            "(PP-5.1).",
        ),
        RuleDefinition(
            "CALC-002",
            "Subtotal matches sum of line totals",
            "Subtotal mismatch",
            Severity.MEDIUM,
            "PP-4.2",
            "Place the invoice on payment hold and request a corrected invoice or credit note "
            "(PP-5.1).",
        ),
        RuleDefinition(
            "CALC-003",
            "VAT amount matches subtotal × VAT rate",
            "VAT amount mismatch",
            Severity.MEDIUM,
            "PP-4.2",
            "Place the invoice on payment hold and request a corrected invoice or credit note "
            "(PP-5.1).",
        ),
        RuleDefinition(
            "CALC-004",
            "Total matches subtotal + VAT",
            "Total mismatch",
            Severity.MEDIUM,
            "PP-4.2",
            "Place the invoice on payment hold and request a corrected invoice or credit note "
            "(PP-5.1).",
        ),
        RuleDefinition(
            "VAT-001",
            "Standard domestic VAT rate applied",
            "Non-standard VAT rate",
            Severity.MEDIUM,
            "PP-4.1",
            "Have the finance team review the VAT rate before payment.",
        ),
        RuleDefinition(
            "VAT-002",
            "Foreign supplier VAT treatment",
            "Foreign supplier VAT review",
            Severity.LOW,
            "PP-4.4",
            "Route the invoice to the tax team for manual VAT review.",
        ),
        RuleDefinition(
            "CUR-001",
            "Invoice in base currency",
            "Non-DKK currency",
            Severity.MEDIUM,
            "PP-2.3",
            "Route the invoice to Treasury for conversion at the published rate before "
            "approval thresholds are evaluated.",
        ),
        RuleDefinition(
            "APR-001",
            "Required approval recorded",
            "Required approval missing",
            Severity.HIGH,
            "PP-3.1",
            "Place the invoice on payment hold and request approval from the budget owner at "
            "the required level (PP-3.4).",
        ),
        RuleDefinition(
            "APR-002",
            "Approver has sufficient authority",
            "Insufficient approval authority",
            Severity.HIGH,
            "PP-3.2",
            "Place the invoice on payment hold and obtain approval at the required level (PP-3.4).",
        ),
        RuleDefinition(
            "APR-003",
            "Approval threshold evaluated",
            "Approval threshold not evaluated",
            Severity.MEDIUM,
            "PP-3.1",
            "Resolve the missing amount information, then evaluate the approval requirement "
            "manually.",
        ),
    )
}

_REQUIRED_HEADER_FIELDS: tuple[tuple[str, str], ...] = (
    ("invoice_number", "invoice number"),
    ("vendor.name", "vendor name"),
    ("issue_date", "issue date"),
    ("currency", "currency"),
    ("line_items", "line items"),
    ("subtotal", "subtotal"),
    ("vat_rate", "VAT rate"),
    ("vat_amount", "VAT amount"),
    ("total", "total"),
)

_REQUIRED_LINE_FIELDS: tuple[str, ...] = ("description", "quantity", "unit_price", "line_total")


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return len(value) == 0
    return False


def _header_value(invoice: Invoice, path: str) -> object:
    if path == "vendor.name":
        return invoice.vendor.name if invoice.vendor else None
    return getattr(invoice, path)


def _fmt(amount: Decimal | None, currency: str | None) -> str:
    if amount is None:
        return "n/a"
    return f"{currency or ''} {amount:,.2f}".strip()


class _Validator:
    def __init__(self, invoice: Invoice, policy: ValidationPolicy) -> None:
        self.invoice = invoice
        self.policy = policy
        self.currency = invoice.currency
        self.findings: list[Finding] = []
        self.checks: list[CheckResult] = []

    # -- helpers -------------------------------------------------------------------------------

    def _finding(
        self,
        rule_id: str,
        description: str,
        *,
        suffix: str | None = None,
        expected: str | None = None,
        actual: str | None = None,
        field_name: str | None = None,
        policy_reference: str | None = None,
    ) -> None:
        rule = RULES[rule_id]
        self.findings.append(
            Finding(
                finding_id=f"{rule_id}-{suffix}" if suffix else rule_id,
                rule_id=rule_id,
                severity=rule.severity,
                title=rule.finding_title,
                description=description,
                expected_value=expected,
                actual_value=actual,
                policy_reference=policy_reference or rule.policy_reference,
                recommended_action=rule.recommended_action,
                field=field_name,
            )
        )

    def _check(
        self, rule_id: str, status: CheckStatus, detail: str, policy_reference: str | None = None
    ) -> None:
        rule = RULES[rule_id]
        self.checks.append(
            CheckResult(
                rule_id=rule_id,
                name=rule.name,
                status=status,
                policy_reference=policy_reference or rule.policy_reference,
                detail=detail,
            )
        )

    def _within_tolerance(self, stated: Decimal, expected: Decimal) -> bool:
        return abs(stated - expected) <= self.policy.rounding_tolerance

    # -- rules ---------------------------------------------------------------------------------

    def check_required_fields(self) -> None:
        missing = [
            (path, label)
            for path, label in _REQUIRED_HEADER_FIELDS
            if _is_missing(_header_value(self.invoice, path))
        ]
        for path, label in missing:
            self._finding(
                "REQ-001",
                f"The invoice does not state the {label}, which is a mandatory field.",
                suffix=path.replace(".", "_").upper(),
                expected="present",
                actual="missing",
                field_name=path,
            )
        if missing:
            labels = ", ".join(label for _, label in missing)
            self._check("REQ-001", CheckStatus.FAILED, f"Missing: {labels}.")
        else:
            self._check("REQ-001", CheckStatus.PASSED, "All mandatory fields are present.")

        incomplete = 0
        for index, line in enumerate(self.invoice.line_items):
            missing_line = [f for f in _REQUIRED_LINE_FIELDS if _is_missing(getattr(line, f))]
            if missing_line:
                incomplete += 1
                self._finding(
                    "REQ-002",
                    f"Line {line.line_id} is missing: {', '.join(missing_line)}.",
                    suffix=f"L{line.line_id}",
                    expected="description, quantity, unit_price, line_total",
                    actual=f"missing {', '.join(missing_line)}",
                    field_name=f"line_items[{index}]",
                )
        if not self.invoice.line_items:
            self._check("REQ-002", CheckStatus.SKIPPED, "The invoice has no line items.")
        elif incomplete:
            self._check("REQ-002", CheckStatus.FAILED, f"{incomplete} line item(s) incomplete.")
        else:
            self._check("REQ-002", CheckStatus.PASSED, "All line items are complete.")

    def check_line_totals(self) -> list[LineCalculation]:
        calculations: list[LineCalculation] = []
        mismatches = 0
        unverifiable = 0
        for index, line in enumerate(self.invoice.line_items):
            expected: Decimal | None = None
            if line.quantity is not None and line.unit_price is not None:
                expected = round_money(line.quantity * line.unit_price)
            calculations.append(
                LineCalculation(
                    line_id=line.line_id, calculated_total=expected, stated_total=line.line_total
                )
            )
            if expected is None or line.line_total is None:
                unverifiable += 1
                continue
            if not self._within_tolerance(line.line_total, expected):
                mismatches += 1
                difference = line.line_total - expected
                self._finding(
                    "CALC-001",
                    f"Line {line.line_id}: {line.quantity} × {_fmt(line.unit_price, self.currency)}"
                    f" = {_fmt(expected, self.currency)}, but the invoice states "
                    f"{_fmt(line.line_total, self.currency)} (difference "
                    f"{_fmt(difference, self.currency)}).",
                    suffix=f"L{line.line_id}",
                    expected=str(expected),
                    actual=str(line.line_total),
                    field_name=f"line_items[{index}].line_total",
                )

        if not self.invoice.line_items:
            self._check("CALC-001", CheckStatus.SKIPPED, "No line items to verify.")
        elif mismatches:
            self._check("CALC-001", CheckStatus.FAILED, f"{mismatches} line total(s) incorrect.")
        elif unverifiable:
            self._check(
                "CALC-001", CheckStatus.SKIPPED, f"{unverifiable} line(s) could not be verified."
            )
        else:
            self._check("CALC-001", CheckStatus.PASSED, "All line totals are correct.")
        return calculations

    def _compare_amount(
        self,
        rule_id: str,
        label: str,
        field_name: str,
        stated: Decimal | None,
        expected: Decimal | None,
        how: str,
    ) -> None:
        if expected is None or stated is None:
            reason = "stated value missing" if stated is None else "inputs incomplete"
            self._check(rule_id, CheckStatus.SKIPPED, f"Cannot verify {label}: {reason}.")
            return
        if self._within_tolerance(stated, expected):
            self._check(
                rule_id,
                CheckStatus.PASSED,
                f"Stated {label} {_fmt(stated, self.currency)} matches the calculated value.",
            )
            return
        difference = stated - expected
        self._finding(
            rule_id,
            f"The stated {label} is {_fmt(stated, self.currency)}, but {how} gives "
            f"{_fmt(expected, self.currency)} (difference {_fmt(difference, self.currency)}; "
            f"tolerance {_fmt(self.policy.rounding_tolerance, self.currency)}).",
            expected=str(expected),
            actual=str(stated),
            field_name=field_name,
        )
        self._check(
            rule_id,
            CheckStatus.FAILED,
            f"Stated {label} differs from the calculated value by "
            f"{_fmt(difference, self.currency)}.",
        )

    def check_vat_rate(self) -> None:
        vendor_country = self.invoice.vendor.country if self.invoice.vendor else None
        rate = self.invoice.vat_rate
        if not vendor_country:
            self._check("VAT-001", CheckStatus.SKIPPED, "Vendor country unknown.")
            return
        if vendor_country.upper() != self.policy.domestic_country:
            self._finding(
                "VAT-002",
                f"The vendor is registered in {vendor_country}. VAT treatment of foreign "
                "suppliers is outside the scope of automated validation.",
                expected=f"vendor country {self.policy.domestic_country}",
                actual=f"vendor country {vendor_country}",
                field_name="vendor.country",
            )
            self._check("VAT-002", CheckStatus.FAILED, "Foreign supplier: manual VAT review.")
            return
        if rate is None:
            self._check("VAT-001", CheckStatus.SKIPPED, "VAT rate not stated.")
            return
        if rate != self.policy.standard_vat_rate:
            self._finding(
                "VAT-001",
                f"The invoice applies a VAT rate of {rate:.2%}, but the standard domestic rate "
                f"is {self.policy.standard_vat_rate:.0%}.",
                expected=str(self.policy.standard_vat_rate),
                actual=str(rate),
                field_name="vat_rate",
            )
            self._check("VAT-001", CheckStatus.FAILED, f"Non-standard VAT rate {rate:.2%}.")
        else:
            self._check(
                "VAT-001",
                CheckStatus.PASSED,
                f"Standard domestic VAT rate {rate:.0%} applied.",
            )

    def check_approval(self, net_amount: Decimal | None) -> str | None:
        """Returns the approval role required by policy, or None if none is required."""
        currency = self.invoice.currency
        if currency and currency.upper() != self.policy.base_currency:
            self._finding(
                "CUR-001",
                f"The invoice is in {currency}. Approval thresholds are defined in "
                f"{self.policy.base_currency} and no verified exchange rate is available, so the "
                "approval requirement cannot be evaluated automatically.",
                expected=self.policy.base_currency,
                actual=currency,
                field_name="currency",
            )
            self._check("CUR-001", CheckStatus.FAILED, f"Invoice currency is {currency}.")
            self._check("APR-001", CheckStatus.SKIPPED, "Requires conversion by Treasury first.")
            return None
        if currency:
            self._check("CUR-001", CheckStatus.PASSED, f"Invoice is in {currency}.")

        if net_amount is None:
            self._finding(
                "APR-003",
                "The net amount could not be determined, so the approval threshold could not be "
                "evaluated.",
                expected="determinable net amount",
                actual="unknown",
                field_name="subtotal",
            )
            self._check("APR-001", CheckStatus.SKIPPED, "Net amount unknown.")
            return None

        tier = next(
            (
                t
                for t in sorted(self.policy.approval_tiers, key=lambda t: t.threshold, reverse=True)
                if net_amount > t.threshold
            ),
            None,
        )
        if tier is None:
            lowest = min(t.threshold for t in self.policy.approval_tiers)
            self._check(
                "APR-001",
                CheckStatus.PASSED,
                f"Net amount {_fmt(net_amount, currency)} does not exceed "
                f"{_fmt(lowest, currency)}; no approval required.",
            )
            return None

        approval = self.invoice.approval
        threshold_text = (
            f"net amount {_fmt(net_amount, currency)} exceeds {_fmt(tier.threshold, currency)}"
        )
        if approval is None or _is_missing(approval.approved_by) or approval.approved_on is None:
            self._finding(
                "APR-001",
                f"The {threshold_text}, which requires {tier.required_role} approval, but no "
                "complete approval record (approver, role and date) is present.",
                expected=f"{tier.required_role} approval",
                actual="no approval recorded",
                field_name="approval",
                policy_reference=tier.policy_reference,
            )
            self._check(
                "APR-001",
                CheckStatus.FAILED,
                f"{tier.required_role.capitalize()} approval required but missing.",
                policy_reference=tier.policy_reference,
            )
            return tier.required_role

        role = (approval.approver_role or "").strip().lower()
        role_label = approval.approver_role or "unknown"
        required_rank = self.policy.role_rank[tier.required_role]
        if self.policy.role_rank.get(role, 0) < required_rank:
            self._finding(
                "APR-002",
                f"The {threshold_text}, which requires {tier.required_role} approval, but the "
                f"invoice was approved by a person with role '{role_label}'.",
                expected=f"{tier.required_role} or higher",
                actual=role_label,
                field_name="approval.approver_role",
                policy_reference=tier.policy_reference,
            )
            self._check(
                "APR-001",
                CheckStatus.FAILED,
                "Approval recorded at insufficient level.",
                policy_reference=tier.policy_reference,
            )
            return tier.required_role

        self._check(
            "APR-001",
            CheckStatus.PASSED,
            f"{tier.required_role.capitalize()} approval required and recorded "
            f"({approval.approver_role}, {approval.approved_on.isoformat()}).",
            policy_reference=tier.policy_reference,
        )
        return tier.required_role

    def run(self) -> ValidationResult:
        invoice = self.invoice
        self.check_required_fields()
        lines = self.check_line_totals()

        calc_subtotal: Decimal | None = None
        if lines and all(line.calculated_total is not None for line in lines):
            calc_subtotal = sum(
                (line.calculated_total for line in lines if line.calculated_total is not None),
                Decimal("0"),
            )
        calc_vat = (
            round_money(calc_subtotal * invoice.vat_rate)
            if calc_subtotal is not None and invoice.vat_rate is not None
            else None
        )
        calc_total = (
            calc_subtotal + calc_vat if calc_subtotal is not None and calc_vat is not None else None
        )

        self._compare_amount(
            "CALC-002",
            "subtotal",
            "subtotal",
            invoice.subtotal,
            calc_subtotal,
            "the sum of the recalculated line totals",
        )
        self._compare_amount(
            "CALC-003",
            "VAT amount",
            "vat_amount",
            invoice.vat_amount,
            calc_vat,
            f"subtotal × VAT rate ({invoice.vat_rate})",
        )
        self._compare_amount(
            "CALC-004",
            "total",
            "total",
            invoice.total,
            calc_total,
            "recalculated subtotal + recalculated VAT",
        )
        self.check_vat_rate()

        # PP-3.1: thresholds use the calculated net amount; fall back to the stated subtotal only
        # when the line items cannot be recalculated.
        net_amount = calc_subtotal if calc_subtotal is not None else invoice.subtotal
        required_role = self.check_approval(net_amount)

        return ValidationResult(
            invoice_id=invoice.invoice_id,
            status=_status_for(self.findings),
            findings=self.findings,
            checks=self.checks,
            calculated=CalculatedAmounts(
                lines=lines, subtotal=calc_subtotal, vat_amount=calc_vat, total=calc_total
            ),
            required_approval_role=required_role,
            rounding_tolerance=self.policy.rounding_tolerance,
            engine_version=ENGINE_VERSION,
        )


def _status_for(findings: list[Finding]) -> InvoiceStatus:
    """High severity → Flagged; any other finding → Requires Review; none → Passed."""
    if any(f.severity == Severity.HIGH for f in findings):
        return InvoiceStatus.FLAGGED
    if findings:
        return InvoiceStatus.REQUIRES_REVIEW
    return InvoiceStatus.PASSED


def validate_invoice(
    invoice: Invoice, policy: ValidationPolicy = DEFAULT_POLICY
) -> ValidationResult:
    """Validate one invoice deterministically. Pure function: safe to call from any worker."""
    return _Validator(invoice, policy).run()
