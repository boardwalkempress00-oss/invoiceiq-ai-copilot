"""Invoice Analysis: details, line items, recalculated totals, grouped findings, policy text."""

from __future__ import annotations

import streamlit as st

from invoiceiq.frontend import components, state
from invoiceiq.frontend.api_client import ApiError
from invoiceiq.frontend.presentation import (
    CHECK_SYMBOLS,
    group_findings,
    line_item_rows,
    referenced_policy_ids,
    severity_style,
    totals_rows,
)

st.title("Invoice Analysis")
client = state.client()
try:
    invoices = client.list_invoices()
    invoice_id = components.invoice_selector(invoices, key="invoice_picker")
    invoice = client.get_invoice(invoice_id)
    validation = client.validate(invoice_id)
    policy = client.policy_sections()
except ApiError as error:
    components.backend_error(error)
    st.stop()

currency = invoice.get("currency")
vendor = invoice.get("vendor") or {}
buyer = invoice.get("buyer") or {}
approval = invoice.get("approval")

# --- Header ----------------------------------------------------------------------------------
with st.container(border=True):
    head = st.columns([2, 1, 1, 1])
    with head[0]:
        components.status_badge(validation["status"])
        st.markdown(f"#### {invoice.get('invoice_number') or invoice_id}")
        st.caption(f"{vendor.get('name') or 'Vendor not stated'} · issued "
                   f"{invoice.get('issue_date') or 'n/a'}")  # fmt: skip
    with head[1]:
        components.amount_block("Stated total", invoice.get("total"), currency)
    with head[2]:
        components.amount_block(
            "Calculated total", validation["calculated"]["total"], currency, note="By engine"
        )
    with head[3]:
        st.html('<div class="iq-eyebrow">Findings</div>')
        st.markdown(f"### {len(validation['findings'])}")

# --- Details ---------------------------------------------------------------------------------
left, right = st.columns(2)
with left, st.container(border=True):
    st.markdown("**Invoice details**")
    components.definition_list(
        [
            ("Vendor", vendor.get("name")),
            ("Vendor VAT no.", vendor.get("vat_number")),
            ("Vendor country", vendor.get("country")),
            ("Buyer", buyer.get("name")),
            ("Cost center", buyer.get("cost_center")),
            ("Purchase order", invoice.get("purchase_order")),
            ("Issue date", invoice.get("issue_date")),
            ("Due date", invoice.get("due_date")),
            ("Currency", currency),
        ]
    )
with right, st.container(border=True):
    st.markdown("**Approval**")
    components.definition_list(
        [
            ("Required by policy", validation.get("required_approval_role") or "None"),
            ("Approval recorded", "Yes" if approval else "No"),
            ("Approver role", (approval or {}).get("approver_role")),
            ("Approved on", (approval or {}).get("approved_on")),
        ]
    )
    statuses = [c["status"] for c in validation["checks"]]
    st.markdown("**Checks performed**")
    st.caption(
        f"{statuses.count('passed')} passed · {statuses.count('failed')} failed · "
        f"{statuses.count('skipped')} skipped · engine v{validation['engine_version']} · "
        f"rounding tolerance {currency or ''} {validation['rounding_tolerance']}"
    )

# --- Line items and totals -------------------------------------------------------------------
st.subheader("Line items")
st.dataframe(line_item_rows(invoice, validation), hide_index=True, width="stretch")
st.subheader("Totals")
st.dataframe(totals_rows(invoice, validation), hide_index=True, width="stretch")

# --- Findings --------------------------------------------------------------------------------
st.subheader("Validation findings")
groups = group_findings(validation["findings"])
if not groups:
    st.success(
        "All validation checks passed. No discrepancies were found.", icon=":material/verified:"
    )
for group in groups:
    sev = severity_style(group.severity)
    title = f"{group.title} — {len(group.findings)} finding(s) · highest severity {sev.label}"
    with st.expander(title, expanded=True, icon=group.icon):
        st.markdown(group.summary)
        for finding in group.findings:
            fs = severity_style(finding["severity"])
            with st.container(border=True):
                top = st.columns([3, 1])
                top[0].markdown(f"**{finding['title']}** · `{finding['finding_id']}`")
                with top[1]:
                    st.badge(fs.label, icon=fs.icon, color=fs.color)  # type: ignore[arg-type]
                st.markdown(finding["description"])
                cols = st.columns(3)
                cols[0].markdown(f"**Expected**  \n{finding.get('expected_value') or '—'}")
                cols[1].markdown(f"**Actual**  \n{finding.get('actual_value') or '—'}")
                cols[2].markdown(f"**Policy**  \n`{finding['policy_reference']}`")
                st.markdown(f":material/task_alt: **Recommended action:** "
                            f"{finding['recommended_action']}")  # fmt: skip

with st.expander("All checks", icon=":material/checklist:"):
    st.dataframe(
        [
            {
                "Rule": c["rule_id"],
                "Check": c["name"],
                "Result": CHECK_SYMBOLS.get(c["status"], c["status"]),
                "Detail": c["detail"],
                "Policy": c["policy_reference"],
            }
            for c in validation["checks"]
        ],
        hide_index=True,
        width="stretch",
    )

# --- Policy references -----------------------------------------------------------------------
st.subheader("Relevant purchasing policy")
st.caption("Fictional policy text, retrieved from the backend by stable section ID.")
for section_id in referenced_policy_ids(validation):
    section = policy.get(section_id)
    if section is None:
        continue
    with st.expander(f"{section_id} · {section['title']}", icon=":material/menu_book:"):
        st.caption(section["chapter"])
        st.html(f'<div class="iq-quote">{components.esc(section["text"])}</div>')

if st.button("Ask the AI assistant about this invoice", icon=":material/forum:", type="primary"):
    components.go("chat")
