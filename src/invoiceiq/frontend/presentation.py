"""Pure presentation helpers: formatting, grouping and labels. No Streamlit imports.

These functions only *arrange* data returned by the backend. They never re-validate an invoice,
compare amounts or decide whether something is a discrepancy — that is the backend's job.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from invoiceiq.frontend.api_client import Measurement

SUGGESTED_QUESTIONS: tuple[str, ...] = (
    "Explain this invoice.",
    "Why was this invoice flagged?",
    "Does it comply with our purchasing policy?",
    "What discrepancies were detected?",
    "What should the finance team do next?",
    "Draft a professional follow-up email to the supplier.",
)

# --- Formatting ------------------------------------------------------------------------------


def money(value: str | None, currency: str | None = None) -> str:
    """Format a decimal string from the API, e.g. "7952.50" → "DKK 7,952.50"."""
    if value is None:
        return "—"
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    return f"{currency or ''} {amount:,.2f}".strip()


def quantity(value: str | None) -> str:
    if value is None:
        return "—"
    try:
        return f"{Decimal(str(value)).normalize():f}"
    except InvalidOperation:
        return str(value)


def percent(value: str | None) -> str:
    if value is None:
        return "—"
    try:
        return f"{Decimal(str(value)) * 100:.0f}%"
    except InvalidOperation:
        return str(value)


# --- Status and severity ---------------------------------------------------------------------


@dataclass(frozen=True)
class StatusStyle:
    label: str
    icon: str  # Material icon for st.badge
    color: str  # st.badge colour
    symbol: str  # plain-text symbol for tables


STATUS_STYLES: dict[str, StatusStyle] = {
    "Passed": StatusStyle("Passed", ":material/check_circle:", "green", "✓"),
    "Requires Review": StatusStyle("Requires Review", ":material/rate_review:", "orange", "!"),
    "Flagged": StatusStyle("Flagged", ":material/flag:", "red", "⚑"),
}
_UNKNOWN_STATUS = StatusStyle("Unknown", ":material/help:", "gray", "?")

SEVERITY_STYLES: dict[str, StatusStyle] = {
    "high": StatusStyle("High", ":material/error:", "red", "▲"),
    "medium": StatusStyle("Medium", ":material/warning:", "orange", "■"),
    "low": StatusStyle("Low", ":material/info:", "blue", "●"),
}
_SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}

CHECK_SYMBOLS = {"passed": "✓ Passed", "failed": "✗ Failed", "skipped": "– Skipped"}


def status_style(status: str | None) -> StatusStyle:
    return STATUS_STYLES.get(status or "", _UNKNOWN_STATUS)


def severity_style(severity: str) -> StatusStyle:
    return SEVERITY_STYLES.get(severity, _UNKNOWN_STATUS)


def count_by_status(invoices: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts = dict.fromkeys(STATUS_STYLES, 0)
    for invoice in invoices:
        counts[invoice["status"]] = counts.get(invoice["status"], 0) + 1
    return counts


# --- Findings grouping -----------------------------------------------------------------------


@dataclass(frozen=True)
class FindingGroup:
    key: str
    title: str
    icon: str
    severity: str  # highest severity in the group
    findings: list[dict[str, Any]]
    summary: str


_GROUPS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("approval", "Approval controls", ":material/approval:", ("APR-",)),
    ("data", "Mandatory invoice data", ":material/assignment_late:", ("REQ-",)),
    ("arithmetic", "Invoice arithmetic", ":material/calculate:", ("CALC-",)),
    ("tax", "VAT and currency", ":material/account_balance:", ("VAT-", "CUR-")),
)

_ARITHMETIC_STEPS = {
    "CALC-001": "line totals",
    "CALC-002": "subtotal",
    "CALC-003": "VAT amount",
    "CALC-004": "total",
}


def _group_summary(key: str, findings: list[dict[str, Any]]) -> str:
    count = len(findings)
    if key == "arithmetic":
        if count == 1:
            return f"One amount check failed: the {_ARITHMETIC_STEPS[findings[0]['rule_id']]}."
        first = min(findings, key=lambda f: f["rule_id"])
        return (
            f"{count} related amount checks failed. The earliest failing step is the "
            f"**{_ARITHMETIC_STEPS[first['rule_id']]}**. Later steps are recalculated from the "
            "line items, so a single error can show up at several steps — resolve the earliest "
            "one first."
        )
    if key == "approval":
        return "The invoice cannot be paid until approval at the required level is recorded."
    if key == "data":
        return f"{count} mandatory item(s) missing — the supplier must send a corrected invoice."
    return f"{count} item(s) need manual review by the finance or tax team."


def group_findings(findings: Sequence[dict[str, Any]]) -> list[FindingGroup]:
    """Group related findings by root-cause area, most severe group first.

    Every finding is kept; grouping only changes how they are displayed.
    """
    groups: list[FindingGroup] = []
    assigned: set[str] = set()
    for key, title, icon, prefixes in _GROUPS:
        members = [f for f in findings if f["rule_id"].startswith(prefixes)]
        if not members:
            continue
        members.sort(key=lambda f: f["rule_id"])
        assigned.update(f["finding_id"] for f in members)
        severity = max((f["severity"] for f in members), key=lambda s: _SEVERITY_RANK.get(s, 0))
        groups.append(
            FindingGroup(key, title, icon, severity, members, _group_summary(key, members))
        )
    leftovers = [f for f in findings if f["finding_id"] not in assigned]
    if leftovers:
        severity = max((f["severity"] for f in leftovers), key=lambda s: _SEVERITY_RANK.get(s, 0))
        groups.append(
            FindingGroup(
                "other", "Other findings", ":material/list:", severity, leftovers,
                f"{len(leftovers)} other finding(s).",
            )
        )  # fmt: skip
    groups.sort(key=lambda g: -_SEVERITY_RANK.get(g.severity, 0))
    return groups


_LINE_FIELD_RE = re.compile(r"^line_items\[(\d+)\]")


def line_issues(validation: dict[str, Any]) -> dict[int, list[str]]:
    """Map line index → titles of findings that refer to that line (from the backend)."""
    issues: dict[int, list[str]] = {}
    for finding in validation.get("findings", []):
        match = _LINE_FIELD_RE.match(finding.get("field") or "")
        if match:
            issues.setdefault(int(match.group(1)), []).append(finding["title"])
    return issues


def check_status(validation: dict[str, Any], rule_id: str) -> str | None:
    return next(
        (c["status"] for c in validation.get("checks", []) if c["rule_id"] == rule_id), None
    )


def line_item_rows(invoice: dict[str, Any], validation: dict[str, Any]) -> list[dict[str, str]]:
    currency = invoice.get("currency")
    calculated = {line["line_id"]: line for line in validation["calculated"]["lines"]}
    issues = line_issues(validation)
    rows = []
    for index, item in enumerate(invoice.get("line_items", [])):
        calc = calculated.get(item["line_id"], {})
        if index in issues:
            check = "✗ " + "; ".join(issues[index])
        elif calc.get("calculated_total") is None or item.get("line_total") is None:
            check = "– Not verifiable"
        else:
            check = "✓ OK"
        rows.append(
            {
                "Line": item["line_id"],
                "Description": item.get("description") or "—",
                "Quantity": quantity(item.get("quantity")),
                "Unit price": money(item.get("unit_price"), currency),
                "Stated total": money(item.get("line_total"), currency),
                "Calculated total": money(calc.get("calculated_total"), currency),
                "Check": check,
            }
        )
    return rows


def totals_rows(invoice: dict[str, Any], validation: dict[str, Any]) -> list[dict[str, str]]:
    currency = invoice.get("currency")
    calc = validation["calculated"]
    rows = []
    for label, stated_key, calc_key, rule_id in (
        ("Subtotal (net)", "subtotal", "subtotal", "CALC-002"),
        (f"VAT ({percent(invoice.get('vat_rate'))})", "vat_amount", "vat_amount", "CALC-003"),
        ("Total payable", "total", "total", "CALC-004"),
    ):
        status = check_status(validation, rule_id)
        rows.append(
            {
                "Amount": label,
                "Stated on invoice": money(invoice.get(stated_key), currency),
                "Calculated by engine": money(calc.get(calc_key), currency),
                "Check": CHECK_SYMBOLS.get(status or "", "—"),
            }
        )
    return rows


def referenced_policy_ids(validation: dict[str, Any]) -> list[str]:
    """Policy sections behind the findings, or behind all checks when there are no findings."""
    source = validation.get("findings") or validation.get("checks", [])
    refs = [item["policy_reference"] for item in source]
    return sorted(dict.fromkeys(refs), key=_section_sort_key)


def _section_sort_key(section_id: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in section_id.removeprefix("PP-").split("."))
    except ValueError:
        return (999,)


# --- AI mode ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class AiMode:
    label: str
    tone: str  # success | info | warning | error
    icon: str
    description: str


READ_ONLY_REASON_TEXT: dict[str, str] = {
    "ai_disabled": "AI answers are switched off for this deployment.",
    "persistent_limiter_required": (
        "Live AI is blocked until a persistent usage limiter (planned: Azure Table Storage) is "
        "configured. This protects the public demo from unbounded cost."
    ),
    "limiter_unavailable": "AI is paused because usage cannot be recorded (fail-closed).",
    "session_limit_reached": "This session has used all of its AI questions for today.",
    "daily_limit_reached": "The demo's daily AI allowance is used up. It resets at midnight UTC.",
    "daily_budget_reached": "The demo's daily AI budget is used up. It resets at midnight UTC.",
    "pricing_not_configured": (
        "Live AI is blocked until model prices are configured, so the daily budget can be enforced."
    ),
    "ai_unavailable": "The AI service could not answer this request.",
}


def ai_mode(health: dict[str, Any] | None) -> AiMode:
    if health is None:
        return AiMode(
            "Backend offline", "error", ":material/cloud_off:",
            "The FastAPI backend is not reachable, so invoices and answers cannot be loaded.",
        )  # fmt: skip
    ai = health["ai"]
    if not ai["available"]:
        reason = READ_ONLY_REASON_TEXT.get(ai.get("block_reason") or "", "")
        if ai.get("daily_requests_remaining") == 0 and not ai.get("block_reason"):
            reason = READ_ONLY_REASON_TEXT["daily_limit_reached"]
        return AiMode(
            "Read-only mode", "warning", ":material/lock:",
            f"{reason} Verified validation results are still shown.".strip(),
        )  # fmt: skip
    if ai["live"]:
        return AiMode(
            "Live Azure OpenAI", "success", ":material/bolt:",
            "Answers are generated by an Azure OpenAI model from verified evidence.",
        )  # fmt: skip
    return AiMode(
        "Mock AI", "info", ":material/science:",
        "Offline demo mode: answers are assembled from the same verified evidence by a "
        "deterministic template. No language model is called and no cost is incurred.",
    )  # fmt: skip


def answer_source_label(response: dict[str, Any]) -> tuple[str, str]:
    """Label and badge colour describing where a chat answer came from."""
    if response.get("mode") == "read_only":
        return "Read-only · verified data, no AI", "gray"
    provider = response.get("provider")
    if provider == "mock":
        return "AI-generated · mock provider", "blue"
    if provider == "azure_openai":
        return "AI-generated · Azure OpenAI", "violet"
    return f"AI-generated · {provider}", "blue"


def history_payload(
    messages: Sequence[dict[str, Any]], limit: int = 6, max_chars: int = 1_500
) -> list[dict[str, str]]:
    """Recent turns to send as context. Error placeholders are skipped."""
    turns = [
        {"role": m["role"], "content": m["content"][:max_chars]}
        for m in messages
        if m.get("content") and not m.get("error")
    ]
    return turns[-limit:] if limit else []


# --- Usage labels ------------------------------------------------------------------------------


def token_note(sources: Iterable[str | None]) -> str:
    """Qualifier for token counts: provider-reported counts need none."""
    kinds = {s for s in sources if s}
    if kinds == {"simulated"}:
        return " (simulated)"
    if "simulated" in kinds:
        return " (incl. simulated)"
    if "estimated" in kinds:
        return " (estimated)"
    return ""


def spend_label(cost: str | Decimal | None, currency: str | None, simulated: bool) -> str:
    """AI spend for display. Mock answers cost nothing because no model is called."""
    if simulated:
        return f"0 {currency or 'USD'} (mock, no model call)"
    if cost is None:
        return "—"
    return f"≈ {Decimal(str(cost)):.6f} {currency or ''}".strip()


# --- Observability ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservabilitySummary:
    requests: int
    failed_requests: int
    median_round_trip_ms: float | None
    median_server_ms: float | None
    chat_requests: int
    last_ai_ms: float | None
    median_ai_ms: float | None
    prompt_tokens: int
    completion_tokens: int
    token_sources: frozenset[str]
    simulated_chat_requests: int
    estimated_cost: Decimal | None
    cost_currency: str | None


def summarize(measurements: Sequence[Measurement]) -> ObservabilitySummary:
    """Aggregate measurements recorded in this browser session. Missing data stays missing."""
    round_trips = [m.round_trip_ms for m in measurements]
    server = [m.server_ms for m in measurements if m.server_ms is not None]
    chats = [m for m in measurements if m.path == "/chat" and m.status_code == 200]
    ai_times = [m.ai_ms for m in chats if m.ai_ms is not None]
    costs = [m for m in chats if m.estimated_cost is not None]
    currencies = {m.cost_currency for m in costs if m.cost_currency}
    total_cost: Decimal | None = None
    if costs and len(currencies) <= 1:
        total_cost = sum((Decimal(m.estimated_cost or "0") for m in costs), Decimal("0"))
    return ObservabilitySummary(
        requests=len(measurements),
        failed_requests=sum(
            1 for m in measurements if m.status_code is None or m.status_code >= 500
        ),
        median_round_trip_ms=round(statistics.median(round_trips), 1) if round_trips else None,
        median_server_ms=round(statistics.median(server), 1) if server else None,
        chat_requests=len(chats),
        last_ai_ms=ai_times[-1] if ai_times else None,
        median_ai_ms=round(statistics.median(ai_times), 1) if ai_times else None,
        prompt_tokens=sum(m.prompt_tokens or 0 for m in chats),
        completion_tokens=sum(m.completion_tokens or 0 for m in chats),
        token_sources=frozenset(m.token_source for m in chats if m.token_source),
        simulated_chat_requests=sum(1 for m in chats if m.token_source == "simulated"),
        estimated_cost=total_cost,
        cost_currency=next(iter(currencies), None) if total_cost is not None else None,
    )


def measurement_rows(measurements: Sequence[Measurement]) -> list[dict[str, Any]]:
    """Newest first, for a table view."""

    def show(value: object, fmt: str = "{}") -> str:
        return "—" if value is None else fmt.format(value)

    rows = []
    for m in reversed(measurements):
        tokens = None
        if m.prompt_tokens is not None and m.completion_tokens is not None:
            tokens = m.prompt_tokens + m.completion_tokens
        rows.append(
            {
                "Time (UTC)": m.timestamp.strftime("%H:%M:%S"),
                "Request": f"{m.method} {m.path}",
                "Status": show(m.status_code) if m.status_code is not None else "no response",
                "Round trip (ms)": show(m.round_trip_ms, "{:.1f}"),
                "Server (ms)": show(m.server_ms, "{:.1f}"),
                "AI (ms)": show(m.ai_ms, "{:.1f}"),
                "Tokens": show(tokens, "{:,}"),
                "Token source": show(m.token_source),
                "Est. cost": f"{m.estimated_cost} {m.cost_currency}" if m.estimated_cost else "—",
                "Correlation ID": show(m.correlation_id),
            }
        )
    return rows
