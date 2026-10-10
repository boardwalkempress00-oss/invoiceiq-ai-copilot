from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from invoiceiq.frontend.api_client import Measurement
from invoiceiq.frontend.presentation import (
    ai_mode,
    answer_source_label,
    count_by_status,
    group_findings,
    history_payload,
    line_item_rows,
    measurement_rows,
    money,
    percent,
    quantity,
    referenced_policy_ids,
    summarize,
    totals_rows,
)


def finding(rule_id: str, severity: str = "medium", **extra) -> dict:
    return {
        "finding_id": extra.pop("finding_id", rule_id),
        "rule_id": rule_id,
        "severity": severity,
        "title": rule_id,
        "policy_reference": "PP-4.2",
        **extra,
    }


@pytest.mark.parametrize(
    ("value", "currency", "expected"),
    [
        ("7952.50", "DKK", "DKK 7,952.50"),
        ("0.1", None, "0.10"),
        (None, "DKK", "—"),
        ("abc", "DKK", "abc"),
    ],
)
def test_money(value, currency, expected):
    assert money(value, currency) == expected


def test_quantity_and_percent():
    assert quantity("20") == "20" and quantity("7.50") == "7.5" and quantity(None) == "—"
    assert percent("0.25") == "25%"


def test_count_by_status():
    invoices = [{"status": "Passed"}, {"status": "Flagged"}, {"status": "Flagged"}]
    assert count_by_status(invoices) == {"Passed": 1, "Requires Review": 0, "Flagged": 2}


def test_group_findings_keeps_every_finding_and_orders_by_severity():
    findings = [
        finding("CALC-004"),
        finding("APR-001", "high"),
        finding("CALC-003"),
        finding("VAT-002", "low"),
    ]
    groups = group_findings(findings)
    assert [g.key for g in groups] == ["approval", "arithmetic", "tax"]
    assert sum(len(g.findings) for g in groups) == len(findings)
    arithmetic = groups[1]
    assert [f["rule_id"] for f in arithmetic.findings] == ["CALC-003", "CALC-004"]
    assert "earliest failing step is the **VAT amount**" in arithmetic.summary


def test_single_arithmetic_finding_summary():
    (group,) = group_findings([finding("CALC-004")])
    assert group.summary == "One amount check failed: the total."


def test_unknown_rule_goes_to_other_group():
    (group,) = group_findings([finding("NEW-001", "high")])
    assert group.key == "other"


def test_no_findings_no_groups():
    assert group_findings([]) == []


VALIDATION = {
    "findings": [
        {
            **finding("CALC-001", finding_id="CALC-001-L2"),
            "field": "line_items[1].line_total",
            "title": "Line total mismatch",
        },
    ],
    "checks": [
        {"rule_id": "CALC-002", "status": "passed", "policy_reference": "PP-4.2"},
        {"rule_id": "CALC-003", "status": "failed", "policy_reference": "PP-4.2"},
        {"rule_id": "APR-001", "status": "passed", "policy_reference": "PP-3.1"},
    ],
    "calculated": {
        "lines": [
            {"line_id": "1", "calculated_total": "10.00", "stated_total": "10.00"},
            {"line_id": "2", "calculated_total": "5.00", "stated_total": "6.00"},
            {"line_id": "3", "calculated_total": None, "stated_total": "1.00"},
        ],
        "subtotal": None,
        "vat_amount": None,
        "total": None,
    },
}
INVOICE = {
    "currency": "DKK",
    "vat_rate": "0.25",
    "subtotal": "17.00",
    "vat_amount": "4.25",
    "total": "21.25",
    "line_items": [
        {
            "line_id": "1",
            "description": "A",
            "quantity": "1",
            "unit_price": "10.00",
            "line_total": "10.00",
        },
        {
            "line_id": "2",
            "description": "B",
            "quantity": "1",
            "unit_price": "5.00",
            "line_total": "6.00",
        },
        {
            "line_id": "3",
            "description": None,
            "quantity": None,
            "unit_price": "1.00",
            "line_total": "1.00",
        },
    ],
}


def test_line_item_rows_use_backend_findings_for_line_status():
    rows = line_item_rows(INVOICE, VALIDATION)
    assert [r["Check"] for r in rows] == ["✓ OK", "✗ Line total mismatch", "– Not verifiable"]
    assert rows[1]["Calculated total"] == "DKK 5.00"
    assert rows[2]["Description"] == "—"


def test_totals_rows_use_backend_check_status():
    rows = totals_rows(INVOICE, VALIDATION)
    assert [r["Check"] for r in rows] == ["✓ Passed", "✗ Failed", "—"]
    assert rows[1]["Amount"] == "VAT (25%)"
    assert rows[0]["Calculated by engine"] == "—"  # engine could not calculate: not invented


def test_referenced_policy_ids():
    assert referenced_policy_ids(VALIDATION) == ["PP-4.2"]
    assert referenced_policy_ids({"findings": [], "checks": VALIDATION["checks"]}) == [
        "PP-3.1",
        "PP-4.2",
    ]


