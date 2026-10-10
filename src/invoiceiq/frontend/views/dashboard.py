"""Dashboard: what the app is, the three sample invoices and their validation status."""

from __future__ import annotations

import streamlit as st

from invoiceiq.frontend import components, state
from invoiceiq.frontend.api_client import ApiError
from invoiceiq.frontend.presentation import count_by_status

components.hero(
    "InvoiceIQ",
    "An AI copilot for accounts payable. Python validates every invoice with exact decimal "
    "arithmetic against a purchasing policy; the AI assistant explains the verified results "
    "and cites its evidence.",
    tag="Milestone 1 · fictional demo data",
)

client = state.client()
try:
    invoices = client.list_invoices()
except ApiError as error:
    components.backend_error(error)
    st.stop()

counts = count_by_status(invoices)
kpis = st.columns(4)
kpis[0].metric("Invoices", len(invoices), border=True)
kpis[1].metric("✓ Passed", counts["Passed"], border=True)
kpis[2].metric("! Requires Review", counts["Requires Review"], border=True)
kpis[3].metric("⚑ Flagged", counts["Flagged"], border=True)

st.subheader("Sample invoices")
st.caption("Status and findings are produced by the deterministic validation engine.")
columns = st.columns(len(invoices))
for column, invoice in zip(columns, invoices, strict=True):
    with column.container(border=True):
        components.status_badge(invoice["status"])
        components.amount_block(
            invoice["invoice_number"] or invoice["invoice_id"],
            invoice["total"],
            invoice["currency"],
            note="Stated total incl. VAT",
        )
        components.definition_list(
            [
                ("Vendor", invoice["vendor_name"]),
                ("Issue date", invoice["issue_date"]),
                ("Currency", invoice["currency"]),
                ("Findings", invoice["findings_count"]),
            ]
        )
        if st.button(
            "Analyse invoice",
            key=f"open-{invoice['invoice_id']}",
            icon=":material/arrow_forward:",
            width="stretch",
        ):
            state.select_invoice(invoice["invoice_id"])
            components.go("analysis")

st.subheader("What this project demonstrates")
features = st.columns(3)
with features[0].container(border=True):
    st.markdown(
        "**:material/calculate: Deterministic validation**\n\n"
        "Line totals, subtotal, VAT, grand total, approval thresholds and mandatory fields are "
        "checked in Python with `Decimal` and round-half-up rounding. The AI never does "
        "financial arithmetic."
    )
with features[1].container(border=True):
    st.markdown(
        "**:material/menu_book: Grounded AI answers**\n\n"
        "Policy passages are retrieved with BM25 and passed to the model with the validation "
        "results. Every claim must cite evidence; citations that don't match are removed."
    )
with features[2].container(border=True):
    st.markdown(
        "**:material/shield: Production guardrails**\n\n"
        "Per-session and daily AI limits, fail-closed usage tracking, read-only fallback, "
        "input validation, correlation IDs and structured logs — built for a public demo."
    )
st.caption(
    "All data is fictional. Rules such as 25% VAT and the DKK 20,000 approval threshold are "
    "demo rules, not accounting or tax advice."
)