def health(provider="mock", live=False, available=True, reason=None, remaining=200):
    return {
        "ai": {
            "provider": provider,
            "live": live,
            "available": available,
            "block_reason": reason,
            "daily_requests_remaining": remaining,
        }
    }


def test_ai_mode_labels():
    assert ai_mode(None).label == "Backend offline"
    assert ai_mode(health()).label == "Mock AI"
    assert ai_mode(health("azure_openai", live=True)).label == "Live Azure OpenAI"
    blocked = ai_mode(health("azure_openai", True, False, "persistent_limiter_required"))
    assert blocked.label == "Read-only mode" and "Table Storage" in blocked.description
    exhausted = ai_mode(health(available=False, remaining=0))
    assert "daily AI allowance" in exhausted.description


def test_answer_source_label():
    assert answer_source_label({"mode": "ai", "provider": "mock"})[0].endswith("mock provider")
    assert "Azure OpenAI" in answer_source_label({"mode": "ai", "provider": "azure_openai"})[0]
    assert answer_source_label({"mode": "read_only"})[0].startswith("Read-only")


def test_history_payload_skips_errors_truncates_and_limits():
    messages = [
        {"role": "user", "content": "q" * 3000},
        {"role": "assistant", "content": "x", "error": True},
    ] + [{"role": "user", "content": str(i)} for i in range(10)]
    payload = history_payload(messages, limit=3, max_chars=100)
    assert [m["content"] for m in payload] == ["7", "8", "9"]
    assert len(history_payload(messages[:1], max_chars=100)[0]["content"]) == 100
    assert history_payload(messages, limit=0) == []


def measurement(path="/invoices", status=200, **extra) -> Measurement:
    base = {
        "timestamp": datetime(2026, 10, 10, 12, 0, tzinfo=UTC),
        "method": "GET",
        "path": path,
        "status_code": status,
        "round_trip_ms": 10.0,
        "server_ms": 2.0,
        "correlation_id": "abc",
    }
    return Measurement(**{**base, **extra})


def test_summarize_empty_session_reports_nothing_measured():
    summary = summarize([])
    assert summary.requests == 0
    assert summary.median_round_trip_ms is None
    assert summary.last_ai_ms is None
    assert summary.estimated_cost is None


def test_summarize_aggregates_only_measured_values():
    chat = dict(
        path="/chat",
        method="POST",
        ai_ms=40.0,
        prompt_tokens=100,
        completion_tokens=20,
        token_source="provider",
        estimated_cost="0.000100",
        cost_currency="USD",
    )
    summary = summarize(
        [
            measurement(round_trip_ms=10.0),
            measurement(round_trip_ms=30.0, **chat),
            measurement(round_trip_ms=50.0, **{**chat, "ai_ms": 60.0}),
            measurement(status=None, round_trip_ms=5.0, server_ms=None),
        ]
    )
    assert summary.requests == 4
    assert summary.failed_requests == 1
    assert summary.median_round_trip_ms == 20.0
    assert summary.chat_requests == 2
    assert summary.last_ai_ms == 60.0
    assert summary.prompt_tokens == 200 and summary.completion_tokens == 40
    assert summary.estimated_cost == Decimal("0.000200")
    assert summary.cost_currency == "USD"


def test_summarize_without_prices_has_no_cost():
    summary = summarize(
        [
            measurement(
                path="/chat",
                ai_ms=1.0,
                prompt_tokens=5,
                completion_tokens=5,
                token_source="estimated",
            )
        ]
    )
    assert summary.estimated_cost is None
    assert summary.token_sources == frozenset({"estimated"})


def test_measurement_rows_newest_first():
    rows = measurement_rows([measurement(path="/a"), measurement(path="/b")])
    assert [r["Request"] for r in rows] == ["GET /b", "GET /a"]
    assert rows[0]["Tokens"] == "—" and rows[0]["Est. cost"] == "—"
    assert rows[0]["Round trip (ms)"] == "10.0"


def test_every_backend_read_only_reason_has_a_label():
    from invoiceiq.ai.ai_service import ReadOnlyReason  # test-only import
    from invoiceiq.frontend.presentation import READ_ONLY_REASON_TEXT

    assert {r.value for r in ReadOnlyReason} <= set(READ_ONLY_REASON_TEXT)


def test_token_note_labels_simulated_and_estimated_usage():
    from invoiceiq.frontend.presentation import token_note

    assert token_note(["provider"]) == ""
    assert token_note(["simulated"]) == " (simulated)"
    assert token_note(["simulated", "provider"]) == " (incl. simulated)"
    assert token_note(["estimated"]) == " (estimated)"
    assert token_note([None]) == ""


def test_spend_label_never_shows_a_cost_for_mock_answers():
    from invoiceiq.frontend.presentation import spend_label

    assert spend_label("0.000815", "USD", simulated=True) == "0 USD (mock, no model call)"
    assert spend_label("0.000720", "USD", simulated=False) == "≈ 0.000720 USD"
    assert spend_label(None, None, simulated=False) == "—"
